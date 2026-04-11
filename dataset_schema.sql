-- ============================================
-- Pixiv 图片评分系统数据库结构
-- ============================================
-- 创建日期: 2026-04-05
-- 数据库类型: SQLite
-- 用途: 存储 Pixiv 作品评分数据，用于个人图片喜好模型训练
-- ============================================

-- 删除已存在的表（如果存在）
DROP TABLE IF EXISTS images;
DROP TABLE IF EXISTS bookmark_jobs;
DROP VIEW IF EXISTS score_stats;
DROP VIEW IF EXISTS daily_stats;

-- ============================================
-- 主表: images
-- ============================================
-- 存储 Pixiv 图片的评分数据（按图片评分，而非按作品）
-- 设计理念:
--   - 一个作品（pid）可能包含多张图片（page_index）
--   - 每张图片单独评分
--   - 极简存储，只保留 pid 和评分，训练时实时从 Pixiv 拉取元数据

CREATE TABLE images (
    -- ========== 主键 ==========
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ========== 核心数据 ==========
    -- Pixiv 作品 ID（一个作品可能有多张图片）
    pid INTEGER NOT NULL,

    -- 页码索引（从 0 开始）
    -- 单图作品: page_index = 0
    -- 多图作品: page_index = 0, 1, 2, ...
    page_index INTEGER NOT NULL DEFAULT 0,

    -- 评分（四分类）
    -- 0 = 讨厌 (HATE)
    -- 1 = 中性 (NEUTRAL)
    -- 2 = 有点感觉 (LIKE)
    -- 3 = 非常喜欢 (LOVE)
    -- NULL = 未评分
    score INTEGER CHECK(score IN (0, 1, 2, 3)),

    -- ========== 状态管理 ==========
    -- 图片状态
    -- 'wait' = 待评分
    -- 'done' = 已评分
    -- 'deleted' = 已从活跃评分集合中清理，但保留评分记录
    status TEXT DEFAULT 'wait' CHECK(status IN ('wait', 'done', 'deleted')),

    -- ========== 时间戳 ==========
    -- 图片拉取时间（ISO 8601 格式，如 '2026-04-05T10:30:00'）
    fetched_at TEXT NOT NULL,

    -- 评分时间（ISO 8601 格式，NULL 表示未评分）
    judged_at TEXT,

    -- ========== 图片来源 ==========
    -- 本地文件名（通常由 Pixiv 原图 URL 的 basename 派生）
    local_filename TEXT,

    -- Pixiv 原图 URL，用于请求时经 document-worker 反代访问
    source_image_url TEXT NOT NULL,

    -- ========== 唯一约束 ==========
    -- 同一作品的同一页只能有一条记录
    UNIQUE(pid, page_index)
);

-- ============================================
-- 索引
-- ============================================
-- 用于优化常见查询

-- 按状态查询（获取待评分图片）
CREATE INDEX idx_status ON images(status);

-- 按评分统计（分析喜好分布）
CREATE INDEX idx_score ON images(score);

-- 按 pid 查询（查询某个作品的所有图片）
CREATE INDEX idx_pid ON images(pid);

-- 按评分时间排序（用于清理 judge_done 目录）
CREATE INDEX idx_judged_at ON images(judged_at);

-- 组合索引：按拉取时间和页码排序（用于获取待评分图片）
CREATE INDEX idx_fetched_page ON images(fetched_at, page_index);

-- ============================================
-- 异步任务表: bookmark_jobs
-- ============================================
-- 存储待执行的 Pixiv 收藏任务（按作品维度去重）

CREATE TABLE bookmark_jobs (
    pid INTEGER PRIMARY KEY,
    action TEXT NOT NULL DEFAULT 'bookmark' CHECK(action IN ('bookmark', 'unbookmark')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'done')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_retry_at TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX idx_bookmark_jobs_pending
ON bookmark_jobs(status, next_retry_at, created_at);

-- ============================================
-- 统计视图
-- ============================================

-- 评分分布统计
CREATE VIEW score_stats AS
SELECT
    score,
    CASE score
        WHEN 0 THEN '讨厌'
        WHEN 1 THEN '中性'
        WHEN 2 THEN '有点感觉'
        WHEN 3 THEN '非常喜欢'
        ELSE '未评分'
    END as score_label,
    COUNT(*) as count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM images WHERE score IS NOT NULL), 2) as percentage
FROM images
WHERE score IS NOT NULL
GROUP BY score
ORDER BY score DESC;

-- 每日评分统计
CREATE VIEW daily_stats AS
SELECT
    DATE(judged_at) as date,
    COUNT(*) as judged_count,
    AVG(score) as avg_score,
    COUNT(CASE WHEN score = 3 THEN 1 END) as love_count,
    COUNT(CASE WHEN score = 2 THEN 1 END) as like_count,
    COUNT(CASE WHEN score = 1 THEN 1 END) as neutral_count,
    COUNT(CASE WHEN score = 0 THEN 1 END) as hate_count
FROM images
WHERE judged_at IS NOT NULL
GROUP BY DATE(judged_at)
ORDER BY date DESC;

-- ============================================
-- 常用查询示例
-- ============================================

-- 1. 获取下一张待评分图片
-- SELECT * FROM images
-- WHERE status = 'wait' AND score IS NULL
-- ORDER BY fetched_at ASC, page_index ASC
-- LIMIT 1;

-- 2. 提交评分
-- UPDATE images
-- SET score = ?, judged_at = ?, status = 'done'
-- WHERE pid = ? AND page_index = ?;

-- 3. 获取待清理的已评分图片（保留最近 100 张）
-- SELECT * FROM images
-- WHERE status = 'done'
-- ORDER BY judged_at ASC
-- LIMIT (SELECT MAX(0, COUNT(*) - 100) FROM images WHERE status = 'done');

-- 4. 统计各状态图片数量
-- SELECT status, COUNT(*) as count
-- FROM images
-- GROUP BY status;

-- 5. 导出训练数据
-- SELECT pid, page_index, score
-- FROM images
-- WHERE score IS NOT NULL
-- ORDER BY judged_at ASC;

-- 6. 查看评分分布
-- SELECT * FROM score_stats;

-- 7. 查看每日评分统计
-- SELECT * FROM daily_stats;

-- 8. 查询某个作品的所有图片
-- SELECT * FROM images
-- WHERE pid = 12345678
-- ORDER BY page_index;

-- 9. 统计作品数量（去重 pid）
-- SELECT COUNT(DISTINCT pid) as work_count FROM images;

-- 10. 统计图片数量
-- SELECT COUNT(*) as image_count FROM images;

-- ============================================
-- 数据完整性说明
-- ============================================
-- 1. (pid, page_index) 组合必须唯一，防止重复添加同一张图片
-- 2. score 只能是 0, 1, 2, 3 或 NULL
-- 3. status 只能是 'wait', 'done', 'deleted'
-- 4. fetched_at 必须填写，记录图片拉取时间
-- 5. judged_at 在评分前为 NULL，评分后必须填写
-- 6. page_index 从 0 开始，单图作品为 0，多图作品为 0, 1, 2, ...

-- ============================================
-- 维护建议
-- ============================================
-- 1. 定期备份数据库文件 (dataset.db)
-- 2. 定期清理 status='deleted' 的旧记录（可选）
-- 3. 使用 VACUUM 命令压缩数据库（删除大量记录后）
-- 4. 监控数据库大小，确保 VPS 空间充足
-- 5. 注意区分"作品数量"和"图片数量"：
--    - 作品数量: COUNT(DISTINCT pid)
--    - 图片数量: COUNT(*)
