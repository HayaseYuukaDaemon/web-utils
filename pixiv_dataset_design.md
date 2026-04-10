# Pixiv 图片评分系统设计文档

## 项目概述

这是一个基于 Pixiv 推荐的图片评分系统，用于收集个人图片喜好数据，为训练个人图片喜好模型提供数据集。

### 核心目标
- 从 Pixiv 获取个性化推荐作品
- 通过 Web 界面进行四分类评分
- 收集评分数据用于模型训练
- 在有限的 VPS 空间下高效运行

---

## 评分分类

采用**四分类**评分系统：

| 分值 | 标签 | 含义 |
|------|------|------|
| 3 | 非常喜欢 | LOVE |
| 2 | 有点感觉 | LIKE |
| 1 | 中性 | NEUTRAL |
| 0 | 讨厌 | HATE |

> **自动收藏**：评分 >= 2（LIKE 或 LOVE）时会把作品加入异步收藏队列；收藏任务会在“待评分不足、触发补图批次”时集中执行，也会在应用优雅退出时尽量冲刷。

---

## 系统架构

### 运行机制

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI 应用                          │
│                                                          │
│  ┌──────────────┐        ┌──────────────┐              │
│  │ /dataset     │        │ /judge       │              │
│  │ 展示评分界面  │───────▶│ 提交评分     │              │
│  └──────────────┘        └──────────────┘              │
│                                       │                  │
│                                       ▼                  │
│                              ┌──────────────────────┐   │
│                              │ 执行维护任务           │   │
│                              │ - 低水位时处理收藏队列 │   │
│                              │ - 检查并拉取新作品    │   │
│                              │ - 清理旧已评分图片    │   │
│                              └──────────────────────┘   │
└─────────────────────────────────────────────────────────┘
           │
           ▼
    ┌─────────────────────────────────┐
    │      SQLite 数据库               │
    │  - pid (作品ID)                 │
    │  - score (评分 0-3)             │
    │  - status (wait/done/deleted)   │
    └─────────────────────────────────┘
           │
           ▼
    ┌──────────────────────────────────┐
    │      SQLite 数据库               │
    │  - local_filename               │
    │  - source_image_url             │
    │  (不再缓存图片文件)               │
    └──────────────────────────────────┘
```

**维护任务触发时机**：
- `/judge` 评分成功后**后台执行**（不阻塞响应，维护在后台异步进行）
- `/dataset/maintenance` 端点**后台执行**（不阻塞）

---

## 数据流程

### 1. 作品拉取
- 从 Pixiv 获取个性化推荐（使用 `better_pixiv.py`）
- 直接从 `WorkDetail` 提取每一页的原图链接
- 记录到数据库，状态为 `wait`，评分为 `NULL`

### 2. 评分流程
1. 用户访问 `/dataset` 端点
2. 系统从数据库查询未评分图片（`status='wait' AND score IS NULL`）
3. 展示图片和四个评分按钮
4. 用户点击评分，向 `/judge` 端点发送请求
5. 后端更新数据库：
   - 设置 `score` 字段（0-3）
   - 设置 `judged_at` 时间戳
   - 更新 `status` 为 `done`
6. 如果 `score >= 2`，将作品加入异步收藏队列
7. **后台执行维护任务**：
   - 若待评分图片不足，则在同一个 Pixiv 会话内先批量处理 bookmark 队列，再拉取新作品
   - 清理已评分图片（触发条件：`done_count > max_done`）

### 3. 存储管理

**设计理念**：
- 不再下载和缓存图片文件
- 数据库只保存图片页记录和 Pixiv 原图 URL
- 展示图片时由后端重定向到 `document-worker` 反代 URL

**活跃集合管理策略**：
- 待评分图片（`status='wait'`）：保持 `min_wait` 张以上（默认 20 张，Mock 模式 20 张）
- 已评分图片（`status='done'`）：上限 `max_done` 张（默认 100 张）
- 已清理图片（`status='deleted'`）：仅表示移出活跃评分集合，数据库记录仍保留

### 4. 评分后的自动维护

每次评分后后台触发以下维护任务：

**4.1 确保待评分图片充足**
```python
wait_count = COUNT(*) WHERE status='wait' AND score IS NULL
min_wait = counts_config.get('min_wait', 20)  # Mock: 20, 生产: 20

if wait_count < min_wait:
    fetch_count = 10 if mock_mode else counts_config.get('fetch_count', 50)
    async with create_pixiv_client() as pixiv:
        process_bookmark_jobs_with_client(pixiv)
        fetch_and_store_with_client(pixiv, fetch_count)
```

**4.2 清理已评分图片（LRU 策略）**
```python
done_count = COUNT(*) WHERE status='done'
max_done = counts_config.get('max_done', 100)
keep_count = counts_config.get('keep_count', 50)

if done_count > max_done:
    # 获取需要删除的图片（按 judged_at 升序，保留最近 keep_count 张）
    to_delete = SELECT * FROM images
                WHERE status='done'
                ORDER BY judged_at ASC
                LIMIT (done_count - keep_count)

    for image in to_delete:
        UPDATE images SET status='deleted' WHERE id=image.id
```

---

## 数据库设计

### 表结构（按图片评分）

**设计理念**：
- 一个作品（pid）可能包含多张图片（page_index）
- 每张图片单独评分
- 极简存储，只保留 pid 和评分，训练时实时从 Pixiv 拉取元数据

```sql
CREATE TABLE images (
    -- 主键
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 核心数据
    pid INTEGER NOT NULL,  -- Pixiv 作品 ID（一个作品可能有多张图片）
    page_index INTEGER NOT NULL DEFAULT 0,  -- 页码索引（从 0 开始）
    score INTEGER CHECK(score IN (0, 1, 2, 3)),
    -- 0=讨厌, 1=中性, 2=有点感觉, 3=非常喜欢

    -- 状态管理
    status TEXT DEFAULT 'wait' CHECK(status IN ('wait', 'done', 'deleted')),
    fetched_at TEXT NOT NULL,  -- ISO 8601 格式
    judged_at TEXT,            -- 评分时间

    -- 图片来源
    local_filename TEXT,     -- 由原图 URL basename 派生的文件名
    source_image_url TEXT NOT NULL,   -- Pixiv 原图 URL

    -- 唯一约束
    UNIQUE(pid, page_index)  -- 同一作品的同一页只能有一条记录
);

-- 索引
CREATE INDEX idx_status ON images(status);
CREATE INDEX idx_score ON images(score);
CREATE INDEX idx_pid ON images(pid);
CREATE INDEX idx_judged_at ON images(judged_at);
CREATE INDEX idx_fetched_page ON images(fetched_at, page_index);
```

### 关键设计点

**多图作品处理**：
- 单图作品：`page_index = 0`
- 多图作品：`page_index = 0, 1, 2, ...`
- 每张图片独立评分，独立管理状态

**数量统计**：
- 图片数量：`COUNT(*)`
- 作品数量：`COUNT(DISTINCT pid)`

**状态字段**：
- `wait`：待评分
- `done`：已评分
- `deleted`：已从活跃评分集合中清理，数据库保留评分记录

**评分字段**：
- `NULL`：未评分
- `0-3`：四分类评分

---

## API 端点设计

### 认证

所有 `/dataset` 路由均需认证（通过 `Authoricator` 中间件），需要 `dataset.use` 权限。

### GET /dataset
展示评分界面

**功能**：
- 渲染 HTML 页面，展示图片和四个评分按钮
- 显示当前统计信息

**前端功能**：
- 四个评分按钮（讨厌/中性/有点感觉/非常喜欢）
- 上一张、刷新、跳过按钮
- 键盘快捷键（1-4 评分，← → 切换图片，空格跳过）
- 评分成功后自动加载下一张待评分图片
- 前端会预取后续 5 张待评分图片，并在浏览器中预载图片资源

**返回**：
- HTML 页面（`templates/dataset.html`）

> 注意：评分界面没有手动触发维护的按钮。如需手动触发维护，请调用 `POST /dataset/maintenance` API。

---

### GET /dataset/image/info/offset/{offset}
获取图片信息（JSON）

**功能**：
- 通过 offset 参数灵活获取待评分或已评分图片
- 支持顺序评分、跳过、回溯

**请求参数**：
- `offset` (整数)
  - `offset >= 0`：获取第 offset 张待评分图片
    - `0` = 第一张待评分
    - `1` = 第二张待评分
    - 以此类推
  - `offset < 0`：回溯已评分图片
    - `-1` = 最近评分的一张
    - `-2` = 倒数第二张
    - 以此类推

**查询逻辑**：
```python
if offset >= 0:
    # 获取待评分图片（按拉取时间和页码升序）
    SELECT * FROM images
    WHERE status = 'wait' AND score IS NULL
    ORDER BY fetched_at ASC, page_index ASC
    LIMIT 1 OFFSET ?
else:
    # 回溯已评分图片（按评分时间降序）
    SELECT * FROM images
    WHERE status = 'done' AND score IS NOT NULL
    ORDER BY judged_at DESC
    LIMIT 1 OFFSET ?  -- 使用 abs(offset) - 1
```

**返回**：
```json
{
  "pid": 12345678,
  "page_index": 0,
  "filename": "12345678_p0.jpg",
  "image_url": "https://document-worker.hayaseyuuka.date/?urlToProxy=https%3A%2F%2Fi.pximg.net%2F...&refererURL=https%3A%2F%2Fwww.pixiv.net%2F",
  "score": null,
  "status": "wait",
  "judged_at": null
}
```

**错误响应**：`404 Not Found`（图片不存在）

---

### GET /dataset/image/offset/{offset}
获取图片（redirect 到图片反代 URL）

**功能**：与 `/dataset/image/info/offset/{offset}` 相同，但直接返回 302 重定向到图片 URL

**返回**：`302 Redirect` 到图片反代 URL

---

### GET /dataset/image/{pid}/{page_index}
获取指定图片（redirect 到图片反代 URL）

**功能**：通过 pid 和 page_index 精确获取某张图片

**返回**：`302 Redirect` 到图片反代 URL

---

### POST /dataset/judge
提交评分

**请求参数**：
```json
{
  "pid": 12345678,
  "page_index": 0,
  "score": 3
}
```

**处理流程**：
1. 验证 pid、page_index 和 score
2. 更新数据库：
   ```sql
   UPDATE images
   SET score = ?, judged_at = ?, status = 'done'
   WHERE pid = ? AND page_index = ?;
   ```
3. **自动收藏**：在 `score >= 2` 时先写入异步收藏队列，再由后台批量调用 Pixiv API 将作品加入收藏夹
4. **后台执行维护任务**（不阻塞响应）：
   - 查询待评分图片数量，不足时拉取新作品
   - 查询已评分图片数量，超量时清理旧图片
5. 返回维护结果

**返回**：
```json
{
  "success": true,
  "message": "评分成功",
  "next_image": {
    "pid": 87654321,
    "page_index": 0
  },
  "maintenance_status": "scheduled"
}
```

> 注意：由于维护任务在后台执行，响应中仅返回 `maintenance_status` 字符串（`"scheduled"` 或 `"running"`）。维护任务的实际执行结果可在 `/dataset/stats` 端点查看。

---

### POST /dataset/maintenance
手动触发维护任务

**功能**：后台执行维护任务（不阻塞响应）

**返回**：
```json
{
  "success": true,
  "message": "维护任务已启动",
  "status": "scheduled"
}
```

如果任务已在运行：
```json
{
  "success": false,
  "message": "维护任务正在运行中",
  "status": "running"
}
```

> 注意：当前实现返回的 JSON 不包含 `last_result` 字段。如需查看上次维护结果，可通过 `/dataset/stats` 端点获取当前统计信息。

---

### GET /dataset/stats
获取统计信息

**返回**：
```json
{
  "total_images": 150,
  "total_works": 120,
  "wait_count": 100,
  "done_count": 45,
  "deleted_count": 5
}
```

---

## 目录结构

```
my_utils/
├── app.py                      # FastAPI 主应用
├── better_pixiv.py             # Pixiv API 封装（基于 pixivpy-async）
├── dataset_api.py              # Dataset 路由和 API 端点
├── dataset_db.py               # SQLite 数据库 CRUD 操作
├── pixiv_dataset_service.py    # PixivDatasetService：数据集抓取和自动维护逻辑
├── dataset_schema.sql          # 数据库 Schema
├── site_utils.py               # 认证中间件和权限控制
├── setup_logger.py             # 日志配置
├── pixiv_config.yaml           # Pixiv 配置（token、代理、维护参数等）
├── dataset.db                  # SQLite 数据库文件
└── templates/
    └── dataset.html            # 评分界面模板
```

---

## 技术栈

- **Web 框架**：FastAPI
- **数据库**：SQLite
- **Pixiv API**：`better_pixiv.py`（基于 pixivpy-async）
- **后台任务**：FastAPI BackgroundTasks + asyncio.Lock
- **前端**：HTML + JavaScript（简单的评分界面）
- **图片访问**：`document-worker` 反代 Pixiv 原图
- **认证**：基于 `auth_token` 的权限控制（`site_utils.py`）

---

## 配置参数（pixiv_config.yaml）

```yaml
# Pixiv refresh token
refresh_token: "your_refresh_token"

# 代理设置
proxy: "http://127.0.0.1:10809"

# Mock 模式
# true: 本地开发，拉取数量少（10 份）
# false: 生产环境
mock_mode: true

# 数据库路径
db_path: "dataset.db"

# 维护任务数量配置（counts_config）
counts_config:
  min_wait: 20       # 待评分图片不足时触发拉取的下限
  max_done: 100     # 已评分图片上限，超出时触发清理
  keep_count: 50    # 清理时保留的最近已评分图片数量
  fetch_count: 50   # 生产模式每次拉取的作品数量（Mock 模式固定为 10）
  bookmark_batch_size: 10           # 补图批次里单次最多处理多少个 bookmark job
  bookmark_shutdown_batch_size: 50  # 应用退出前冲刷 bookmark 队列时的批大小
```

### 各参数含义

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `min_wait` | 20 | 待评分图片数量低于此值时，自动拉取新作品 |
| `max_done` | 100 | 已评分图片数量高于此值时，自动清理旧图片 |
| `keep_count` | 50 | 清理时保留的最近已评分图片数量 |
| `fetch_count` | 50 | 生产模式每次拉取的作品数量（Mock 模式固定为 10） |
| `bookmark_batch_size` | 10 | 补图批次里单次处理的 bookmark job 上限 |
| `bookmark_shutdown_batch_size` | 50 | 应用退出前冲刷 bookmark 队列时的批大小 |

---

## 实现要点

### 1. 维护任务执行策略

**评分后后台执行**（`/judge` 端点）：
```python
# 添加后台维护任务（不阻塞响应）
background_tasks.add_task(run_maintenance_task)
```

**手动触发后台执行**（`/dataset/maintenance` 端点）：
```python
background_tasks.add_task(run_maintenance_task)
```

使用 `asyncio.Lock` 防止并发执行重复拉取。

### 2. 自动收藏

评分 >= 2（LIKE 或 LOVE）时，先把作品加入异步收藏队列：
```python
if request.score > 1 and dataset_service:
    dataset_service.enqueue_bookmark_job(request.pid)
```

当 `wait_count < min_wait`、系统进入补图批次时，后台维护任务会先批量消费这些 job，再在同一个 Pixiv 会话内拉取推荐作品，避免每次评分都单独登录一次 Pixiv。

另外，应用优雅退出时也会尽量冲刷当前可执行的 bookmark 队列。

### 3. 图片访问策略

所有图片请求都基于数据库中的 `source_image_url` 动态生成反代地址：

```text
https://document-worker.hayaseyuuka.date/?urlToProxy={图片链接}&refererURL=https://www.pixiv.net/
```

`source_image_url` 现在被视为必备字段，迁移完成后运行时不再做懒回填兼容。

### 4. 认证机制

使用 `Authoricator` 中间件进行权限控制：
```python
dependencies=[Depends(Authoricator([UserAbilities.DATASET_USE]))]
```

权限通过 `auth.json` 配置文件管理，详见 `site_utils.py`。

---

## 未来扩展

1. **批量评分**：一次展示多张图片，提高评分效率
2. **评分统计**：展示各分类的数量分布
3. **回溯功能**：查看已评分图片，支持修改评分
4. **过滤器**：按作者、标签过滤推荐（需要扩展数据库）
5. **模型反馈**：训练后的模型预测评分，与实际评分对比

---

## 注意事项

1. **VPS 空间管理**：
   - 不再缓存图片文件
   - 定期清理已评分图片状态（LRU 策略，保留最近 50 张）
   - 数据库保存 `pid/page_index/score/source_image_url`

2. **Pixiv 作品删除风险**：
   - 训练时可能遇到作品被删除
   - 需要在训练脚本中处理 404 错误

3. **并发控制**：
   - 拉取任务使用 `asyncio.Lock` 避免重复拉取
   - 评分请求是原子操作（SQLite UPDATE）

4. **数据备份**：
   - 定期备份 `dataset.db`
   - 这是唯一的数据源

5. **认证**：
   - 所有 API 端点需要有效的 `auth_token`
   - token 通过 cookie、query 参数或 header 传递
   - 需要 `dataset.use` 权限
