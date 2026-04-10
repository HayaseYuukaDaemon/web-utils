import os
import re
from pathlib import Path
import yaml
from fastapi.staticfiles import StaticFiles
from site_utils import Authoricator, UserAbilities, get_logger
import fastapi
from fastapi import staticfiles
import pydantic
from dataclasses import field
import httpx

app_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = fastapi.FastAPI(**app_kwargs)
logger = get_logger('Site')
app.mount("/src", StaticFiles(directory="src"), name="src")

# --- 静态配置部分 ---

# --- Proxy 配置 ---
CUSTOM_CONFIG_FILE = Path('custom_config.yaml')
if CUSTOM_CONFIG_FILE.exists():
    logger.info(f'loading custom nodes from {CUSTOM_CONFIG_FILE}')
SOCKS_PROXY_ENDPOINT = 'socks5://127.0.0.1:40000'
# --- 密钥管理器配置 ---
VAULT_CONFIGS_DIR = Path('vault_configs')
if VAULT_CONFIGS_DIR.exists():
    logger.info(f'loading vault configs from {VAULT_CONFIGS_DIR}')
    assert VAULT_CONFIGS_DIR.is_dir()
else:
    logger.warning('no value configs found, creating...')
    VAULT_CONFIGS_DIR.mkdir(exist_ok=True)
# --- 静态配置结束 ---


# --- 静态文件服务 ---
static_files = staticfiles.StaticFiles(directory="static")


@app.api_route("/static/{file_path:path}",
               methods=["GET", "HEAD"],
               dependencies=[fastapi.Depends(Authoricator([UserAbilities.STATIC_READ]))])
async def serve_static_protected(file_path: str, request: fastapi.Request):
    try:
        return await static_files.get_response(file_path, request.scope)
    except Exception as e:
        logger.exception(f'Error in static file', exc_info=e)
        raise fastapi.HTTPException(status_code=404, detail="File not found")


# --- 静态文件结束 ---


# --- 认证服务 ---
@app.get('/auth',
         name='site.auth')
async def auth():
    return fastapi.responses.HTMLResponse(content=Path('templates/auth.html').read_text(encoding='utf-8'))


# --- 认证服务结束 ---


# --- Proxy 路由部分 ---
proxy_router = fastapi.APIRouter(prefix='/proxy', tags=['proxy'])


def filterOutseaProxies(lst):
    """Keep elements after the second string containing '-'."""
    count = 0
    for i, element in enumerate(lst):
        if '-' in element:
            count += 1
        if count == 2:
            return lst[i:]
    return []


def addNode(conf, node):
    """Append a proxy node and its name to the first proxy-group."""
    conf['proxies'].append(node)
    conf['proxy-groups'][-1]['proxies'].append(node['name'])


async def fetchProxy(sub_url: str) -> bytes | None:
    headers = {
        "User-Agent": "Clash/1.18.0",
        "Accept-Encoding": "gzip",  # Clash 通常只发这个
        "Connection": "keep-alive"
    }
    try:
        async with httpx.AsyncClient(proxy=httpx.Proxy(SOCKS_PROXY_ENDPOINT),
                                     headers=headers,
                                     max_redirects=50,
                                     follow_redirects=True) as client:
            response = await client.get(sub_url)
        if response.status_code == 200:
            return response.content
        else:
            logger.error(f'服务器返回状态码: {response.status_code}')
    except Exception as e:
        logger.exception('请求过程中发生错误: ', exc_info=e)
    return None


def processCNAProxy(origin_content_str: str) -> str:
    proxy_dict = yaml.safe_load(origin_content_str)
    proxy_group_white_list = ('🚀 节点选择', '💬 Telegram', '🇨🇳 中国大陆')

    diminish_proxy_groups = []

    for proxy_group in proxy_dict['proxy-groups']:
        if proxy_group['name'] not in proxy_group_white_list:
            diminish_proxy_groups.append(proxy_group['name'])

    proxy_dict['proxy-groups'] = list(filter(lambda pg: pg['name'] not in diminish_proxy_groups, proxy_dict['proxy-groups']))
    proxy_dict['rules'] = list(filter(lambda r: r.split(',')[-1] not in diminish_proxy_groups, proxy_dict['rules']))

    select_proxy_group = next((item for item in proxy_dict['proxy-groups'] if '节点选择' in item["name"]), None)
    china_proxy_group = next((item for item in proxy_dict['proxy-groups'] if '中国大陆' in item["name"]), None)
    china_proxy_group['proxies'].insert(1, '🚀 节点选择')
    select_proxy_group_proxies = select_proxy_group['proxies']
    only_foreign_proxies = []
    into_foreign_region = False
    for proxy in select_proxy_group_proxies:
        if not into_foreign_region:
            if '国际' in proxy:
                into_foreign_region = True
            continue
        only_foreign_proxies.append(proxy)
    select_proxy_group['proxies'] = only_foreign_proxies.copy()
    custom_proxy_group = {
        'name': 'Google',
        'type': 'select',
        'proxies': only_foreign_proxies
    }
    custom_proxy_group['proxies'].insert(0, '🚀 节点选择')
    proxy_dict['proxy-groups'].insert(1, custom_proxy_group)
    addional_rules = ('DOMAIN-SUFFIX,google.com,Google', 'DOMAIN-SUFFIX,googleapis.com,Google')
    for rule in addional_rules:
        proxy_dict['rules'].insert(-1, rule)
    return yaml.safe_dump(proxy_dict, allow_unicode=True, default_flow_style=False)


@proxy_router.get('/sub',
                  name='proxy.get.sub',
                  dependencies=[fastapi.Depends(Authoricator([UserAbilities.PROXY_READ]))])
async def handleSubProxies(sub_name: str = '', sub_config: str = ''):
    proxy_filename = Path('proxy_url')
    if sub_name:
        proxy_filename = Path(f'{sub_name}_{proxy_filename}')
    if not proxy_filename.exists():
        logger.warning(f"Proxy configuration error: {proxy_filename} not found.")
        raise fastapi.HTTPException(status_code=fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR)
    upstream_content = await fetchProxy(proxy_filename.read_text().strip())
    if upstream_content is None:
        raise fastapi.HTTPException(status_code=fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR,
                                    detail='详情见服务器log')
    if sub_config == 'origin':
        return fastapi.Response(upstream_content, media_type='application/x-yaml')
    proceed_proxy = processCNAProxy(upstream_content.decode())
    return fastapi.Response(proceed_proxy, media_type='application/x-yaml')


@proxy_router.get('/',
                  name='proxy.get',
                  dependencies=[fastapi.Depends(Authoricator([UserAbilities.PROXY_READ]))])
async def handleProxy():
    if not CUSTOM_CONFIG_FILE.exists():
        logger.warning(f'自定义配置文件缺失, 请求无效')
        raise fastapi.HTTPException(status_code=fastapi.status.HTTP_404_NOT_FOUND)
    return fastapi.Response(CUSTOM_CONFIG_FILE.read_text(), media_type='application/x-yaml')


app.include_router(proxy_router)
# --- Proxy 路由结束 ---

# --- 剪贴板路由部分 ---
clipboard_router = fastapi.APIRouter(prefix='/clipboard', tags=['clipboard'])
clipboard_content = ''


@clipboard_router.get('/',
                      name='clipboard.show')
async def showClipboard():
    return fastapi.responses.HTMLResponse(content=Path('templates/cloud_clipborad.html').read_text(encoding='utf-8'))


@clipboard_router.get('/api',
                      name='clipboard.get',
                      dependencies=[fastapi.Depends(Authoricator([UserAbilities.CLIPBOARD_READ]))])
async def readClipboard():
    return fastapi.responses.PlainTextResponse(content=clipboard_content)


@clipboard_router.put('/api',
                      name='clipboard.write',
                      dependencies=[fastapi.Depends(Authoricator([UserAbilities.CLIPBOARD_WRITE]))])
async def writeClipboard(request: fastapi.Request):
    global clipboard_content
    clipboard_content = await request.body()
    return fastapi.Response(status_code=fastapi.status.HTTP_200_OK)


app.include_router(clipboard_router)
# --- 剪贴板路由结束 ---

# --- 密钥管理器路由 ---
vault_router = fastapi.APIRouter(prefix='/vault', tags=['vault'])


class KeyConfig(pydantic.BaseModel):
    platform: str
    length: int
    symbols: str | None = field(default=None)


@vault_router.get('/',
                  name='vault.show',
                  dependencies=[fastapi.Depends(Authoricator())])
async def showVault():
    return fastapi.responses.HTMLResponse(content=Path('templates/vault.html').read_text(encoding='utf-8'))


@vault_router.get('/list',
                  name='vault.list',
                  dependencies=[fastapi.Depends(Authoricator())])
async def listVault():
    return fastapi.responses.HTMLResponse(content=Path('templates/list_vaults.html').read_text(encoding='utf-8'))


@vault_router.get('/api/key_configs',
                  name='vault.key_config.get',
                  dependencies=[fastapi.Depends(Authoricator([UserAbilities.VAULT_READ]))])
async def getVaultKeyConfigs():
    config_files = {Path(f) for f in os.listdir(VAULT_CONFIGS_DIR)}
    configs: dict[str, KeyConfig] = {}
    for config_filename in config_files:
        config_filepath = VAULT_CONFIGS_DIR / config_filename
        if not config_filepath.is_file():
            continue
        try:
            config = KeyConfig.model_validate(yaml.safe_load(config_filepath.read_text(encoding='utf-8')))
        except pydantic.ValidationError as e:
            logger.exception(f"解析 {config_filepath} 失败", exc_info=e)
            continue
        configs[config_filename.stem] = config
    return configs


def is_safe_filename(filename: str) -> bool:
    """
    只允许: 大小写字母(a-z, A-Z), 数字(0-9), 下划线(_)
    """
    # 1. 空检查 (必须做，否则空字符串可能导致逻辑错误)
    if not filename:
        return False
    # 2. 正则白名单匹配
    # re.ASCII 标志确保 \w 只匹配 ASCII 字符 (如果你改用 \w 的话)
    # 但这里直接写死 [a-zA-Z0-9_] 最稳，不受 locale 影响
    return bool(re.fullmatch(r'^[a-zA-Z0-9_]+$', filename))


@vault_router.put('/api/key_configs/{config_name}',
                  name='vault.key_config.put',
                  dependencies=[fastapi.Depends(Authoricator([UserAbilities.VAULT_CREATE]))])
async def setVaultKeyConfig(config_name: str, key_config: KeyConfig):
    if not is_safe_filename(config_name):
        raise fastapi.HTTPException(status_code=fastapi.status.HTTP_400_BAD_REQUEST,
                                    detail='非法文件名')
    config_filepath = VAULT_CONFIGS_DIR / f'{config_name}.yaml'
    if config_filepath.exists():
        raise fastapi.HTTPException(status_code=fastapi.status.HTTP_400_BAD_REQUEST,
                                    detail='配置已存在')
    with open(config_filepath, 'w', encoding='utf-8') as f:
        f.write(yaml.dump(key_config.model_dump()))
    return fastapi.responses.Response(status_code=fastapi.status.HTTP_204_NO_CONTENT)


@vault_router.delete('/api/key_configs/{config_name}',
                     name='vault.key_config.delete',
                     dependencies=[fastapi.Depends(Authoricator([UserAbilities.VAULT_DELETE]))])
async def deleteVaultKeyConfig(config_name: str):
    config_filepath = VAULT_CONFIGS_DIR / f'{config_name}.yaml'
    if not config_filepath.exists():
        return fastapi.responses.Response(status_code=fastapi.status.HTTP_204_NO_CONTENT)
    config_filepath.unlink()
    return fastapi.responses.Response(status_code=fastapi.status.HTTP_204_NO_CONTENT)


app.include_router(vault_router)
# --- 密钥管理器结束 ---

# --- Dataset 评分系统 ---
# 挂载 mock_r2 静态文件目录（用于本地开发）
if Path('mock_r2').exists():
    app.mount("/mock_r2", StaticFiles(directory="mock_r2"), name="mock_r2")
    logger.info('已挂载 mock_r2 静态文件目录')

# 包含 dataset 路由
from dataset_api import dataset_router, dataset_service
app.include_router(dataset_router)
logger.info('已加载 dataset 路由')


@app.on_event("shutdown")
async def flush_dataset_bookmarks_on_shutdown():
    """
    在优雅退出阶段尽量冲刷待执行收藏任务。

    第一次 Ctrl+C 会走到这里；若再次中断，uvicorn 会直接终止进程。
    """
    if dataset_service is None:
        return

    try:
        result = await dataset_service.flush_pending_bookmark_jobs_on_shutdown()
        logger.info(f'应用退出前收藏任务冲刷完成: {result}')
    except Exception as e:
        logger.error(f'应用退出前冲刷收藏任务失败: {e}', exc_info=True)
# --- Dataset 评分系统结束 ---
