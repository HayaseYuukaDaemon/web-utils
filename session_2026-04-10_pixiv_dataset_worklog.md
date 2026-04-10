# 2026-04-10 Pixiv Dataset Worklog

本文件记录本次会话中围绕 Pixiv 图片评分数据集系统所做的主要改动，并与对应 `git log` 提交保持对齐。

## 范围

本次会话的主线是把评分系统从“下载图片再访问”改为“只存 Pixiv 原图链接，请求时走反代”，并继续优化：

- 收藏任务的批处理时机
- 页面端图片预载
- 退出时冲刷待执行收藏队列
- 生产环境数据迁移与上线验证

## Git Log 对照

按时间顺序列出。

### `38d8475` `Refactor Pixiv dataset service and queue bookmarks`

这一提交是本轮工作的起点，主要完成了两件事：

- 把原先 `pixiv_fetcher` 的职责收束为 `PixivDatasetService`
- 为 `judge` 的高分收藏行为引入 `bookmark_jobs` 异步任务表与批处理框架

这一步确立了后续重构的基本分层：

```text
dataset_api -> PixivDatasetService -> BetterPixiv + DatasetDB
```

### `6c39793` `Ignore local runtime and workspace files`

补齐 `.gitignore`，把本地运行态和工作区噪音排除出版本控制，包括：

- `venv/`
- `logs/`
- `mock_r2/`
- `dataset.db` 及备份
- 本地配置、代理文件、临时脚本等

这一步主要是清理仓库边界，减少后续重构中的脏工作区干扰。

### `fe8064a` `Switch dataset images to source-url proxy flow`

这一提交完成了数据流的核心切换：

- 不再下载图片文件
- 直接从 `BetterPixiv.WorkDetail` 中提取原图 URL
- 在数据库 `images` 表中新增 `source_image_url`
- 图片访问端点改为返回/重定向到：

```text
https://document-worker.hayaseyuuka.date/?urlToProxy={图片链接}&refererURL=https://www.pixiv.net/
```

同时做了这些配套改造：

- `PixivDatasetService.fetch_and_store()` 取代旧下载路径
- 清理逻辑改成只维护数据库 `status`，不再删本地图片文件
- 设计文档和配置样例同步改到“源图 URL + 反代访问”的模型

### `59614fc` `Remove dataset source-url backfill compatibility`

在生产数据库完成一次性迁移之后，删除运行时兼容层：

- 移除 `ensure_source_image_url()` 懒回填逻辑
- 让 `dataset_api` 不再在请求路径中补数据
- 将 `source_image_url` 收紧为必备字段

这一步的目标是让线上逻辑回到单一模型，而不是长期带着迁移兼容代码运行。

### `e8967b4` `Batch bookmarks with fetch maintenance and preload images`

这一步包含两部分：

1. 收藏队列改为按“补图批次”触发
2. 前端增加图片预载

后端方面：

- `auto_maintenance()` 不再每次评分后都立刻处理 `bookmark_jobs`
- 只有当 `wait_count < min_wait`、需要补图时，才会：
  - 打开一个共享的 `BetterPixiv` 会话
  - 先批量处理收藏任务
  - 再在同一个会话里拉推荐并写库

前端方面：

- 评分页增加图片预取/预载，减少连续评分时的等待

### `6803033` `Flush pending bookmark jobs on shutdown`

为优雅退出增加收藏队列冲刷能力：

- `app.py` 增加 `shutdown` hook
- `PixivDatasetService` 增加 `flush_pending_bookmark_jobs_on_shutdown()`
- `DatasetDB` 增加待执行收藏任务计数方法

行为语义是：

- 第一次 `Ctrl+C` 触发优雅退出时，尽量用一个 Pixiv 会话冲刷待处理 bookmark 队列
- 若再次 `Ctrl+C`，则交由进程直接终止

### `b43e2e1` `Fix dataset image prefetch race`

修复评分页的预取竞态：

- 不再使用单个 `prefetchedNextWaitImage`
- 改为维护 `prefetchedWaitImages` 队列
- 引入 `prefetchGeneration`，丢弃过期预取结果
- 预取深度调整为 5 张

修复目标是解决：

- 高速评分时同一张图重复展示
- 旧预取请求晚到，覆盖当前预取状态

### `13d737b` `Fix leftover dataset prefetch variable`

这是对上一提交的一个前端补丁修复：

- 清除 `dataset.html` 中残留的旧变量 `prefetchedNextWaitImage`

该残留会导致页面初始化时直接抛出：

```text
ReferenceError: prefetchedNextWaitImage is not defined
```

修复后线上页面恢复正常加载。

## 生产环境操作记录

本次会话中，除代码提交外，还在生产环境做了几项重要操作。

### 数据库备份

生产环境数据库做过多次显式备份，相关文件包括：

- `dataset.db.bak_source_url_migration_20260410_111339`
- `dataset.db.bak_batch_bookmark_preload_20260410_123915`

这些备份对应的节点分别是：

- `source_image_url` 迁移前
- bookmark 批处理与前端预载优化上线前

### `source_image_url` 一次性迁移

生产环境曾存在大量旧记录缺少 `source_image_url`。本次会话中通过临时脚本完成了迁移：

- 批量调用 Pixiv API 获取作品详情
- 回填所有缺失的 `source_image_url`
- 同步校正 `local_filename`
- 最终核对为 `remaining_missing=0`

这也是后续删除懒回填兼容层的前提。

### 上线验证

每次关键提交上线后都做了最小冒烟验证，覆盖：

- `GET /dataset`
- `GET /dataset/stats`
- `GET /dataset/image/info/offset/...`
- `GET /dataset/image/{pid}/{page_index}`

验证重点包括：

- 页面正常加载
- 图片 URL 指向 `document-worker`
- 302 跳转正常
- bookmark 队列按新的批次语义工作
- shutdown 时能冲刷待执行 bookmark 队列

## 当前状态

到本文件记录时，系统的关键行为如下：

- 图片不再下载到本地
- 数据库持久化 `source_image_url`
- 图片访问走 `document-worker` 反代
- 高分收藏进入 `bookmark_jobs`
- bookmark 只在“待评分不足、需要补图”的维护批次中集中处理
- 进程退出时会尽量冲刷待执行 bookmark 队列
- 前端对后续 5 张图片做预取和预载，并修复了重复展示竞态

## 备注

本文件偏向“工作日志”和“变更说明”，不是正式设计文档。若后续还要继续演进 Pixiv 数据集系统，建议：

- 保持本文件按提交补充
- 让 [pixiv_dataset_design.md](/D:/CodeLib/my_utils/pixiv_dataset_design.md) 只承担稳定设计描述
- 让本文件记录阶段性的实现与上线轨迹
