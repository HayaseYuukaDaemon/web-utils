"""
Pixiv 图片评分系统 - 数据库操作模块

提供数据库初始化和 CRUD 操作功能
按图片评分（一个作品可能包含多张图片，每张图片单独评分）
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from contextlib import contextmanager


class DatasetDB:
    """数据库操作类"""

    def __init__(self, db_path: str = "dataset.db", schema_path: str = "dataset_schema.sql"):
        """
        初始化数据库连接

        Args:
            db_path: 数据库文件路径
            schema_path: 数据库结构文件路径
        """
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path)
        if self.db_path.exists():
            self.ensure_runtime_schema()

    @contextmanager
    def get_connection(self):
        """获取数据库连接的上下文管理器"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使查询结果可以通过列名访问
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def init_database(self) -> None:
        """
        初始化数据库

        如果数据库文件不存在，创建并执行 schema.sql
        如果已存在，不做任何操作
        """
        if self.db_path.exists():
            print(f"数据库已存在: {self.db_path}")
            return

        if not self.schema_path.exists():
            raise FileNotFoundError(f"Schema 文件不存在: {self.schema_path}")

        print(f"正在创建数据库: {self.db_path}")
        schema_sql = self.schema_path.read_text(encoding='utf-8')

        with self.get_connection() as conn:
            conn.executescript(schema_sql)

        self.ensure_runtime_schema()
        print("数据库初始化完成")

    def ensure_runtime_schema(self) -> None:
        """为已有数据库补齐运行时需要的增量表结构。"""
        with self.get_connection() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(images)").fetchall()
            }
            if "source_image_url" not in columns:
                conn.execute("ALTER TABLE images ADD COLUMN source_image_url TEXT")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bookmark_jobs (
                    pid INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'done')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_retry_at TEXT NOT NULL,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bookmark_jobs_pending
                ON bookmark_jobs(status, next_retry_at, created_at)
                """
            )

    def add_image(self, pid: int, page_index: int, local_filename: str,
                  source_image_url: str | None = None,
                  fetched_at: Optional[str] = None) -> bool:
        """
        添加单张图片

        Args:
            pid: Pixiv 作品 ID
            page_index: 页码索引（从 0 开始）
            local_filename: 本地文件名
            source_image_url: 图片原始 URL
            fetched_at: 拉取时间（ISO 8601 格式），默认为当前时间

        Returns:
            bool: 是否添加成功
        """
        if fetched_at is None:
            fetched_at = datetime.now().isoformat()

        try:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO images (pid, page_index, local_filename, source_image_url, fetched_at, status)
                    VALUES (?, ?, ?, ?, ?, 'wait')
                    """,
                    (pid, page_index, local_filename, source_image_url, fetched_at)
                )
            print(f"添加图片成功: pid={pid}, page={page_index}, filename={local_filename}")
            return True
        except sqlite3.IntegrityError:
            print(f"图片已存在: pid={pid}, page={page_index}")
            return False
        except Exception as e:
            print(f"添加图片失败: {e}")
            return False

    def add_images(
            self,
            pid: int,
            filenames: list[str],
            source_image_urls: list[str] | None = None,
            fetched_at: Optional[str] = None
    ) -> int:
        """
        批量添加一个作品的多张图片

        Args:
            pid: Pixiv 作品 ID
            filenames: 文件名列表（按页码顺序）
            source_image_urls: 原图 URL 列表（按页码顺序）
            fetched_at: 拉取时间（ISO 8601 格式），默认为当前时间

        Returns:
            int: 成功添加的图片数量
        """
        if fetched_at is None:
            fetched_at = datetime.now().isoformat()

        success_count = 0
        for page_index, filename in enumerate(filenames):
            source_image_url = None
            if source_image_urls and page_index < len(source_image_urls):
                source_image_url = source_image_urls[page_index]
            if self.add_image(pid, page_index, filename, source_image_url, fetched_at):
                success_count += 1

        print(f"批量添加完成: pid={pid}, 成功 {success_count}/{len(filenames)} 张")
        return success_count

    def judge_image(self, pid: int, page_index: int, score: int) -> bool:
        """
        评分图片（首次评分）

        Args:
            pid: Pixiv 作品 ID
            page_index: 页码索引
            score: 评分 (0-3)

        Returns:
            bool: 是否评分成功
        """
        if score not in (0, 1, 2, 3):
            print(f"评分无效: score={score}，必须是 0-3")
            return False

        judged_at = datetime.now().isoformat()

        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE images
                    SET score = ?, judged_at = ?, status = 'done'
                    WHERE pid = ? AND page_index = ?
                    """,
                    (score, judged_at, pid, page_index)
                )
                if cursor.rowcount == 0:
                    print(f"评分失败: pid={pid}, page={page_index} 不存在")
                    return False

            print(f"评分成功: pid={pid}, page={page_index}, score={score}")
            return True
        except Exception as e:
            print(f"评分失败: {e}")
            return False

    def update_score(self, pid: int, page_index: int, score: int) -> bool:
        """
        修改已有评分

        Args:
            pid: Pixiv 作品 ID
            page_index: 页码索引
            score: 新评分 (0-3)

        Returns:
            bool: 是否修改成功
        """
        if score not in (0, 1, 2, 3):
            print(f"评分无效: score={score}，必须是 0-3")
            return False

        judged_at = datetime.now().isoformat()

        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE images
                    SET score = ?, judged_at = ?
                    WHERE pid = ? AND page_index = ?
                    """,
                    (score, judged_at, pid, page_index)
                )
                if cursor.rowcount == 0:
                    print(f"修改评分失败: pid={pid}, page={page_index} 不存在")
                    return False

            print(f"修改评分成功: pid={pid}, page={page_index}, new_score={score}")
            return True
        except Exception as e:
            print(f"修改评分失败: {e}")
            return False

    def update_status(self, pid: int, page_index: int, status: str) -> bool:
        """
        修改图片状态

        Args:
            pid: Pixiv 作品 ID
            page_index: 页码索引
            status: 新状态 ('wait', 'done', 'deleted')

        Returns:
            bool: 是否修改成功
        """
        if status not in ('wait', 'done', 'deleted'):
            print(f"状态无效: status={status}，必须是 'wait', 'done', 'deleted'")
            return False

        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE images
                    SET status = ?
                    WHERE pid = ? AND page_index = ?
                    """,
                    (status, pid, page_index)
                )
                if cursor.rowcount == 0:
                    print(f"修改状态失败: pid={pid}, page={page_index} 不存在")
                    return False

            print(f"修改状态成功: pid={pid}, page={page_index}, new_status={status}")
            return True
        except Exception as e:
            print(f"修改状态失败: {e}")
            return False

    def update_image_source_url(self, pid: int, page_index: int, source_image_url: str) -> bool:
        """更新图片原始 URL。"""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE images
                    SET source_image_url = ?
                    WHERE pid = ? AND page_index = ?
                    """,
                    (source_image_url, pid, page_index)
                )
                return cursor.rowcount > 0
        except Exception as e:
            print(f"更新图片原始 URL 失败: {e}")
            return False

    def get_image_by_offset(self, offset: int) -> Optional[dict]:
        """
        根据 offset 获取图片

        Args:
            offset: 偏移量
                - offset >= 0: 获取第 offset 张待评分图片
                - offset < 0: 获取倒数第 abs(offset) 张已评分图片

        Returns:
            dict | None: 图片信息字典，如果不存在返回 None
        """
        try:
            with self.get_connection() as conn:
                if offset >= 0:
                    # 获取待评分图片（按拉取时间和页码排序）
                    cursor = conn.execute(
                        """
                        SELECT * FROM images
                        WHERE status = 'wait' AND score IS NULL
                        ORDER BY fetched_at ASC, page_index ASC
                        LIMIT 1 OFFSET ?
                        """,
                        (offset,)
                    )
                else:
                    # 获取已评分图片（按评分时间倒序）
                    cursor = conn.execute(
                        """
                        SELECT * FROM images
                        WHERE status = 'done' AND score IS NOT NULL
                        ORDER BY judged_at DESC
                        LIMIT 1 OFFSET ?
                        """,
                        (abs(offset) - 1,)
                    )

                row = cursor.fetchone()
                if row is None:
                    return None

                return dict(row)
        except Exception as e:
            print(f"查询图片失败: {e}")
            return None

    def get_image_by_pid_page(self, pid: int, page_index: int) -> Optional[dict]:
        """
        根据 pid 和 page_index 获取图片

        Args:
            pid: Pixiv 作品 ID
            page_index: 页码索引

        Returns:
            dict | None: 图片信息字典，如果不存在返回 None
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM images WHERE pid = ? AND page_index = ?",
                    (pid, page_index)
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return dict(row)
        except Exception as e:
            print(f"查询图片失败: {e}")
            return None

    def get_images_by_pid(self, pid: int) -> list[dict]:
        """
        获取某个作品的所有图片

        Args:
            pid: Pixiv 作品 ID

        Returns:
            list[dict]: 图片信息列表（按 page_index 排序）
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM images WHERE pid = ? ORDER BY page_index",
                    (pid,)
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"查询作品图片失败: {e}")
            return []

    def get_stats(self) -> dict:
        """
        获取统计信息

        Returns:
            dict: 包含各种统计数据
        """
        try:
            with self.get_connection() as conn:
                # 图片统计
                cursor = conn.execute(
                    """
                    SELECT
                        COUNT(*) as total_images,
                        COUNT(DISTINCT pid) as total_works,
                        COUNT(CASE WHEN status = 'wait' THEN 1 END) as wait_count,
                        COUNT(CASE WHEN status = 'done' THEN 1 END) as done_count,
                        COUNT(CASE WHEN status = 'deleted' THEN 1 END) as deleted_count,
                        COUNT(CASE WHEN score IS NOT NULL THEN 1 END) as judged_count,
                        COUNT(CASE WHEN score IS NULL THEN 1 END) as unjudged_count
                    FROM images
                    """
                )
                stats = dict(cursor.fetchone())

                # 评分分布
                cursor = conn.execute(
                    """
                    SELECT score, COUNT(*) as count
                    FROM images
                    WHERE score IS NOT NULL
                    GROUP BY score
                    ORDER BY score DESC
                    """
                )
                score_dist = {row['score']: row['count'] for row in cursor.fetchall()}
                stats['score_distribution'] = score_dist

                return stats
        except Exception as e:
            print(f"获取统计信息失败: {e}")
            return {}

    def get_score_distribution(self) -> list[dict]:
        """
        获取评分分布（使用视图）

        Returns:
            list[dict]: 评分分布列表
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute("SELECT * FROM score_stats")
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"获取评分分布失败: {e}")
            return []

    def export_training_data(self) -> list[tuple[int, int, int]]:
        """
        导出训练数据

        Returns:
            list[tuple[int, int, int]]: [(pid, page_index, score), ...] 列表
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT pid, page_index, score
                    FROM images
                    WHERE score IS NOT NULL
                    ORDER BY judged_at ASC
                    """
                )
                return [(row['pid'], row['page_index'], row['score']) for row in cursor.fetchall()]
        except Exception as e:
            print(f"导出训练数据失败: {e}")
            return []

    def get_images_to_cleanup(self, keep_count: int = 100) -> list[dict]:
        """
        获取需要清理的已评分图片（保留最近 keep_count 张）

        Args:
            keep_count: 保留的图片数量

        Returns:
            list[dict]: 需要清理的图片列表
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT * FROM images
                    WHERE status = 'done'
                    ORDER BY judged_at ASC
                    LIMIT MAX(0, (SELECT COUNT(*) FROM images WHERE status = 'done') - ?)
                    """,
                    (keep_count,)
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"获取待清理图片失败: {e}")
            return []

    def enqueue_bookmark_job(self, pid: int) -> bool:
        """
        为作品加入收藏任务队列。

        已完成的任务不会重复入队；未完成任务会被刷新为 pending。
        """
        now = datetime.now().isoformat()

        try:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO bookmark_jobs (
                        pid, status, attempts, created_at, updated_at, next_retry_at, last_error
                    )
                    VALUES (?, 'pending', 0, ?, ?, ?, NULL)
                    ON CONFLICT(pid) DO UPDATE SET
                        status = CASE
                            WHEN bookmark_jobs.status = 'done' THEN 'done'
                            ELSE 'pending'
                        END,
                        attempts = CASE
                            WHEN bookmark_jobs.status = 'done' THEN bookmark_jobs.attempts
                            ELSE 0
                        END,
                        updated_at = excluded.updated_at,
                        next_retry_at = CASE
                            WHEN bookmark_jobs.status = 'done' THEN bookmark_jobs.next_retry_at
                            ELSE excluded.next_retry_at
                        END,
                        last_error = CASE
                            WHEN bookmark_jobs.status = 'done' THEN bookmark_jobs.last_error
                            ELSE NULL
                        END
                    """,
                    (pid, now, now, now)
                )
            print(f"加入收藏队列成功: pid={pid}")
            return True
        except Exception as e:
            print(f"加入收藏队列失败: {e}")
            return False

    def get_pending_bookmark_jobs(self, limit: int = 20) -> list[dict]:
        """获取当前可执行的收藏任务。"""
        now = datetime.now().isoformat()

        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT *
                    FROM bookmark_jobs
                    WHERE status = 'pending' AND next_retry_at <= ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (now, limit)
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"获取待执行收藏任务失败: {e}")
            return []

    def mark_bookmark_job_done(self, pid: int) -> bool:
        """将收藏任务标记为已完成。"""
        now = datetime.now().isoformat()

        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE bookmark_jobs
                    SET status = 'done',
                        updated_at = ?,
                        next_retry_at = ?,
                        last_error = NULL
                    WHERE pid = ?
                    """,
                    (now, now, pid)
                )
                return cursor.rowcount > 0
        except Exception as e:
            print(f"标记收藏任务完成失败: {e}")
            return False

    def mark_bookmark_job_retry(self, pid: int, delay_seconds: int, error_message: str) -> bool:
        """记录收藏任务失败，并设置下次重试时间。"""
        now = datetime.now()
        next_retry_at = (now + timedelta(seconds=delay_seconds)).isoformat()

        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE bookmark_jobs
                    SET attempts = attempts + 1,
                        updated_at = ?,
                        next_retry_at = ?,
                        last_error = ?
                    WHERE pid = ?
                    """,
                    (now.isoformat(), next_retry_at, error_message, pid)
                )
                return cursor.rowcount > 0
        except Exception as e:
            print(f"记录收藏任务重试失败: {e}")
            return False


# 评分枚举
class JudgeScore:
    """评分枚举类"""
    HATE = 0      # 讨厌
    NEUTRAL = 1   # 中性
    LIKE = 2      # 有点感觉
    LOVE = 3      # 非常喜欢

    @staticmethod
    def get_label(score: int) -> str:
        """获取评分标签"""
        labels = {
            0: "讨厌",
            1: "中性",
            2: "有点感觉",
            3: "非常喜欢"
        }
        return labels.get(score, "未知")


# 测试代码
if __name__ == "__main__":
    # 初始化数据库
    db = DatasetDB()
    db.init_database()

    # 测试添加单张图片
    print("\n=== 测试添加单张图片 ===")
    db.add_image(12345678, 0, "12345678_p0.jpg")

    # 测试添加多张图片（一个作品）
    print("\n=== 测试添加多张图片 ===")
    filenames = [f"87654321_p{i}.jpg" for i in range(5)]
    db.add_images(87654321, filenames)

    # 测试评分
    print("\n=== 测试评分 ===")
    db.judge_image(12345678, 0, JudgeScore.LOVE)
    db.judge_image(87654321, 0, JudgeScore.LIKE)
    db.judge_image(87654321, 1, JudgeScore.NEUTRAL)

    # 测试查询
    print("\n=== 测试查询 ===")
    image = db.get_image_by_offset(0)
    print(f"第一张待评分图片: pid={image['pid']}, page={image['page_index']}")

    image = db.get_image_by_offset(-1)
    print(f"最近评分的图片: pid={image['pid']}, page={image['page_index']}, score={image['score']}")

    # 查询作品的所有图片
    images = db.get_images_by_pid(87654321)
    print(f"\n作品 87654321 的所有图片 ({len(images)} 张):")
    for img in images:
        print(f"  page={img['page_index']}, score={img['score']}, status={img['status']}")

    # 测试统计
    print("\n=== 测试统计 ===")
    stats = db.get_stats()
    print(f"总图片数: {stats['total_images']}")
    print(f"总作品数: {stats['total_works']}")
    print(f"待评分: {stats['wait_count']}")
    print(f"已评分: {stats['done_count']}")
    print(f"评分分布: {stats['score_distribution']}")

    # 测试导出训练数据
    print("\n=== 导出训练数据 ===")
    training_data = db.export_training_data()
    print(f"训练数据 ({len(training_data)} 条):")
    for pid, page, score in training_data:
        print(f"  pid={pid}, page={page}, score={score} ({JudgeScore.get_label(score)})")
