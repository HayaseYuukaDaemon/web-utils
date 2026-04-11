import asyncio
import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Awaitable, Callable, Union

import natsort
from PIL import Image
from pixivpy_async import AppPixivAPI, PixivClient
from pydantic import BaseModel, ConfigDict, Field
from rich.progress import Progress, TaskID, TextColumn, BarColumn, TimeRemainingColumn


class User(BaseModel):
    id: int
    name: str
    account: str | None = None
    profile_image_urls: dict | None = None
    is_followed: bool | None = None
    is_accept_request: bool | None = None


class Tag(BaseModel):
    name: str
    translated_name: str | None = None


class MetaSinglePage(BaseModel):
    original_image_url: str | None = None


class MetaPageImageUrls(BaseModel):
    original: str
    square_medium: str | None = None
    medium: str | None = None
    large: str | None = None


class MetaPage(BaseModel):
    image_urls: MetaPageImageUrls


class WorkDetail(BaseModel):
    model_config = ConfigDict(extra='ignore')

    id: int
    title: str
    type: str
    caption: str
    user: User
    tags: list[Tag]
    create_date: str
    page_count: int
    width: int
    height: int
    total_view: int
    total_bookmarks: int
    meta_single_page: MetaSinglePage | None = None
    meta_pages: list[MetaPage] | None = None
    sanity_level: int | None = None
    x_restrict: int | None = None
    restrict: int | None = None
    is_bookmarked: bool | None = None
    visible: bool | None = None
    is_muted: bool | None = None
    total_comments: int | None = None
    illust_ai_type: int | None = None
    illust_book_style: int | None = None
    comment_access_control: int | None = None
    restriction_attributes: list[str] | None = None


class DownloadResult(BaseModel):
    task_id: int = 0
    total: int = 0
    extra_info: str | None = None
    failed_units: list[Union[str, "DownloadResult"]] = Field(default_factory=list)
    success_units: list[Union[Path, "DownloadResult"]] = Field(default_factory=list)


class PixivError(Exception):
    def __init__(self, message: str = ''):
        self.message = message
        super().__init__(self.message)


class IllustNotFoundError(PixivError):
    pass


class BetterPixiv:
    """
    Pixiv API 封装，支持 async 上下文管理器用法。

    用法:
        async with BetterPixiv(refresh_token='...') as bp:
            work = await bp.get_work_details(123)
            await bp.download(work)
    """

    def __init__(
            self,
            proxy: str | None = None,
            refresh_token: str | None = None,
            storage_path: Path | None = None,
            bypass: bool = False,
            logger: logging.Logger | None = None,
            debug: bool = False,
    ):
        if refresh_token is None:
            raise PixivError('refresh_token is required')
        self.refresh_token = refresh_token
        self.proxy = proxy
        self.bypass = bypass
        self.storage_path: Path = Path(os.path.curdir) if storage_path is None else storage_path
        self.viewed: list[int] | None = None

        if logger is not None:
            self.logger = logger
        else:
            try:
                from .setup_logger import get_logger  # type: ignore[import-not-found]
            except ImportError:
                from setup_logger import get_logger
            self.logger = get_logger('pixiv', debug=debug)

        self.api: AppPixivAPI | None = None
        self._client: PixivClient | None = None

    async def __aenter__(self) -> "BetterPixiv":
        """每次进入时创建新连接并刷新 access token。"""
        self._client = PixivClient(proxy=self.proxy, bypass=self.bypass)
        aapi = AppPixivAPI(client=self._client.start())
        access_json: dict = await aapi.login(refresh_token=self.refresh_token)
        access_token = access_json.get('access_token')
        if not access_token:
            raise PixivError(f'无法刷新 token: 返回 {access_json}')
        aapi.set_auth(refresh_token=self.refresh_token, access_token=access_token)
        self.api = aapi
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.close()
        self.api = None
        self._client = None

    async def _download_file(
            self,
            sem: asyncio.Semaphore,
            url: str,
            progress: Progress | None = None,
            image_task_id: TaskID | None = None,
    ) -> Path:
        async with sem:
            assert self.api
            filename = Path(url.split('/')[-1])
            file_path = self.storage_path / filename
            if progress and image_task_id is not None:
                progress.update(image_task_id, description=f'↓ {filename}')

            for retry_times in range(10):
                try:
                    if not file_path.exists():
                        await self.api.download(url, path=str(self.storage_path))
                    if progress and image_task_id is not None:
                        progress.advance(image_task_id)
                    return file_path
                except Exception as dl_e:
                    self.logger.warning(
                        f'下载 {os.path.basename(url)} 异常: {dl_e}, '
                        f'重试第 {retry_times} 次'
                    )
                    await asyncio.sleep(1)
            else:
                if progress and image_task_id is not None:
                    progress.update(image_task_id, description=f'✗ {filename}')
                raise PixivError(f"Download failed after retries: {url}")

    async def _download_single_work(
            self,
            work: WorkDetail,
            sem: asyncio.Semaphore,
            progress: Progress | None = None,
            work_task_id: TaskID | None = None,
    ) -> tuple[DownloadResult, TaskID | None]:
        """下载单个作品，返回 (结果, 图片层 task_id 或 None)。

        图片层 task 由本方法内部创建和管理Caller 负责在完成后移除图片层 task。
        """
        assert self.api
        result = DownloadResult()
        image_task_id: TaskID | None = None

        if work.type not in ("illust", "ugoira"):
            result.extra_info = 'work 不是 illust 或 ugoira'
            return result, image_task_id

        if work.type == 'illust':
            work_url_list: list[str] = []
            if work.meta_pages:
                work_url_list = [mp.image_urls.original for mp in work.meta_pages]
            elif work.meta_single_page:
                work_url_list.append(work.meta_single_page.original_image_url)  # type: ignore
            result.total = len(work_url_list)

            if progress and work_task_id is not None:
                title_short = work.title[:15] + '...' if len(work.title) > 15 else work.title
                image_task_id = progress.add_task(
                    title_short,
                    total=len(work_url_list),
                    completed=0,
                )

                async def download_one(url: str) -> tuple[str, Path | None]:
                    try:
                        return url, await self._download_file(sem, url, progress, image_task_id)
                    except PixivError:
                        return url, None

                results = await asyncio.gather(*[download_one(url) for url in work_url_list])
                for url, path in results:
                    if path:
                        result.success_units.append(path)
                    else:
                        result.failed_units.append(url)
            else:
                tasks = [
                    self._download_file(sem, url)
                    for url in work_url_list
                ]
                await asyncio.gather(*tasks)
        else:
            # ugoira: 单次下载，不需要图片层进度
            result.total = 1
            metadata = await self.api.ugoira_metadata(work.id)
            zip_url = metadata['ugoira_metadata']['zip_urls']['medium']
            filename = Path(zip_url.split('/')[-1])
            zip_path = self.storage_path / filename
            try:
                if not zip_path.exists():
                    await self._download_file(sem, zip_url)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.logger.warning(e)
                result.extra_info = str(e)
                result.failed_units.append(zip_url)
                return result, image_task_id
            with zipfile.ZipFile(zip_path, 'r') as zf:
                image_files = natsort.natsorted([
                    f for f in zf.namelist()
                    if f.lower().endswith(('.png', '.jpg', '.jpeg'))
                ])
                images: list[Image.Image] = [
                    Image.open(io.BytesIO(zf.read(f))) for f in image_files
                ]
            if not images:
                result.failed_units.append(zip_url)
                result.extra_info = 'No images found in the ZIP file'
                return result, image_task_id
            gif_path = self.storage_path / Path(f'{filename}.gif')
            images[0].save(
                gif_path,
                save_all=True,
                append_images=images[1:],
                duration=100,
                loop=1,
            )
            zip_path.unlink()
            result.success_units.append(gif_path)

        return result, image_task_id

    async def download(
            self,
            works: list[WorkDetail] | WorkDetail,
            max_workers: int = 3,
    ) -> DownloadResult:
        if not isinstance(works, list):
            works = [works]
        download_result = DownloadResult()
        if not works:
            return download_result
        semaphore = asyncio.Semaphore(max_workers)
        download_result.total = len(works)
        self.logger.info(f'启动下载任务, 目标 ID 数: {len(works)}, 最大并发: {max_workers}')
        self.storage_path.mkdir(parents=True, exist_ok=True)

        with Progress(
            TextColumn('[bold cyan]作品'),
            BarColumn(bar_width=20),
            TextColumn('[progress.percentage]{task.percentage:>3.0f}%'),
            TextColumn('[{task.completed}/{task.total}]'),
            TimeRemainingColumn(),
        ) as progress:
            work_task_id = progress.add_task(
                '[green]作品进度',
                total=len(works),
                completed=0,
            )

            async def run_work(wid: WorkDetail) -> DownloadResult:
                res, img_task_id = await self._download_single_work(
                    wid, semaphore, progress, work_task_id
                )
                if img_task_id is not None:
                    progress.remove_task(img_task_id)
                progress.update(work_task_id, advance=1)
                return res

            task_results: list[DownloadResult] = await asyncio.gather(
                *[run_work(wid) for wid in works]
            )

        for task_result in task_results:
            if task_result.total > 0 and task_result.total == len(task_result.success_units):
                download_result.success_units.append(task_result)
            else:
                download_result.failed_units.append(task_result)
        return download_result

    async def get_work_details(self, work_id: int) -> WorkDetail | None:
        assert self.api
        self.logger.debug(f'正在获取作品详情: {work_id}')
        resp: dict = await self.api.illust_detail(work_id)
        self.logger.debug(f'底层返回: {resp}')
        if isinstance(resp, str):
            raise PixivError(resp)
        if resp.get('error'):
            if resp['error']['user_message'] == 'ページが見つかりませんでした':
                return None
            raise PixivError(str(resp))
        return WorkDetail.model_validate(resp['illust'])

    async def get_user_works(
            self,
            user_id: int,
            max_page_cnt: int = 0,
            hook_func: Callable[[list[WorkDetail], int], Awaitable[bool]] | None = None,
    ) -> list[WorkDetail]:
        assert self.api
        user_work_list: list[WorkDetail] = []
        now_page = 1
        work_offset: int | None = None
        try:
            while True:
                works: dict = await self.api.user_illusts(user_id, offset=work_offset)
                is_continue = True
                segment_works = [WorkDetail.model_validate(w) for w in works['illusts']]
                user_work_list += segment_works
                if hook_func:
                    is_continue = await hook_func(segment_works, now_page)
                if not is_continue:
                    raise KeyError
                next_url: str = works['next_url']
                if not next_url:
                    return user_work_list
                idx = next_url.find('offset=') + len('offset=')
                work_offset = int(next_url[idx:])
                self.logger.debug(f'作品翻页中, {next_url=}')
                await asyncio.sleep(0.5)
                if max_page_cnt and now_page >= max_page_cnt:
                    raise KeyError
                now_page += 1
        except KeyError:
            return user_work_list

    async def get_favs(
            self,
            user_id: int,
            max_page_cnt: int = 0,
            hook_func: Callable[[list[WorkDetail], int], Awaitable[bool]] | None = None,
    ) -> list[WorkDetail]:
        assert self.api
        fav_list: list[WorkDetail] = []
        now_page = 1
        max_mark: str | None = None
        try:
            while True:
                favs: dict = await self.api.user_bookmarks_illust(
                    user_id, max_bookmark_id=int(max_mark) if max_mark else None
                )
                segment_favs = [WorkDetail.model_validate(f) for f in favs['illusts']]
                fav_list += segment_favs
                is_continue = True
                if hook_func:
                    is_continue = await hook_func(segment_favs, now_page)
                if not is_continue:
                    raise KeyError
                next_url: str = favs['next_url']
                if not next_url:
                    return fav_list
                self.logger.debug(f'收藏翻页中, {next_url=}')
                idx = next_url.find('max_bookmark_id=') + len('max_bookmark_id=')
                max_mark = next_url[idx:]
                await asyncio.sleep(0.5)
                if max_page_cnt and now_page >= max_page_cnt:
                    raise KeyError
                now_page += 1
        except KeyError:
            return fav_list

    async def get_new_works(self, user_id: int, id_anchor: int) -> list[WorkDetail]:
        new_works: list[WorkDetail] = []

        async def work_hook(work_details: list[WorkDetail], _page: int) -> bool:
            for work_detail in work_details:
                if work_detail.id > id_anchor:
                    new_works.append(work_detail)
                else:
                    return False
            return True

        await self.get_user_works(user_id, hook_func=work_hook)
        return new_works

    async def get_ranking(self, tag_filter: str = 'day_male') -> list[dict]:
        assert self.api
        rank_json: dict = await self.api.illust_ranking(tag_filter)
        return rank_json.get('illusts', [])

    async def get_recommended_illusts(self, viewed: list[int] | None = None) -> list[WorkDetail]:
        assert self.api
        if viewed is None:
            viewed = self.viewed
        resp: dict = await self.api.illust_recommended(content_type='illust', viewed=viewed)
        recommended = [WorkDetail.model_validate(w) for w in resp.get('illusts', [])]
        self.viewed = [w.id for w in recommended]
        return recommended

    async def search_works(
            self,
            word: str,
            match_type: str = 'part',
            sort: str = 'date_desc',
            time_dist: str = 'month',
            min_marks: int | None = None,
            offset: int | None = None,
    ) -> dict:
        assert self.api
        if offset == 0:
            offset = None
        if match_type == 'content':
            search_target = "title_and_caption"
        elif match_type == 'all':
            search_target = "exact_match_for_tags"
        else:
            search_target = "partial_match_for_tags"
        if time_dist == 'day':
            duration = 'within_last_day'
        elif time_dist == 'week':
            duration = 'within_last_week'
        else:
            duration = 'within_last_month'
        return await self.api.search_illust(
            word,
            search_target,
            sort,
            duration,
            min_bookmarks=min_marks,
            offset=offset,
        )

    async def bookmark_illust(self, illust_id: int):
        assert self.api
        return await self.api.illust_bookmark_add(illust_id)
    
    async def unbookmark_illust(self, illust_id: int):
        assert self.api
        return await self.api.illust_bookmark_delete(illust_id)


if __name__ == '__main__':
    import sys
    import io

    # 强制 Windows 控制台使用 UTF-8 输出
    if sys.platform == 'win32' and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    import time

    PROXY = 'http://127.0.0.1:10809'
    REFRESH_TOKEN = 'a4TF-gC5kRkciAiZ5MhGUoVw6zb3AXO1M1DmnAeFGlk'
    TEST_PID = 134140980
    STORAGE_PATH = Path('./mock_r2/pixiv_dataset')

    def hr(title: str):
        print(f'\n{"=" * 60}')
        print(f'  {title}')
        print(f'{"=" * 60}')

    async def test():
        test_user_id: int = 0
        t0 = time.time()

        async with BetterPixiv(
            proxy=PROXY,
            refresh_token=REFRESH_TOKEN,
            storage_path=STORAGE_PATH,
            debug=False,
        ) as bp:
            # ── 1. get_work_details ─────────────────────────────────────────────
            hr('1. get_work_details')
            work = await bp.get_work_details(TEST_PID)
            if work:
                print(f'  标题: {work.title}')
                print(f'  作者ID: {work.user.id}')
                print(f'  作者名: {work.user.name}')
                print(f'  类型: {work.type}')
                print(f'  标签数: {len(work.tags)}')
                print(f'  收藏数: {work.total_bookmarks}')
                print(f'  浏览数: {work.total_view}')
                print(f'  多图页数: {work.page_count}')
                if work.meta_pages:
                    print(f'  多图 URL 数: {len(work.meta_pages)}')
                    print(f'  第一张原图: {work.meta_pages[0].image_urls.original}')
                elif work.meta_single_page:
                    print(f'  单图 URL: {work.meta_single_page.original_image_url}')
                test_user_id = work.user.id
                print(f'  标签: {[t.name for t in work.tags[:5]]}')
            else:
                print(f'  作品 {TEST_PID} 未找到（可能已删除）')
                return

            # ── 2. download ─────────────────────────────────────────────────────
            hr('2. download')
            try:
                dl_result = await bp.download(work, max_workers=3)
            except Exception as e:
                print(f'  下载异常（可能是网络问题）: {e}')
                dl_result = None
            if dl_result:
                print(f'  总任务数: {dl_result.total}')
                print(f'  成功单元: {len(dl_result.success_units)}')
                print(f'  失败单元: {len(dl_result.failed_units)}')
                if dl_result.extra_info:
                    print(f'  附加信息: {dl_result.extra_info}')
                for f in dl_result.success_units[:5]:
                    print(f'    + {f}')

            # ── 3. get_user_works ────────────────────────────────────────────────
            hr('3. get_user_works')
            works = await bp.get_user_works(test_user_id, max_page_cnt=2)
            print(f'  共获取作品数: {len(works)}')
            if works:
                print(f'  第一条: [{works[0].id}] {works[0].title} by {works[0].user.name}')

            # ── 4. get_favs ─────────────────────────────────────────────────────
            hr('4. get_favs')
            favs = await bp.get_favs(test_user_id, max_page_cnt=1)
            print(f'  共获取收藏数: {len(favs)}')
            if favs:
                print(f'  第一条收藏: [{favs[0].id}] {favs[0].title}')

            # ── 5. get_ranking ──────────────────────────────────────────────────
            hr('5. get_ranking')
            ranking = await bp.get_ranking(tag_filter='day_male')
            print(f'  今日男性向榜单作品数: {len(ranking)}')
            if ranking:
                r0 = ranking[0]
                print(f'  第一名: [{r0.get("id")}] {r0.get("title")} '
                      f'by {r0.get("user", {}).get("name")}')

            # ── 6. get_recommended_illusts ──────────────────────────────────────
            hr('6. get_recommended_illusts')
            recs = await bp.get_recommended_illusts()
            print(f'  推荐作品数: {len(recs)}')
            if recs:
                print(f'  第一条: [{recs[0].id}] {recs[0].title}')

            # ── 7. search_works ────────────────────────────────────────────────
            hr('7. search_works')
            search_word = works[0].tags[0].name if works and works[0].tags else 'illustration'
            print(f'  搜索关键词: {search_word}')
            results = await bp.search_works(
                word=search_word,
                match_type='part',
                sort='date_desc',
                time_dist='month',
            )
            illusts = results.get('illusts', [])
            print(f'  搜索结果数: {len(illusts)}')
            if illusts:
                print(f'  第一条: [{illusts[0]["id"]}] {illusts[0]["title"]}')

            # ── 8. get_new_works ────────────────────────────────────────────────
            hr('8. get_new_works')
            if works:
                anchor = works[0].id
                new_works = await bp.get_new_works(test_user_id, id_anchor=anchor)
                print(f'  锚点 {anchor} 之后的新作品数: {len(new_works)}')

            # ── 9. bookmark_illust（按需取消注释） ───────────────────────────────
            # hr('9. bookmark_illust')
            # result = await bp.bookmark_illust(TEST_PID)
            # print(f'  收藏结果: {result}')

        hr('全部测试完成')
        print(f'  总耗时: {time.time() - t0:.1f}s')

    asyncio.run(test())
