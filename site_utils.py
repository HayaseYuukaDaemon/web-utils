import enum
import hashlib
import pydantic
from pydantic import BaseModel
from pathlib import Path
from setup_logger import get_logger
import fastapi
import json

logger = get_logger('SiteUtils')


class UserAbilities(enum.Enum):
    PROXY_READ = 'proxy.read'
    STATIC_READ = 'static.read'
    CLIPBOARD_READ = 'clipboard.read'
    CLIPBOARD_WRITE = 'clipboard.write'
    VAULT_READ = 'vault.read'
    VAULT_CREATE = 'vault.write'
    VAULT_DELETE = 'vault.delete'
    DATASET_USE = 'dataset.use'  # 使用图片评分系统


class UserInfo(BaseModel):
    username: str
    abilities: list[UserAbilities]
    admin: bool = False

    @property
    def is_admin(self):
        return self.admin

    def has_ability(self, ability: UserAbilities):
        return ability in self.abilities


class UserConfig(BaseModel):
    users: dict[str, UserInfo]


auth_file_path = Path('auth.json')
auth_config: UserConfig | None = None
if auth_file_path.exists():
    with open(auth_file_path) as pwd_f:
        auth_file_content = pwd_f.read()
        try:
            auth_config = UserConfig.model_validate(json.loads(auth_file_content))
        except (pydantic.ValidationError, json.JSONDecodeError) as ve:
            logger.warning(f'认证文件不合规, 将忽略: {ve}')
else:
    logger.warning('认证文件未配置, 默认允许所有人进行任何操作')


async def get_current_user(request: fastapi.Request) -> UserInfo | None:
    token = request.cookies.get("auth_token", None)
    if not token:
        token = request.query_params.get("auth_token", None)
    if not token:
        token = request.headers.get('auth_token', None)
    if auth_config is None:
        return UserInfo(username='__DEFAULT_ADMIN__', admin=True, abilities=[])
    if not token:
        return None
    user = auth_config.users.get(token, None)
    if not user:
        return None
    return user


class Authoricator:
    def __init__(
            self,
            required_abilities: list[UserAbilities] | None = None,
    ):
        self.required_abilities = required_abilities

    async def __call__(self, user: UserInfo = fastapi.Depends(get_current_user)) -> UserInfo | None:
        if user is None:
            raise fastapi.HTTPException(status_code=fastapi.status.HTTP_401_UNAUTHORIZED, detail='需要登录, 或用户不存在')
        if user.admin:
            return user
        if self.required_abilities is None:
            return user
        for ability in self.required_abilities:
            if not user.has_ability(ability):
                raise fastapi.HTTPException(status_code=fastapi.status.HTTP_403_FORBIDDEN,
                                            detail=f'当前操作需要权限: {ability}, 用户 {user.username}无该权限')
        return user


def get_file_hash(file_path: Path, chunk_size: int = 65536) -> str:
    hash_md5 = hashlib.md5()
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()
