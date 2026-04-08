import asyncio
import functools
import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Awaitable, Callable, Union

import natsort
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from pixivpy_async import *
from tqdm import tqdm


class User(BaseModel):
    id: int
    name: str
    account: str | None = None
    profile_image_urls: list[str] | None = None
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


class ClientWrapper:
    def __init__(self, pixiv: "BetterPixiv"):
        self.pixiv = pixiv
        self.refresh_token: str = pixiv.refresh_token
        self.access_token: str | None = pixiv.access_token
        self.proxy: str | None = pixiv.proxy
        self.bypass: bool = pixiv.bypass

    async def __aenter__(self):
        if self.pixiv.api:
            return self.pixiv.api
        self.client = PixivClient(proxy=self.proxy, bypass=self.bypass)
        aapi = AppPixivAPI(client=self.client.start())
        if self.access_token is None:
            self.pixiv.logger.debug('无access token, 正在刷新')
            access_json: dict = await aapi.login(refresh_token=self.refresh_token)
            access_token = access_json.get('access_token', None)
            if not access_json:
                raise PixivError(f'无法刷新token: 返回{access_json}')
            self.access_token = access_token
            self.pixiv.logger.debug(f'更新到at: {self.access_token}')
            self.pixiv.access_token = self.access_token
            self.pixiv.logger.debug(f'刷新到上层BetterPixiv实例的at: {self.pixiv.access_token}')
            aapi.set_auth(refresh_token=self.refresh_token, access_token=self.access_token)
        else:
            self.pixiv.logger.debug(f'获取到上层BetterPixiv实例的at: {self.access_token}')
            aapi.set_auth(refresh_token=self.refresh_token, access_token=self.access_token)
        self.pixiv.api = self
        return aapi

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.pixiv.api = None
        await self.client.close()


class BetterPixiv:
    def __init__(self,
                 proxy: str | None = None,
                 refresh_token: str | None = None,
                 storge_path: Path | None = None,
                 bypass: bool = False,
                 logger: logging.Logger | None = None,
                 debug: bool = False):
        if refresh_token is None:
            raise PixivError('refresh_token is required')
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.proxy = proxy
        self.bypass = bypass
        self.storge_path: Path = Path(os.path.curdir) if storge_path is None else storge_path
        self.api: ClientWrapper | None = None
        self.viewed: list[int] | None = None
        if not logger:
            try:
                from .setup_logger import get_logger
            except ImportError:
                from setup_logger import get_logger
            self.logger = get_logger('pixiv', debug=debug)
        else:
            self.logger = logger

    @staticmethod
    def retry_on_error(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except PixivError:
                # 获取 self（第一个参数）
                self_arg = args[0] if args else None
                if self_arg and hasattr(self_arg, 'access_token'):
                    self_arg.access_token = None
                return await func(*args, **kwargs)

        return wrapper

    async def token_refresh(self) -> str:
        self.access_token = None
        async with ClientWrapper(self):
            assert self.access_token is not None
            return self.access_token

    def set_storge_path(self, path: Path):
        if path.is_absolute():
            self.storge_path = path
        else:
            self.storge_path = Path(os.path.curdir) / path
        if not self.storge_path.exists():
            try:
                self.storge_path.mkdir(parents=True)
                self.logger.info('未检测到设置的下载目录，已创建')
            except OSError as e:
                self.storge_path = Path(os.path.curdir)
                self.logger.warning('目录创建失败，将使用默认目录', e)

    async def _download_single_file(
            self,
            api: AppPixivAPI,
            sem: asyncio.Semaphore,
            url: str,
            file_downloaded_callback: Callable[[str, bool], None] | None = None,
    ) -> Path:
        async with sem:
            filename = Path(url.split('/')[-1])
            file_path = self.storge_path / filename
            for retry_times in range(10):
                try:
                    file_result = True
                    if not file_path.exists():
                        file_result = await api.download(url, path=str(self.storge_path))
                    if file_downloaded_callback:
                        file_downloaded_callback(url, file_result)
                    return file_path
                except Exception as dl_e:
                    self.logger.warning(f'下载{os.path.basename(url)} 异常: {dl_e}, 重试第{retry_times}次')
                    await asyncio.sleep(1)
            else:
                raise PixivError(f"Download failed after retries: {url}")

    async def _download_single_work(
            self,
            api: AppPixivAPI,
            work_details: WorkDetail,
            sem: asyncio.Semaphore,
            phase_callback: Callable[[int, str], None] | None = None,
    ) -> DownloadResult:
        download_result = DownloadResult()
        if work_details.type not in ("illust", "ugoira"):
            download_result.extra_info = 'work不是illust或ugoria'
            return download_result
        if work_details.type == 'illust':
            work_url_list: list[str] = []
            if work_details.meta_pages:
                work_url_list = [mp.image_urls.original for mp in work_details.meta_pages]
            elif work_details.meta_single_page:
                work_url_list.append(work_details.meta_single_page.original_image_url)  # type: ignore
            download_result.total = len(work_url_list)

            def _phase_callback(single_url: str, task_result: bool):
                if task_result:
                    download_result.success_units.append(self.storge_path / Path(os.path.basename(single_url)))
                else:
                    download_result.failed_units.append(single_url)
                if phase_callback:
                    phase_callback(work_details.id, single_url)

            tasks = [self._download_single_file(api, sem, url, _phase_callback) for url in work_url_list]
            await asyncio.gather(*tasks)
        else:
            download_result.total = 1
            ugoira_metadata = await api.ugoira_metadata(work_details.id)
            zip_url = ugoira_metadata['ugoira_metadata']['zip_urls']['medium']
            filename = Path(zip_url.split('/')[-1])
            zip_path = self.storge_path / filename
            try:
                if not zip_path.exists():
                    if not await self._download_single_file(api, sem, zip_url):
                        download_result.failed_units.append(zip_url)
                        download_result.extra_info = f'Error in downloading {zip_url}'
                        return download_result
            except KeyboardInterrupt:
                raise KeyboardInterrupt
            except Exception as e:
                self.logger.warning(e)
                download_result.extra_info = str(e)
                download_result.failed_units.append(zip_url)
                return download_result
            with zipfile.ZipFile(zip_path, 'r') as zip_file:
                image_files = [f for f in zip_file.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
                image_files = natsort.natsorted(image_files)
                images: list[Image.Image] = []
                for image_file in image_files:
                    with zip_file.open(image_file) as image_data:
                        images.append(Image.open(io.BytesIO(image_data.read())))
            if not images:
                download_result.failed_units.append(zip_url)
                download_result.extra_info = 'No images found in the ZIP file'
                return download_result
            gif_path = self.storge_path / Path(f'{filename}.gif')
            images[0].save(
                gif_path,
                save_all=True,
                append_images=images[1:],
                duration=100,
                loop=1,
            )
            zip_path.unlink()
            download_result.success_units.append(gif_path)
        return download_result

    async def download(
            self,
            work_ids: list[WorkDetail] | WorkDetail,
            max_workers: int = 3,
            phase_callback: Callable[[int, str], None] | None = None,
    ) -> DownloadResult:
        if not isinstance(work_ids, list):
            work_ids = [work_ids]
        download_result = DownloadResult()
        if len(work_ids) == 0:
            return download_result
        semaphore = asyncio.Semaphore(max_workers)
        download_result.total = len(work_ids)
        self.logger.info(f'启动下载任务, 目标ID数: {len(work_ids)}, 最大并发: {max_workers}')
        pbar_works: tqdm | None = None
        if phase_callback is None:
            pbar_works = tqdm(total=len(work_ids), desc="[作品进度]", position=0, leave=True, colour='green')

        def on_file_downloaded(work_id: int, url: str):
            if phase_callback:
                phase_callback(work_id, url)

        async with ClientWrapper(self) as api:
            async def work_task_wrapper(wid: WorkDetail) -> DownloadResult:
                res = await self._download_single_work(
                    api,
                    wid,
                    semaphore,
                    phase_callback=on_file_downloaded,
                )
                if pbar_works:
                    pbar_works.update(1)
                return res

            task_results: list[DownloadResult] = await asyncio.gather(
                *[work_task_wrapper(work_id) for work_id in work_ids]
            )
            if pbar_works:
                pbar_works.close()

            for task_result in task_results:
                if task_result.total > 0 and task_result.total == len(task_result.success_units):
                    download_result.success_units.append(task_result)
                else:
                    download_result.failed_units.append(task_result)
            return download_result

    @retry_on_error
    async def get_work_details(self, work_id: int) -> WorkDetail | None:
        async with ClientWrapper(self) as api:
            self.logger.debug(f'正在获取作品详情: {work_id}')
            work_details_json: dict = await api.illust_detail(work_id)
            self.logger.debug(f'底层返回: {work_details_json}')
            if isinstance(work_details_json, str):
                raise PixivError(work_details_json)
            if work_details_json.get('error'):
                if work_details_json['error']['user_message'] == 'ページが見つかりませんでした':
                    return None
                raise PixivError(work_details_json)
            illust_detail_json = work_details_json['illust']
            return WorkDetail.model_validate(illust_detail_json)

    @retry_on_error
    async def get_user_works(
            self,
            user_id: int,
            max_page_cnt: int = 0,
            hook_func: Callable[[list[WorkDetail], int], Awaitable[bool]] | None = None,
    ) -> list[WorkDetail]:
        user_work_list: list[WorkDetail] = []
        now_page = 1
        work_offset: int | None = None
        try:
            async with ClientWrapper(self) as api:
                while True:
                    works: dict = await api.user_illusts(user_id, offset=work_offset)
                    is_continue = True
                    segment_works = [WorkDetail.model_validate(fav_work) for fav_work in works['illusts']]
                    user_work_list += segment_works
                    if hook_func:
                        is_continue = await hook_func(segment_works, now_page)
                    if not is_continue:
                        raise KeyError
                    next_url: str = works['next_url']
                    if not next_url:
                        return user_work_list
                    index = next_url.find('offset=') + len('offset=')
                    work_offset = int(next_url[index:])
                    self.logger.debug(f'作品翻页中, {next_url=}')
                    await asyncio.sleep(0.5)
                    if max_page_cnt and now_page >= max_page_cnt:
                        raise KeyError
                    now_page += 1
        except KeyError:
            return user_work_list

    @retry_on_error
    async def get_favs(
            self,
            user_id: int,
            max_page_cnt: int = 0,
            hook_func: Callable[[list[WorkDetail], int], Awaitable[bool]] | None = None,
    ) -> list[WorkDetail]:
        fav_list: list[WorkDetail] = []
        now_page = 1
        max_mark: str | None = None
        try:
            async with ClientWrapper(self) as api:
                while True:
                    favs: dict = await api.user_bookmarks_illust(
                        user_id, max_bookmark_id=int(max_mark) if max_mark else None
                    )
                    segment_favs = [WorkDetail.model_validate(fav_work) for fav_work in favs['illusts']]
                    fav_list += segment_favs
                    await asyncio.sleep(0.5)
                    is_continue = True
                    if hook_func:
                        is_continue = await hook_func(segment_favs, now_page)
                    if not is_continue:
                        raise KeyError
                    next_url: str = favs['next_url']
                    if not next_url:
                        return fav_list
                    self.logger.debug(f'收藏翻页中, {next_url=}')
                    index = next_url.find('max_bookmark_id=') + len('max_bookmark_id=')
                    max_mark = next_url[index:]
                    if max_page_cnt and now_page >= max_page_cnt:
                        raise KeyError
                    now_page += 1
        except KeyError:
            return fav_list

    @retry_on_error
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

    @retry_on_error
    async def get_ranking(self, tag_filter: str = 'day_male') -> list[dict]:
        async with ClientWrapper(self) as api:
            rank_json = await api.illust_ranking(tag_filter)
            return rank_json.get('illusts', [])

    async def get_recommended_illusts(self, viewed: list[int] | None = None) -> list[WorkDetail]:
        if viewed is None:
            viewed = self.viewed
        async with ClientWrapper(self) as api:
            resp = await api.illust_recommended(content_type='illust', viewed=viewed)
            recommended_illusts = [WorkDetail.model_validate(work) for work in resp.get('illusts', [])]
            self.viewed = [work.id for work in recommended_illusts]
            return recommended_illusts

    @retry_on_error
    async def search_works(
            self,
            word: str,
            match_type: str = 'part',
            sort: str = 'date_desc',
            time_dist: str = 'month',
            start_date: str | None = None,
            end_date: str | None = None,
            min_marks: int | None = None,
            offset: int | None = None,
    ) -> dict:
        if offset == 0:
            offset = None
        if match_type == 'content':
            search_target = "title_and_caption"
        elif match_type == 'all':
            search_target = "exact_match_for_tags"
        else:
            search_target = "partial_match_for_tags"
        if start_date:
            pass
        if end_date:
            pass
        if time_dist == 'day':
            duration = 'within_last_day'
        elif time_dist == 'week':
            duration = 'within_last_week'
        else:
            duration = 'within_last_month'
        async with ClientWrapper(self) as api:
            return await api.search_illust(
                word,
                search_target,
                sort,
                duration,
                min_bookmarks=min_marks,
                offset=offset,
            )

    async def bookmark_illust(self, illust_id: int):
        async with ClientWrapper(self) as api:
            return await api.illust_bookmark_add(illust_id)


if __name__ == '__main__':
    async def test():
        bp = BetterPixiv(proxy='http://127.0.0.1:10809', refresh_token='a4TF-gC5kRkciAiZ5MhGUoVw6zb3AXO1M1DmnAeFGlk')
        print(await bp.bookmark_illust(134140980))

    asyncio.run(test())
