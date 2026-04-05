"""
Pixiv 图片评分系统 - 数据库操作模块

提供数据库初始化和 CRUD 操作功能
按图片评分（一个作品可能包含多张图片，每张图片单独评分）
"""

import sqlite3
from datetime import datetime
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

        print("数据库初始化完成")

    def add_image(self, pid: int, page_index: int, local_filename: str,
                  fetched_at: Optional[str] = None) -> bool:
        """
        添加单张图片

        Args:
            pid: Pixiv 作品 ID
            page_index: 页码索引（从 0 开始）
            local_filename: 本地文件名
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
                    INSERT INTO images (pid, page_index, local_filename, fetched_at, status)
                    VALUES (?, ?, ?, ?, 'wait')
                    """,
                    (pid, page_index, local_filename, fetched_at)
                )
            print(f"添加图片成功: pid={pid}, page={page_index}, filename={local_filename}")
            return True
        except sqlite3.IntegrityError:
            print(f"图片已存在: pid={pid}, page={page_index}")
            return False
        except Exception as e:
            print(f"添加图片失败: {e}")
            return False

    def add_images(self, pid: int, filenames: list[str], fetched_at: Optional[str] = None) -> int:
        """
        批量添加一个作品的多张图片

        Args:
            pid: Pixiv 作品 ID
            filenames: 文件名列表（按页码顺序）
            fetched_at: 拉取时间（ISO 8601 格式），默认为当前时间

        Returns:
            int: 成功添加的图片数量
        """
        if fetched_at is None:
            fetched_at = datetime.now().isoformat()

        success_count = 0
        for page_index, filename in enumerate(filenames):
            if self.add_image(pid, page_index, filename, fetched_at):
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
