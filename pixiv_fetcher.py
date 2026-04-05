"""
Pixiv 作品拉取模块

从 Pixiv 获取推荐作品并下载到本地，添加到数据库
"""

import asyncio
import json
from pathlib import Path
from typing import Optional
import yaml

from better_pixiv import BetterPixiv, WorkDetail
from dataset_db import DatasetDB
from setup_logger import get_logger


class PixivFetcher:
    """Pixiv 作品拉取器"""

    def __init__(self,
                 refresh_token: str,
                 proxy: Optional[str] = None,
                 storage_path: Path = Path("./mock_r2/pixiv_dataset/judge_wait"),
                 db_path: str = "dataset.db",
                 r2_base_url: Optional[str] = None,
                 r2_path_prefix: str = "pixiv_dataset",
                 mock_mode: bool = True):
        """
        初始化拉取器

        Args:
            refresh_token: Pixiv refresh token
            proxy: 代理地址，如 'http://127.0.0.1:10809'
            storage_path: 图片存储目录
            db_path: 数据库路径
            r2_base_url: R2 bucket 的公开访问 URL
            r2_path_prefix: 图片在 R2 中的路径前缀
            mock_mode: 是否为 mock 模式（本地开发）
        """
        self.logger = get_logger('PixivFetcher')
        self.refresh_token = refresh_token
        self.proxy = proxy
        self.storage_path = Path(storage_path).expanduser()
        self.db = DatasetDB(db_path)
        self.r2_base_url = r2_base_url
        self.r2_path_prefix = r2_path_prefix
        self.mock_mode = mock_mode

        # 确保存储目录存在
        if not self.storage_path.exists():
            self.storage_path.mkdir(parents=True)
            self.logger.info(f'创建存储目录: {self.storage_path}')

        # 初始化 BetterPixiv
        self.pixiv = BetterPixiv(
            proxy=self.proxy,
            refresh_token=self.refresh_token,
            storge_path=self.storage_path,
            logger=self.logger
        )

    @classmethod
    def from_config(cls, config_path: str = "pixiv_config.yaml"):
        """
        从配置文件创建拉取器

        Args:
            config_path: 配置文件路径

        Returns:
            PixivFetcher 实例
        """
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # 判断是否为 mock 模式
        mock_mode = config.get('mock_mode', True)

        # 根据 mock 模式选择存储路径
        storage_config = config.get('storage_path', {})
        if isinstance(storage_config, dict):
            storage_base = storage_config.get('mock' if mock_mode else 'production')
        else:
            storage_base = storage_config

        storage_base = Path(storage_base).expanduser()
        storage_path = storage_base / 'judge_wait'

        # 获取 R2 配置
        r2_config = config.get('r2', {})

        # 根据 mock 模式选择 base_url
        if mock_mode:
            r2_base_url = r2_config.get('mock_base_url', 'http://localhost:8000/static/mock_r2')
        else:
            r2_base_url = r2_config.get('base_url')

        return cls(
            refresh_token=config['refresh_token'],
            proxy=config.get('proxy'),
            storage_path=storage_path,
            db_path=config.get('db_path', 'dataset.db'),
            r2_base_url=r2_base_url,
            r2_path_prefix=r2_config.get('path_prefix', 'pixiv_dataset'),
            mock_mode=mock_mode
        )

    def get_r2_url(self, filename: str, status: str = 'wait') -> Optional[str]:
        """
        生成图片的 R2 访问 URL

        Args:
            filename: 文件名，如 '12345678_p0.jpg'
            status: 图片状态（已废弃，所有图片都在同一目录）

        Returns:
            R2 URL，如果未配置 R2 则返回 None
        """
        if not self.r2_base_url:
            return None

        # 所有图片统一存储在 judge_wait 目录
        url = f"{self.r2_base_url.rstrip('/')}/{self.r2_path_prefix}/judge_wait/{filename}"
        return url

    async def fetch_recommended_works(self, count: int = 100) -> list[WorkDetail]:
        """
        拉取推荐作品

        Args:
            count: 需要拉取的作品数量

        Returns:
            作品详情列表
        """
        self.logger.info(f'[拉取] 开始拉取推荐作品，目标数量: {count}')
        self.logger.debug(f'[拉取] 当前配置 - proxy: {self.proxy}, mock_mode: {self.mock_mode}')
        works = []

        while len(works) < count:
            self.logger.debug(f'[拉取] 当前已拉取 {len(works)} 份，继续拉取...')
            batch = await self.pixiv.get_recommended_illusts()

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

        # 只返回需要的数量
        works = works[:count]
        self.logger.info(f'[拉取] 拉取完成，共 {len(works)} 份作品')
        return works

    async def download_and_save_work(self, work: WorkDetail) -> int:
        """
        下载作品的所有图片并保存到数据库

        Args:
            work: 作品详情

        Returns:
            成功添加到数据库的图片数量
        """
        self.logger.info(f'[下载] 开始处理作品: pid={work.id}, title={work.title}, pages={work.page_count}')
        self.logger.debug(f'[下载] 作品类型: {work.type}, 存储路径: {self.storage_path}')

        # 下载作品
        self.logger.debug(f'[下载] 调用 pixiv.download() 下载作品...')
        download_result = await self.pixiv.download([work], max_workers=3)

        self.logger.debug(f'[下载] 下载结果: success_units={len(download_result.success_units)}, failed_units={len(download_result.failed_units)}')

        if not download_result.success_units:
            self.logger.warning(f'[下载] 作品 {work.id} 下载失败')
            return 0

        # 获取下载成功的文件列表
        if isinstance(download_result.success_units[0], Path):
            # 单个作品下载成功，success_units 是文件路径列表
            downloaded_files = download_result.success_units
            self.logger.debug(f'[下载] 下载模式: 单作品，文件列表长度: {len(downloaded_files)}')
        else:
            # 批量下载，success_units 是 DownloadResult 列表
            downloaded_files = download_result.success_units[0].success_units
            self.logger.debug(f'[下载] 下载模式: 批量，文件列表长度: {len(downloaded_files)}')

        # 添加到数据库
        success_count = 0
        for idx, file_path in enumerate(downloaded_files):
            if not isinstance(file_path, Path):
                self.logger.warning(f'[下载] 跳过非 Path 对象: {file_path}')
                continue

            # 从文件名解析 page_index
            # 文件名格式: {pid}_p{page_index}.{ext}
            filename = file_path.name
            self.logger.debug(f'[下载] 处理文件 {idx+1}/{len(downloaded_files)}: {filename}')

            try:
                # 提取 page_index
                parts = filename.split('_p')
                if len(parts) == 2:
                    page_index = int(parts[1].split('.')[0])
                else:
                    page_index = 0
                self.logger.debug(f'[下载] 解析 page_index: {page_index}')
            except (ValueError, IndexError) as e:
                self.logger.warning(f'[下载] 无法解析文件名: {filename}，使用 page_index=0, 错误: {e}')
                page_index = 0

            # 添加到数据库
            if self.db.add_image(work.id, page_index, filename):
                success_count += 1
                self.logger.debug(f'[下载] 已添加到数据库: pid={work.id}, page={page_index}, filename={filename}')
            else:
                self.logger.warning(f'[下载] 添加到数据库失败: pid={work.id}, page={page_index}')

        self.logger.info(f'[下载] 作品 {work.id} 处理完成: {success_count}/{work.page_count} 张图片已添加到数据库')
        return success_count

    async def fetch_and_download(self, count: int = 100) -> dict:
        """
        拉取并下载推荐作品

        Args:
            count: 需要拉取的作品数量

        Returns:
            统计信息字典
        """
        self.logger.info(f'开始拉取并下载 {count} 份作品')

        # 拉取推荐作品
        works = await self.fetch_recommended_works(count)

        if not works:
            self.logger.warning('没有拉取到作品')
            return {
                'total_works': 0,
                'total_images': 0,
                'success_works': 0,
                'success_images': 0
            }

        # 下载并保存
        total_images = 0
        success_images = 0
        success_works = 0

        for work in works:
            total_images += work.page_count
            count = await self.download_and_save_work(work)
            success_images += count
            if count > 0:
                success_works += 1

        stats = {
            'total_works': len(works),
            'total_images': total_images,
            'success_works': success_works,
            'success_images': success_images
        }

        self.logger.info(f'拉取完成: {stats}')
        return stats

    def get_wait_count(self) -> int:
        """
        获取待评分图片数量

        Returns:
            待评分图片数量
        """
        stats = self.db.get_stats()
        return stats.get('wait_count', 0)

    async def maintain_wait_queue(self, min_count: int = 100, target_count: int = 150):
        """
        维护待评分队列

        如果待评分图片少于 min_count，则拉取作品直到达到 target_count

        Args:
            min_count: 最小图片数量
            target_count: 目标图片数量
        """
        wait_count = self.get_wait_count()
        self.logger.info(f'当前待评分图片数量: {wait_count}')

        if wait_count >= min_count:
            self.logger.info(f'待评分图片充足，无需拉取')
            return

        # 计算需要拉取的作品数量（估算）
        # 假设平均每个作品 1.5 张图片
        needed_images = target_count - wait_count
        needed_works = int(needed_images / 1.5) + 10  # 多拉取一些以确保达到目标

        self.logger.info(f'需要拉取约 {needed_works} 份作品以达到目标 {target_count} 张图片')

        await self.fetch_and_download(needed_works)

        # 检查结果
        new_wait_count = self.get_wait_count()
        self.logger.info(f'拉取后待评分图片数量: {new_wait_count}')

    def cleanup_done_images(self, keep_count: int = 100) -> int:
        """
        清理已评分图片（LRU 策略）

        保留最近 keep_count 张已评分图片，删除更旧的

        Args:
            keep_count: 保留的图片数量

        Returns:
            删除的图片数量
        """
        self.logger.info(f'开始清理已评分图片，保留最近 {keep_count} 张')

        # 获取需要删除的图片
        to_delete = self.db.get_images_to_cleanup(keep_count)

        if not to_delete:
            self.logger.info('没有需要清理的图片')
            return 0

        deleted_count = 0
        for image in to_delete:
            # 构建文件路径
            file_path = self.storage_path / image['local_filename']

            # 删除文件
            try:
                if file_path.exists():
                    file_path.unlink()
                    self.logger.debug(f'已删除文件: {file_path.name}')
                else:
                    self.logger.warning(f'文件不存在: {file_path.name}')

                # 更新数据库状态
                self.db.update_status(image['pid'], image['page_index'], 'deleted')
                deleted_count += 1
            except Exception as e:
                self.logger.error(f'删除文件失败: {file_path.name}, 错误: {e}')

        self.logger.info(f'清理完成，删除了 {deleted_count} 张图片')
        return deleted_count

    async def auto_maintenance(self, min_wait: int = 150, max_done: int = 100) -> dict:
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

        result = {
            'fetched_works': 0,
            'fetched_images': 0,
            'deleted_images': 0
        }

        # 根据 mock_mode 决定拉取数量
        fetch_count = 10 if self.mock_mode else 200
        self.logger.info(f'[自动维护] 当前模式: {"测试模式" if self.mock_mode else "生产模式"}，拉取数量: {fetch_count}')

        # 1. 确保待评分图片充足
        wait_count = self.get_wait_count()
        self.logger.info(f'[自动维护] 当前待评分图片: {wait_count} 张')

        if wait_count < min_wait:
            self.logger.info(f'[自动维护] 待评分图片不足，开始拉取 {fetch_count} 份作品')
            fetch_result = await self.fetch_and_download(fetch_count)
            result['fetched_works'] = fetch_result['success_works']
            result['fetched_images'] = fetch_result['success_images']
            self.logger.info(f'[自动维护] 拉取完成: {result["fetched_works"]} 份作品，{result["fetched_images"]} 张图片')
        else:
            self.logger.info(f'[自动维护] 待评分图片充足，无需拉取')

        # 2. 清理已评分图片
        stats = self.db.get_stats()
        done_count = stats.get('done_count', 0)
        self.logger.info(f'[自动维护] 当前已评分图片: {done_count} 张')

        if done_count > max_done:
            self.logger.info(f'[自动维护] 已评分图片过多，开始清理（保留最近 {max_done} 张）')
            deleted = self.cleanup_done_images(max_done)
            result['deleted_images'] = deleted
            self.logger.info(f'[自动维护] 清理完成: 删除了 {deleted} 张图片')
        else:
            self.logger.info(f'[自动维护] 已评分图片数量合理，无需清理')

        self.logger.info(f'[自动维护] 维护任务完成: {result}')
        return result


# 测试代码
if __name__ == "__main__":
    async def test():
        # 从配置文件创建拉取器
        try:
            fetcher = PixivFetcher.from_config()
        except FileNotFoundError:
            # 如果配置文件不存在，使用硬编码配置（仅用于测试）
            print("配置文件不存在，使用测试配置")
            fetcher = PixivFetcher(
                refresh_token='a4TF-gC5kRkciAiZ5MhGUoVw6zb3AXO1M1DmnAeFGlk',
                proxy='http://127.0.0.1:10809',
                storage_path=Path('judge_wait'),
                db_path='dataset.db'
            )

        # 测试拉取 5 份作品
        print("\n=== 测试拉取 5 份作品 ===")
        stats = await fetcher.fetch_and_download(5)
        print(f"拉取结果: {stats}")

        # 测试维护队列
        print("\n=== 测试维护队列 ===")
        await fetcher.maintain_wait_queue(min_count=10, target_count=20)

        # 查看数据库统计
        print("\n=== 数据库统计 ===")
        db_stats = fetcher.db.get_stats()
        print(f"总图片数: {db_stats['total_images']}")
        print(f"总作品数: {db_stats['total_works']}")
        print(f"待评分: {db_stats['wait_count']}")

    asyncio.run(test())
