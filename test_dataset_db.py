"""
数据库模块完整测试（按图片评分版本）
测试所有功能和边界情况
"""

import os
from pathlib import Path
from dataset_db import DatasetDB, JudgeScore


def cleanup_test_db():
    """清理测试数据库"""
    test_db = Path("test_dataset.db")
    if test_db.exists():
        test_db.unlink()
        print("已清理旧的测试数据库")


def test_init_database():
    """测试数据库初始化"""
    print("\n" + "="*50)
    print("测试 1: 数据库初始化")
    print("="*50)

    db = DatasetDB("test_dataset.db")
    db.init_database()

    # 验证数据库文件已创建
    assert Path("test_dataset.db").exists(), "数据库文件未创建"
    print("[OK] 数据库文件创建成功")

    # 再次初始化应该不报错
    db.init_database()
    print("[OK] 重复初始化不报错")

    return db


def test_add_single_image(db: DatasetDB):
    """测试添加单张图片"""
    print("\n" + "="*50)
    print("测试 2: 添加单张图片")
    print("="*50)

    # 正常添加
    assert db.add_image(100001, 0, "100001_p0.jpg"), "添加图片失败"
    print("[OK] 添加单张图片成功")

    # 重复添加应该失败
    assert not db.add_image(100001, 0, "100001_p0.jpg"), "重复添加应该失败"
    print("[OK] 重复添加正确拒绝")


def test_add_multiple_images(db: DatasetDB):
    """测试添加多张图片（一个作品）"""
    print("\n" + "="*50)
    print("测试 3: 添加多张图片")
    print("="*50)

    # 批量添加一个作品的多张图片
    filenames = [f"100002_p{i}.jpg" for i in range(5)]
    count = db.add_images(100002, filenames)
    assert count == 5, f"应该添加 5 张图片，实际添加了 {count} 张"
    print("[OK] 批量添加 5 张图片成功")

    # 验证图片都已添加
    images = db.get_images_by_pid(100002)
    assert len(images) == 5, f"应该有 5 张图片，实际有 {len(images)} 张"
    print("[OK] 验证图片数量正确")

    # 添加更多单图作品
    for i in range(3, 11):
        pid = 100000 + i
        db.add_image(pid, 0, f"{pid}_p0.jpg")
    print("[OK] 添加更多单图作品成功")


def test_judge_image(db: DatasetDB):
    """测试评分功能"""
    print("\n" + "="*50)
    print("测试 4: 评分功能")
    print("="*50)

    # 正常评分
    assert db.judge_image(100001, 0, JudgeScore.LOVE), "评分失败"
    print("[OK] 评分成功")

    # 验证状态已更新
    image = db.get_image_by_pid_page(100001, 0)
    assert image['score'] == 3, "评分未正确保存"
    assert image['status'] == 'done', "状态未更新为 done"
    assert image['judged_at'] is not None, "评分时间未记录"
    print("[OK] 评分数据正确保存")

    # 重复评分应该失败（已评分的图片）
    assert not db.judge_image(100001, 0, JudgeScore.HATE), "重复评分应该失败"
    print("[OK] 重复评分正确拒绝")

    # 无效评分
    assert not db.judge_image(100002, 0, 5), "无效评分应该失败"
    print("[OK] 无效评分正确拒绝")

    # 不存在的图片
    assert not db.judge_image(999999, 0, JudgeScore.LOVE), "不存在的图片评分应该失败"
    print("[OK] 不存在的图片评分正确拒绝")

    # 评分多图作品的不同图片
    db.judge_image(100002, 0, JudgeScore.LOVE)
    db.judge_image(100002, 1, JudgeScore.LIKE)
    db.judge_image(100002, 2, JudgeScore.NEUTRAL)
    db.judge_image(100002, 3, JudgeScore.HATE)
    # page 4 不评分，留作待评分
    print("[OK] 多图作品的不同图片评分成功")

    # 评分其他单图作品
    db.judge_image(100003, 0, JudgeScore.LIKE)
    db.judge_image(100004, 0, JudgeScore.NEUTRAL)
    print("[OK] 其他作品评分成功")


def test_update_score(db: DatasetDB):
    """测试修改评分"""
    print("\n" + "="*50)
    print("测试 5: 修改评分")
    print("="*50)

    # 修改已有评分
    assert db.update_score(100001, 0, JudgeScore.LIKE), "修改评分失败"
    image = db.get_image_by_pid_page(100001, 0)
    assert image['score'] == 2, "评分未正确修改"
    print("[OK] 修改评分成功")

    # 修改不存在的图片
    assert not db.update_score(999999, 0, JudgeScore.LOVE), "修改不存在的图片应该失败"
    print("[OK] 修改不存在的图片正确拒绝")


def test_update_status(db: DatasetDB):
    """测试修改状态"""
    print("\n" + "="*50)
    print("测试 6: 修改状态")
    print("="*50)

    # 修改状态
    assert db.update_status(100005, 0, 'deleted'), "修改状态失败"
    image = db.get_image_by_pid_page(100005, 0)
    assert image['status'] == 'deleted', "状态未正确修改"
    print("[OK] 修改状态成功")

    # 无效状态
    assert not db.update_status(100005, 0, 'invalid'), "无效状态应该失败"
    print("[OK] 无效状态正确拒绝")


def test_get_image_by_offset(db: DatasetDB):
    """测试按 offset 查询"""
    print("\n" + "="*50)
    print("测试 7: 按 offset 查询")
    print("="*50)

    # 获取第一张待评分图片
    image = db.get_image_by_offset(0)
    assert image is not None, "应该有待评分图片"
    assert image['score'] is None, "应该是未评分图片"
    print(f"[OK] 第一张待评分图片: pid={image['pid']}, page={image['page_index']}")

    # 获取第二张待评分图片
    image = db.get_image_by_offset(1)
    assert image is not None, "应该有第二张待评分图片"
    print(f"[OK] 第二张待评分图片: pid={image['pid']}, page={image['page_index']}")

    # 获取最近评分的图片
    image = db.get_image_by_offset(-1)
    assert image is not None, "应该有已评分图片"
    assert image['score'] is not None, "应该是已评分图片"
    print(f"[OK] 最近评分的图片: pid={image['pid']}, page={image['page_index']}, score={image['score']}")

    # 获取倒数第二张
    image = db.get_image_by_offset(-2)
    assert image is not None, "应该有倒数第二张"
    print(f"[OK] 倒数第二张: pid={image['pid']}, page={image['page_index']}, score={image['score']}")

    # 超出范围
    image = db.get_image_by_offset(999)
    assert image is None, "超出范围应该返回 None"
    print("[OK] 超出范围正确返回 None")


def test_get_images_by_pid(db: DatasetDB):
    """测试查询作品的所有图片"""
    print("\n" + "="*50)
    print("测试 8: 查询作品的所有图片")
    print("="*50)

    # 查询多图作品
    images = db.get_images_by_pid(100002)
    assert len(images) == 5, f"作品 100002 应该有 5 张图片，实际有 {len(images)} 张"
    print(f"[OK] 作品 100002 有 5 张图片")

    # 验证图片按 page_index 排序
    for i, img in enumerate(images):
        assert img['page_index'] == i, f"图片顺序错误，期望 page={i}，实际 page={img['page_index']}"
    print("[OK] 图片按 page_index 正确排序")

    # 验证评分状态
    assert images[0]['score'] == 3, "page 0 应该评分为 3"
    assert images[1]['score'] == 2, "page 1 应该评分为 2"
    assert images[2]['score'] == 1, "page 2 应该评分为 1"
    assert images[3]['score'] == 0, "page 3 应该评分为 0"
    assert images[4]['score'] is None, "page 4 应该未评分"
    print("[OK] 各图片评分状态正确")

    # 查询单图作品
    images = db.get_images_by_pid(100001)
    assert len(images) == 1, "单图作品应该只有 1 张图片"
    print("[OK] 单图作品查询正确")


def test_get_stats(db: DatasetDB):
    """测试统计功能"""
    print("\n" + "="*50)
    print("测试 9: 统计功能")
    print("="*50)

    stats = db.get_stats()

    print(f"总图片数: {stats['total_images']}")
    print(f"总作品数: {stats['total_works']}")
    print(f"待评分: {stats['wait_count']}")
    print(f"已评分: {stats['done_count']}")
    print(f"已删除: {stats['deleted_count']}")
    print(f"评分分布: {stats['score_distribution']}")

    # 验证统计数据
    # 10 个作品：100001-100010
    # 100001: 1 张（已评分）
    # 100002: 5 张（4 张已评分，1 张待评分）
    # 100003-100004: 各 1 张（已评分）
    # 100005: 1 张（已删除）
    # 100006-100010: 各 1 张（待评分）
    # 总计：14 张图片，10 个作品
    # 已评分：100001(1) + 100002(4) + 100003(1) + 100004(1) = 7 张
    assert stats['total_images'] == 14, f"总图片数应该是 14，实际是 {stats['total_images']}"
    assert stats['total_works'] == 10, f"总作品数应该是 10，实际是 {stats['total_works']}"
    assert stats['judged_count'] == 7, f"已评分应该是 7，实际是 {stats['judged_count']}"
    print("[OK] 统计数据正确")


def test_score_distribution(db: DatasetDB):
    """测试评分分布"""
    print("\n" + "="*50)
    print("测试 10: 评分分布")
    print("="*50)

    dist = db.get_score_distribution()

    print("\n评分分布:")
    for item in dist:
        print(f"  {item['score_label']}: {item['count']} ({item['percentage']}%)")

    assert len(dist) > 0, "应该有评分分布数据"
    print("[OK] 评分分布查询成功")


def test_export_training_data(db: DatasetDB):
    """测试导出训练数据"""
    print("\n" + "="*50)
    print("测试 11: 导出训练数据")
    print("="*50)

    data = db.export_training_data()

    print(f"\n训练数据 ({len(data)} 条):")
    for pid, page, score in data[:5]:  # 只显示前 5 条
        print(f"  pid={pid}, page={page}, score={score} ({JudgeScore.get_label(score)})")

    assert len(data) == 7, f"应该有 7 条训练数据，实际有 {len(data)} 条"
    assert all(isinstance(pid, int) and isinstance(page, int) and isinstance(score, int)
               for pid, page, score in data), "数据格式错误"
    print("[OK] 训练数据导出成功")


def test_cleanup_images(db: DatasetDB):
    """测试清理功能"""
    print("\n" + "="*50)
    print("测试 12: 清理功能")
    print("="*50)

    # 添加更多已评分图片
    for i in range(11, 21):
        pid = 100000 + i
        db.add_image(pid, 0, f"{pid}_p0.jpg")
        db.judge_image(pid, 0, JudgeScore.LIKE)

    # 获取需要清理的图片（保留 5 张）
    to_cleanup = db.get_images_to_cleanup(keep_count=5)

    print(f"\n需要清理的图片数: {len(to_cleanup)}")
    print("需要清理的图片:")
    for img in to_cleanup[:3]:  # 只显示前 3 个
        print(f"  pid={img['pid']}, page={img['page_index']}, judged_at={img['judged_at']}")

    # 验证清理逻辑
    stats = db.get_stats()
    total_done = stats['done_count']
    expected_cleanup = max(0, total_done - 5)
    assert len(to_cleanup) == expected_cleanup, \
        f"清理数量不正确，期望 {expected_cleanup}，实际 {len(to_cleanup)}"
    print(f"[OK] 清理逻辑正确（保留 5 张，清理 {len(to_cleanup)} 张）")


def test_edge_cases(db: DatasetDB):
    """测试边界情况"""
    print("\n" + "="*50)
    print("测试 13: 边界情况")
    print("="*50)

    # 查询不存在的图片
    image = db.get_image_by_pid_page(999999, 0)
    assert image is None, "不存在的图片应该返回 None"
    print("[OK] 查询不存在的图片正确返回 None")

    # 查询不存在的作品
    images = db.get_images_by_pid(999999)
    assert len(images) == 0, "不存在的作品应该返回空列表"
    print("[OK] 查询不存在的作品正确返回空列表")

    # 空数据库的统计
    db2 = DatasetDB("empty_test.db")
    db2.init_database()
    stats = db2.get_stats()
    assert stats['total_images'] == 0, "空数据库图片数应该是 0"
    assert stats['total_works'] == 0, "空数据库作品数应该是 0"
    print("[OK] 空数据库统计正确")

    # 清理空数据库
    Path("empty_test.db").unlink()
    print("[OK] 边界情况测试通过")


def test_multi_page_work_scenarios(db: DatasetDB):
    """测试多图作品的各种场景"""
    print("\n" + "="*50)
    print("测试 14: 多图作品场景")
    print("="*50)

    # 添加一个 10 张图片的作品
    pid = 200001
    filenames = [f"{pid}_p{i}.jpg" for i in range(10)]
    db.add_images(pid, filenames)
    print("[OK] 添加 10 张图片的作品")

    # 只评分部分图片
    db.judge_image(pid, 0, JudgeScore.LOVE)
    db.judge_image(pid, 5, JudgeScore.LIKE)
    db.judge_image(pid, 9, JudgeScore.HATE)
    print("[OK] 评分部分图片")

    # 验证统计
    images = db.get_images_by_pid(pid)
    judged = [img for img in images if img['score'] is not None]
    unjudged = [img for img in images if img['score'] is None]
    assert len(judged) == 3, "应该有 3 张已评分"
    assert len(unjudged) == 7, "应该有 7 张未评分"
    print(f"[OK] 作品 {pid}: 3 张已评分，7 张未评分")

    # 验证待评分图片的顺序（应该按 page_index 排序）
    wait_images = [img for img in images if img['status'] == 'wait']
    for i in range(len(wait_images) - 1):
        assert wait_images[i]['page_index'] < wait_images[i+1]['page_index'], \
            "待评分图片应该按 page_index 排序"
    print("[OK] 待评分图片按 page_index 正确排序")


def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("开始运行完整测试套件（按图片评分版本）")
    print("="*60)

    # 清理旧数据
    cleanup_test_db()

    try:
        # 运行测试
        db = test_init_database()
        test_add_single_image(db)
        test_add_multiple_images(db)
        test_judge_image(db)
        test_update_score(db)
        test_update_status(db)
        test_get_image_by_offset(db)
        test_get_images_by_pid(db)
        test_get_stats(db)
        test_score_distribution(db)
        test_export_training_data(db)
        test_cleanup_images(db)
        test_edge_cases(db)
        test_multi_page_work_scenarios(db)

        print("\n" + "="*60)
        print("[OK] 所有测试通过！")
        print("="*60)

    except AssertionError as e:
        print(f"\n[FAIL] 测试失败: {e}")
        raise
    except Exception as e:
        print(f"\n[FAIL] 测试出错: {e}")
        raise
    finally:
        # 清理测试数据库
        cleanup_test_db()
        print("\n已清理测试数据库")


if __name__ == "__main__":
    run_all_tests()
