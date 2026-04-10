"""
Pixiv 数据集服务模块

负责从 Pixiv 获取推荐作品、写入数据集并维护任务状态。
"""

import asyncio
from urllib.parse import quote
from pathlib import Path
from typing import Optional

import yaml

from better_pixiv import BetterPixiv, WorkDetail
from dataset_db import DatasetDB
from setup_logger import get_logger


class PixivDatasetService:
    """Pixiv 数据集服务，负责抓取、入库和维护。"""

    DOCUMENT_WORKER_BASE_URL = "https://document-worker.hayaseyuuka.date/"
    PIXIV_REFERER_URL = "https://www.pixiv.net/"

    def __init__(self,
                 refresh_token: str,
                 proxy: Optional[str] = None,
                 db_path: str = "dataset.db",
                 counts_config: dict | None = None,
                 mock_mode: bool = True):
        """
        初始化数据集服务

        Args:
            refresh_token: Pixiv refresh token
            proxy: 代理地址，如 'http://127.0.0.1:10809'
            db_path: 数据库路径
            mock_mode: 是否为 mock 模式（本地开发）
        """
        self.logger = get_logger('PixivDatasetService')
        self.refresh_token = refresh_token
        self.proxy = proxy
        self.db = DatasetDB(db_path)
        self.mock_mode = mock_mode
        self.counts_config = counts_config or {}

    def create_pixiv_client(self) -> BetterPixiv:
        """创建一个新的 BetterPixiv 上下文实例。"""
        return BetterPixiv(
            proxy=self.proxy,
            refresh_token=self.refresh_token,
            logger=self.logger
        )

    def enqueue_bookmark_job(self, pid: int) -> bool:
        """把作品加入异步收藏队列。"""
        return self.db.enqueue_bookmark_job(pid)

    @classmethod
    def from_config(cls, config_path: str = "pixiv_config.yaml"):
        """
        从配置文件创建数据集服务

        Args:
            config_path: 配置文件路径

        Returns:
            PixivDatasetService 实例
        """
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # 判断是否为 mock 模式
        mock_mode = config.get('mock_mode', True)

        return cls(
            refresh_token=config['refresh_token'],
            proxy=config.get('proxy'),
            db_path=config.get('db_path', 'dataset.db'),
            counts_config=config.get('counts_config', {}),
            mock_mode=mock_mode
        )

    def get_proxied_image_url(self, source_image_url: str | None) -> Optional[str]:
        """生成经 document-worker 反代后的图片访问 URL。"""
        if not source_image_url:
            return None
        encoded_image_url = quote(source_image_url, safe="")
        encoded_referer = quote(self.PIXIV_REFERER_URL, safe="")
        return (
            f"{self.DOCUMENT_WORKER_BASE_URL}"
            f"?urlToProxy={encoded_image_url}"
            f"&refererURL={encoded_referer}"
        )

    async def _fetch_recommended_works_with_client(
            self,
            pixiv: BetterPixiv,
            count: int,
    ) -> list[WorkDetail]:
        """在已建立的 Pixiv 会话内拉取推荐作品。"""
        works = []

        while len(works) < count:
            self.logger.debug(f'[拉取] 当前已拉取 {len(works)} 份，继续拉取...')
            batch = await pixiv.get_recommended_illusts()

            if not batch:
                self.logger.warning('[拉取] 没有更多推荐作品')
                break

            self.logger.debug(f'[拉取] 本批次获取到 {len(batch)} 份作品')
            for work in batch:
                self.logger.debug(f'[拉取] 作品: pid={work.id}, title={work.title}, pages={work.page_count}')

            works.extend(batch)
            self.logger.info(f'[拉取] 已拉取 {len(works)} 份作品')

            # 避免请求过快
            await asyncio.sleep(1)

        return works

    def _extract_work_image_records(self, work: WorkDetail) -> list[tuple[int, str, str]]:
        """
        从作品详情中提取图片页记录。

        Returns:
            [(page_index, filename, source_image_url), ...]
        """
        records: list[tuple[int, str, str]] = []
        if work.type not in ("illust", "ugoira"):
            return records

        if work.meta_pages:
            for page_index, meta_page in enumerate(work.meta_pages):
                source_image_url = meta_page.image_urls.original
                filename = Path(source_image_url.split("/")[-1]).name
                records.append((page_index, filename, source_image_url))
            return records

        if work.meta_single_page and work.meta_single_page.original_image_url:
            source_image_url = work.meta_single_page.original_image_url
            filename = Path(source_image_url.split("/")[-1]).name
            records.append((0, filename, source_image_url))
        return records

    def _store_work_image_records(self, work: WorkDetail) -> int:
        """把作品图片页记录写入数据库，不下载文件。"""
        self.logger.info(f'[入库] 开始处理作品: pid={work.id}, title={work.title}, pages={work.page_count}')
        image_records = self._extract_work_image_records(work)
        if not image_records:
            self.logger.warning(f'[入库] 作品 {work.id} 没有可用图片链接')
            return 0

        success_count = 0
        for page_index, filename, source_image_url in image_records:
            if self.db.add_image(work.id, page_index, filename, source_image_url):
                success_count += 1
                self.logger.debug(
                    f'[入库] 已添加到数据库: pid={work.id}, page={page_index}, filename={filename}'
                )
            else:
                self.logger.warning(f'[入库] 添加到数据库失败: pid={work.id}, page={page_index}')

        self.logger.info(f'[入库] 作品 {work.id} 处理完成: {success_count}/{len(image_records)} 张图片已写入数据库')
        return success_count

    async def _fetch_and_store_with_client(self, pixiv: BetterPixiv, count: int) -> dict:
        """在单个 Pixiv 会话内拉取推荐作品并写入数据库。"""
        self.logger.info(f'开始拉取并写入 {count} 份作品')
        works = await self._fetch_recommended_works_with_client(pixiv, count)
        if not works:
            self.logger.warning('没有拉取到作品')
            return {
                'total_works': 0,
                'total_images': 0,
                'success_works': 0,
                'success_images': 0
            }

        # 写入数据库
        total_images = 0
        success_images = 0
        success_works = 0
        for work in works:
            image_records = self._extract_work_image_records(work)
            total_images += len(image_records)
            count = self._store_work_image_records(work)
            success_images += count
            if count > 0:
                success_works += 1

        stats = {
            'total_works': len(works),
            'total_images': total_images,
            'success_works': success_works,
            'success_images': success_images
        }

        self.logger.info(f'写入完成: {stats}')
        return stats

    async def fetch_and_store(self, count: int) -> dict:
        """
        拉取推荐作品并把图片链接写入数据库

        Args:
            count: 需要拉取的作品数量

        Returns:
            统计信息字典
        """
        self.logger.debug(f'[拉取] 当前配置 - proxy: {self.proxy}, mock_mode: {self.mock_mode}')
        async with self.create_pixiv_client() as pixiv:
            return await self._fetch_and_store_with_client(pixiv, count)

    async def bookmark_illust(self, illust_id: int) -> None:
        """为单次收藏请求创建独立 Pixiv 会话。"""
        async with self.create_pixiv_client() as pixiv:
            await pixiv.bookmark_illust(illust_id)

    def get_bookmark_retry_delay_seconds(self, attempts: int) -> int:
        """根据失败次数计算下一次收藏任务的退避时间。"""
        return min(60 * (2 ** attempts), 6 * 60 * 60)

    async def _process_bookmark_jobs_with_client(self, pixiv: BetterPixiv, batch_size: int | None = None) -> dict:
        """
        在单个 Pixiv 会话内批量处理收藏任务。
        """
        if batch_size is None:
            batch_size = self.counts_config.get('bookmark_batch_size', 10) if self.counts_config else 10

        jobs = self.db.get_pending_bookmark_jobs(batch_size)
        result = {
            'queued_jobs': len(jobs),
            'bookmarked_works': 0,
            'failed_jobs': 0,
        }
        if not jobs:
            self.logger.info('[收藏任务] 当前没有待执行收藏任务')
            return result

        self.logger.info(f'[收藏任务] 开始处理 {len(jobs)} 个收藏任务')

        for job in jobs:
            pid = job['pid']
            try:
                await pixiv.bookmark_illust(pid)
                self.db.mark_bookmark_job_done(pid)
                result['bookmarked_works'] += 1
                self.logger.info(f'[收藏任务] 收藏成功: pid={pid}')
            except Exception as e:
                delay_seconds = self.get_bookmark_retry_delay_seconds(job['attempts'])
                self.db.mark_bookmark_job_retry(pid, delay_seconds, str(e))
                result['failed_jobs'] += 1
                self.logger.warning(
                    f'[收藏任务] 收藏失败: pid={pid}, '
                    f'下次重试 {delay_seconds}s 后, error={e}'
                )

        self.logger.info(f'[收藏任务] 执行完成: {result}')
        return result

    async def process_bookmark_jobs(self, batch_size: int | None = None) -> dict:
        """批量处理收藏任务，为独立触发场景创建单独 Pixiv 会话。"""
        async with self.create_pixiv_client() as pixiv:
            return await self._process_bookmark_jobs_with_client(pixiv, batch_size)

    def get_wait_count(self) -> int:
        """
        获取待评分图片数量

        Returns:
            待评分图片数量
        """
        stats = self.db.get_stats()
        return stats.get('wait_count', 0)

    def cleanup_done_images(self, keep_count: int) -> int:
        """
        清理已评分图片（LRU 策略）

        保留最近 keep_count 张已评分图片，删除更旧的

        Args:
            keep_count: 保留的图片数量

        Returns:
            删除的图片数量
        """
        self.logger.info(f'开始清理已评分图片状态，保留最近 {keep_count} 张')

        # 获取需要删除的图片
        to_delete = self.db.get_images_to_cleanup(keep_count)

        if not to_delete:
            self.logger.info('没有需要清理的图片')
            return 0

        deleted_count = 0
        for image in to_delete:
            try:
                # 不再落盘图片，只切换数据库状态以压缩活跃评分集合。
                self.db.update_status(image['pid'], image['page_index'], 'deleted')
                deleted_count += 1
            except Exception as e:
                self.logger.error(f'更新图片状态失败: pid={image["pid"]}, page={image["page_index"]}, 错误: {e}')

        self.logger.info(f'清理完成，删除了 {deleted_count} 张图片')
        return deleted_count

    async def auto_maintenance(self, min_wait: int | None = None, max_done: int | None = None) -> dict:
        """
        自动维护任务

        确保待评分图片 >= min_wait，已评分图片 <= max_done

        Args:
            min_wait: 最小待评分图片数量
            max_done: 最大已评分图片数量

        Returns:
            维护结果统计
        """
        self.logger.info('[自动维护] 开始执行自动维护任务')
        if min_wait is None:
            min_wait = self.counts_config.get('min_wait', 20)
            self.logger.info(f'[自动维护] 默认或从配置文件加载 {min_wait=}')
        if max_done is None:
            max_done = self.counts_config.get('max_done', 100)
            self.logger.info(f'[自动维护] 默认或从配置文件加载 {max_done=}')
        result = {
            'fetched_works': 0,
            'fetched_images': 0,
            'deleted_images': 0,
            'bookmark_jobs': {
                'queued_jobs': 0,
                'bookmarked_works': 0,
                'failed_jobs': 0,
            }
        }

        # 根据 mock_mode 决定拉取数量
        fetch_count = 10 if self.mock_mode else self.counts_config.get('fetch_count', 50)
        self.logger.info(f'[自动维护] 当前模式: {"测试模式" if self.mock_mode else "生产模式"}，拉取数量: {fetch_count}')

        # 1. 确保待评分图片充足
        wait_count = self.get_wait_count()
        self.logger.info(f'[自动维护] 当前待评分图片: {wait_count} 张')

        if wait_count < min_wait:
            self.logger.info(f'[自动维护] 待评分图片不足，开始处理收藏任务并拉取 {fetch_count} 份作品')
            async with self.create_pixiv_client() as pixiv:
                result['bookmark_jobs'] = await self._process_bookmark_jobs_with_client(pixiv)
                fetch_result = await self._fetch_and_store_with_client(pixiv, fetch_count)
            result['fetched_works'] = fetch_result['success_works']
            result['fetched_images'] = fetch_result['success_images']
            self.logger.info(f'[自动维护] 拉取完成: {result["fetched_works"]} 份作品，{result["fetched_images"]} 张图片')
        else:
            self.logger.info(f'[自动维护] 待评分图片充足，跳过收藏任务处理和拉取')

        # 2. 清理已评分图片
        stats = self.db.get_stats()
        done_count = stats.get('done_count', 0)
        self.logger.info(f'[自动维护] 当前已评分图片: {done_count} 张')

        keep_count = self.counts_config.get('keep_count', 50)

        if done_count > max_done:
            self.logger.info(f'[自动维护] 已评分图片过多，开始清理（保留最近 {keep_count} 张）')
            deleted = self.cleanup_done_images(keep_count)
            result['deleted_images'] = deleted
            self.logger.info(f'[自动维护] 清理完成: 删除了 {deleted} 张图片')
        else:
            self.logger.info(f'[自动维护] 已评分图片数量合理，无需清理')

        self.logger.info(f'[自动维护] 维护任务完成: {result}')
        return result
