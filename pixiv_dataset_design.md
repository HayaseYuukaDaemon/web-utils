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

---

## 系统架构

### 运行机制

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI 应用                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Lifespan 后台任务                                │  │
│  │  - 监控 judge_wait 目录                          │  │
│  │  - 图片数量 < 100 时自动拉取 100 份新作品        │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──────────────┐        ┌──────────────┐              │
│  │ /dataset     │        │ /judge       │              │
│  │ 展示评分界面  │───────▶│ 提交评分     │              │
│  └──────────────┘        └──────────────┘              │
└─────────────────────────────────────────────────────────┘
           │                        │
           ▼                        ▼
    ┌─────────────┐          ┌─────────────┐
    │ judge_wait/ │          │ judge_done/ │
    │ (100-200张) │─────────▶│ (最多100张) │
    └─────────────┘  评分后   └─────────────┘
           │                        │
           ▼                        ▼
    ┌──────────────────────────────────┐
    │      SQLite 数据库                │
    │  - pid (作品ID)                   │
    │  - score (评分 0-3)               │
    │  - status (wait/done/deleted)    │
    └──────────────────────────────────┘
```

---

## 数据流程

### 1. 作品拉取
- 从 Pixiv 获取个性化推荐（使用 `better_pixiv.py`）
- 下载图片到存储目录（Mock 模式：`mock_r2/pixiv_dataset/judge_wait/`）
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
6. 执行后台维护任务（见下文）

### 3. 存储管理

**设计理念**：
- 所有图片存储在同一个目录（不区分 wait/done）
- 通过数据库 `status` 字段区分待评分和已评分
- 按需清理文件，保持存储空间可控

**目录结构**：
```
~/cf_r2/cf-disk/pixiv_dataset/judge_wait/  # 生产环境
mock_r2/pixiv_dataset/judge_wait/           # Mock 模式
```

**文件管理策略**：
- 待评分图片（`status='wait'`）：保持 100+ 张
- 已评分图片（`status='done'`）：保持 100 张以内
- 已删除图片（`status='deleted'`）：文件已清理，数据库保留记录

### 4. 评分后的自动维护

每次评分后自动执行以下维护任务：

**4.1 确保待评分图片充足**
```python
wait_count = COUNT(*) WHERE status='wait' AND score IS NULL

if wait_count < 100:
    # 直接拉取 100 份新作品
    fetch_and_download(100)
```

**4.2 清理已评分图片（LRU 策略）**
```python
done_count = COUNT(*) WHERE status='done'

if done_count > 100:
    # 获取需要删除的图片（按 judged_at 升序，保留最近 100 张）
    to_delete = SELECT * FROM images
                WHERE status='done'
                ORDER BY judged_at ASC
                LIMIT (done_count - 100)

    # 删除文件并更新数据库状态
    for image in to_delete:
        delete_file(image.local_filename)
        UPDATE images SET status='deleted' WHERE id=image.id
```

### 5. 模型训练
- 从数据库导出所有已评分的 `(pid, page_index, score)`
- 根据 pid 从 Pixiv 实时拉取图片和元数据
- 使用评分作为标签训练分类模型

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

    -- 文件管理
    local_filename TEXT,  -- 文件名，如 '12345678_p0.jpg'

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
- `wait`：待评分（图片文件存在）
- `done`：已评分（图片文件存在）
- `deleted`：已删除（图片文件已清理，数据库保留评分记录）

**评分字段**：
- `NULL`：未评分
- `0-3`：四分类评分

---

## API 端点设计

### GET /dataset
展示评分界面

**功能**：
- 从 `judge_wait/` 目录获取一张未评分图片
- 渲染 HTML 页面，展示图片和四个评分按钮
- 显示当前进度（已评分数量 / 总数量）

**返回**：
- HTML 页面（使用 `templates/dataset.html`）

---

### POST /judge
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
3. **自动维护任务**：
   - **确保待评分图片充足**：
     - 查询待评分图片数量：`COUNT(*) WHERE status='wait' AND score IS NULL`
     - 如果 < 100 张，直接拉取 100 份新作品
   - **清理已评分图片（LRU）**：
     - 查询已评分图片数量：`COUNT(*) WHERE status='done'`
     - 如果 > 100 张，按 `judged_at` 升序删除最旧的图片
     - 删除文件并更新状态为 `deleted`

**返回**：
```json
{
  "success": true,
  "message": "评分成功",
  "next_image": {
    "pid": 87654321,
    "page_index": 0
  },
  "maintenance": {
    "fetched_works": 0,
    "deleted_images": 0
  }
}
```

---

### GET /dataset/image
获取图片（支持顺序评分和回溯）

**功能**：
- 通过 offset 参数灵活获取待评分或已评分图片
- 支持顺序评分、跳过、回溯查看

**请求参数**：
- `offset` (整数，必需)
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
  "success": true,
  "data": {
    "pid": 12345678,
    "page_index": 0,
    "filename": "12345678_p0.jpg",
    "image_url": "/static/judge_wait/12345678_p0.jpg",
    "score": null,  // 待评分为 null，已评分返回 0-3
    "status": "wait",  // 'wait' 或 'done'
    "judged_at": null  // 已评分时返回评分时间
  },
  "meta": {
    "total_wait": 150,  // 待评分总数
    "total_done": 50,   // 已评分总数
    "current_offset": 0  // 当前 offset
  }
}
```

**错误响应**：
```json
{
  "success": false,
  "error": "No image found at offset 100"
}
```

**使用场景**：
1. **顺序评分**：前端从 offset=0 开始，每次评分后 offset+1
2. **跳过图片**：用户可以点击"下一张"跳过当前图片
3. **回溯查看**：用户可以查看最近评分的图片（offset=-1, -2, ...）
4. **修改评分**：回溯到已评分图片，重新提交评分

---

## 目录结构

```
my_utils/
├── app.py                      # FastAPI 主应用
├── better_pixiv.py             # Pixiv API 封装
├── pixiv_dataset_design.md     # 本设计文档
├── dataset.db                  # SQLite 数据库
├── judge_wait/                 # 待评分图片 (100-200张)
│   ├── 12345678_p0.jpg
│   └── ...
├── judge_done/                 # 已评分图片 (最多100张)
│   ├── 87654321_p0.jpg
│   └── ...
└── templates/
    └── dataset.html            # 评分界面模板
```

---

## 技术栈

- **Web 框架**：FastAPI
- **数据库**：SQLite
- **Pixiv API**：`better_pixiv.py`（基于 pixivpy-async）
- **后台任务**：FastAPI Lifespan
- **前端**：HTML + JavaScript（简单的评分界面）

---

## 实现要点

### 1. Lifespan 后台任务

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化
    await init_database()
    await check_and_fetch_works()  # 初始检查
    yield
    # 关闭时清理
    pass
```

### 2. 自动拉取逻辑

```python
async def check_and_fetch_works():
    """检查 judge_wait 目录，少于 100 张时拉取新作品"""
    wait_count = len(list(Path('judge_wait').glob('*')))
    if wait_count < 100:
        await fetch_pixiv_recommendations(count=100)
```

### 3. 目录清理

```python
async def cleanup_judge_done():
    """保持 judge_done 目录最多 100 张图片"""
    # 从数据库获取已评分作品，按 judged_at 排序
    # 删除最旧的图片文件
    # 更新数据库 status 为 'deleted'
```

### 4. 训练数据导出

```python
def export_training_data():
    """导出训练数据"""
    # SELECT pid, score FROM works WHERE score IS NOT NULL
    # 返回 [(pid, score), ...]
```

---

## 未来扩展

1. **批量评分**：一次展示多张图片，提高评分效率
2. **评分统计**：展示各分类的数量分布
3. **回溯功能**：查看 `judge_done/` 中的已评分图片，支持修改评分
4. **过滤器**：按作者、标签过滤推荐（需要扩展数据库）
5. **模型反馈**：训练后的模型预测评分，与实际评分对比

---

## 注意事项

1. **VPS 空间管理**：
   - 定期清理 `judge_done/` 目录
   - 数据库只存 pid，不存图片元数据

2. **Pixiv 作品删除风险**：
   - 训练时可能遇到作品被删除
   - 需要在训练脚本中处理 404 错误

3. **并发控制**：
   - 拉取任务应该加锁，避免重复拉取
   - 评分请求应该是原子操作

4. **数据备份**：
   - 定期备份 `dataset.db`
   - 这是唯一的数据源

---

## 开发计划

- [ ] 创建数据库表结构
- [ ] 实现 `/dataset` 端点和前端界面
- [ ] 实现 `/judge` 端点和评分逻辑
- [ ] 实现 Pixiv 推荐拉取功能
- [ ] 实现 Lifespan 后台任务
- [ ] 实现目录管理和清理逻辑
- [ ] 测试完整流程
- [ ] 编写训练数据导出脚本
