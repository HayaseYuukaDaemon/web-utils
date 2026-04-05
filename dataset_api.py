"""
图片访问 API 端点

提供图片访问接口，redirect 到 Cloudflare R2
"""

import asyncio
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from fastapi.responses import RedirectResponse, FileResponse
from pydantic import BaseModel

from dataset_db import DatasetDB
from pixiv_fetcher import PixivFetcher
from setup_logger import get_logger
from site_utils import Authoricator, UserAbilities


# 创建路由
dataset_router = APIRouter(prefix='/dataset', tags=['dataset'])

# 日志
logger = get_logger('DatasetAPI')

# 全局实例
db = DatasetDB()

# 初始化 fetcher
try:
    fetcher = PixivFetcher.from_config()
    logger.info(f"PixivFetcher 初始化成功 (mock_mode={fetcher.mock_mode})")
except Exception as e:
    logger.error(f"无法初始化 PixivFetcher: {e}", exc_info=True)
    fetcher = None

# 维护任务锁（防止并发执行）
maintenance_lock = asyncio.Lock()
maintenance_running = False


class JudgeRequest(BaseModel):
    """评分请求"""
    pid: int
    page_index: int
    score: int


class ImageResponse(BaseModel):
    """图片信息响应"""
    pid: int
    page_index: int
    filename: str
    image_url: str
    score: int | None
    status: str
    judged_at: str | None


class StatsResponse(BaseModel):
    """统计信息响应"""
    total_images: int
    total_works: int
    wait_count: int
    done_count: int


@dataset_router.get('/image/offset/{offset}',
                    dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))])
async def get_image_by_offset(offset: int):
    """
    按 offset 获取图片（redirect 到 R2）

    Args:
        offset: 偏移量
            - offset >= 0: 获取第 offset 张待评分图片
            - offset < 0: 获取倒数第 abs(offset) 张已评分图片

    Returns:
        RedirectResponse: 重定向到 R2 URL
    """
    # 从数据库查询图片
    image = db.get_image_by_offset(offset)
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")

    # 生成 R2 URL
    if fetcher and fetcher.r2_base_url:
        r2_url = fetcher.get_r2_url(image['local_filename'], image['status'])
        if r2_url:
            return RedirectResponse(url=r2_url, status_code=302)

    raise HTTPException(status_code=500, detail="R2 未配置")


@dataset_router.get('/image/info/offset/{offset}',
                    response_model=ImageResponse,
                    dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))])
async def get_image_info_by_offset(offset: int):
    """
    按 offset 获取图片信息（不 redirect，返回 JSON）

    Args:
        offset: 偏移量

    Returns:
        ImageResponse: 图片信息（包含 R2 URL）
    """
    image = db.get_image_by_offset(offset)
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")

    # 生成 R2 URL
    image_url = None
    if fetcher and fetcher.r2_base_url:
        image_url = fetcher.get_r2_url(image['local_filename'], image['status'])

    return ImageResponse(
        pid=image['pid'],
        page_index=image['page_index'],
        filename=image['local_filename'],
        image_url=image_url or "",
        score=image['score'],
        status=image['status'],
        judged_at=image['judged_at']
    )


@dataset_router.get('/image/{pid}/{page_index}',
                    dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))])
async def get_image(pid: int, page_index: int):
    """
    获取图片（redirect 到 R2）

    Args:
        pid: Pixiv 作品 ID
        page_index: 页码索引

    Returns:
        RedirectResponse: 重定向到 R2 URL
    """
    # 从数据库查询图片信息
    image = db.get_image_by_pid_page(pid, page_index)
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")

    # 生成 R2 URL
    if fetcher and fetcher.r2_base_url:
        r2_url = fetcher.get_r2_url(image['local_filename'], image['status'])
        if r2_url:
            return RedirectResponse(url=r2_url, status_code=302)

    # 如果没有配置 R2，返回错误
    raise HTTPException(status_code=500, detail="R2 未配置")


# 后台维护任务
async def run_maintenance_task():
    """后台执行维护任务"""
    global maintenance_running

    # 检查是否已有维护任务在运行
    if maintenance_lock.locked():
        logger.info("[维护任务] 已有维护任务在运行，跳过")
        return

    async with maintenance_lock:
        maintenance_running = True
        logger.info("[维护任务] 开始执行后台维护任务")

        try:
            if fetcher:
                result = await fetcher.auto_maintenance(min_wait=100, max_done=100)
                logger.info(f"[维护任务] 维护完成: {result}")
            else:
                logger.warning("[维护任务] Fetcher 未初始化，跳过维护")
        except Exception as e:
            logger.error(f"[维护任务] 维护失败: {e}", exc_info=True)
        finally:
            maintenance_running = False


@dataset_router.post('/judge',
                     dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))])
async def judge_image(request: JudgeRequest, background_tasks: BackgroundTasks):
    """
    提交评分

    Args:
        request: 评分请求
        background_tasks: FastAPI 后台任务

    Returns:
        成功响应
    """
    # 验证评分
    if request.score not in (0, 1, 2, 3):
        raise HTTPException(status_code=400, detail="评分必须是 0-3")

    # 评分
    success = db.judge_image(request.pid, request.page_index, request.score)
    if not success:
        raise HTTPException(status_code=400, detail="评分失败")

    # 添加后台维护任务（不阻塞响应）
    background_tasks.add_task(run_maintenance_task)
    logger.info(f"[Judge] 评分成功: pid={request.pid}, page={request.page_index}, score={request.score}")

    # 获取下一张待评分图片
    next_image = db.get_image_by_offset(0)

    return {
        "success": True,
        "message": "评分成功",
        "next_image": {
            "pid": next_image['pid'],
            "page_index": next_image['page_index']
        } if next_image else None,
        "maintenance_status": "running" if maintenance_running else "scheduled"
    }


@dataset_router.get('/',
                    dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))])
async def dataset_page():
    """渲染评分界面"""
    return FileResponse('templates/dataset.html')


@dataset_router.post('/maintenance',
                     dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))])
async def trigger_maintenance(background_tasks: BackgroundTasks):
    """
    手动触发维护任务

    用于初始化或强制拉取图片
    """
    if maintenance_lock.locked():
        return {
            "success": False,
            "message": "维护任务正在运行中",
            "status": "running"
        }

    # 添加后台维护任务
    background_tasks.add_task(run_maintenance_task)
    logger.info("[手动维护] 已触发维护任务")

    return {
        "success": True,
        "message": "维护任务已启动",
        "status": "scheduled"
    }


@dataset_router.get('/stats',
                    response_model=StatsResponse,
                    dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))])
async def get_stats():
    """
    获取统计信息

    Returns:
        StatsResponse: 统计信息
    """
    stats = db.get_stats()
    return StatsResponse(
        total_images=stats['total_images'],
        total_works=stats['total_works'],
        wait_count=stats['wait_count'],
        done_count=stats['done_count']
    )


# 在 app.py 中使用：
# from dataset_api import dataset_router
# app.include_router(dataset_router)
