"""规模测试：2 万成员的标签发 1 万条广播。

断言库内行数不随 成员数×广播数 增长，并验证该规模下
unread_count 与 inbox 首页结果。
"""

import os
import tempfile
import time
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from notification_center import NotificationCenter

MEMBERS = 20_000
BROADCASTS = 10_000


class ScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.path)
        nc = NotificationCenter(cls.path)
        t0 = time.time()
        for i in range(MEMBERS):
            nc.set_user(f"m{i}", "Ambassador", ["big"])
        # 一个中途加入的成员：第 5000 条广播后才入群
        late_join_seq_point = None
        for j in range(BROADCASTS):
            if j == BROADCASTS // 2:
                nc.set_user("late", "Ambassador", ["big"])
            nc.send("tag:big", f"b{j}", "x")
        nc.send("user:m0", "direct", "x")
        cls.setup_seconds = time.time() - t0
        cls.nc = nc

    @classmethod
    def tearDownClass(cls):
        cls.nc.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cls.path + suffix):
                os.remove(cls.path + suffix)

    def test_1_row_counts_not_member_times_broadcast(self):
        cur = self.nc._conn
        (n_notif,) = cur.execute("SELECT COUNT(*) FROM notifications").fetchone()
        (n_reads,) = cur.execute("SELECT COUNT(*) FROM reads").fetchone()
        (n_intervals,) = cur.execute("SELECT COUNT(*) FROM intervals").fetchone()
        (n_users,) = cur.execute("SELECT COUNT(*) FROM users").fetchone()
        # 广播每条一行：1 万条广播 + 1 条直发
        self.assertEqual(n_notif, BROADCASTS + 1)
        self.assertEqual(n_reads, 0)
        # 成员关系行与用户数同阶，与广播数无关
        self.assertLessEqual(n_intervals, 4 * (MEMBERS + 1))
        total = n_notif + n_reads + n_intervals + n_users
        self.assertLess(total, 200_000)  # 远低于 2万×1万=2亿

    def test_2_unread_count_at_scale(self):
        t0 = time.time()
        self.assertEqual(self.nc.unread_count("m0"), BROADCASTS + 1)
        self.assertEqual(self.nc.unread_count(f"m{MEMBERS - 1}"), BROADCASTS)
        # 中途加入者只看到加入后发出的 5000 条
        self.assertEqual(self.nc.unread_count("late"), BROADCASTS // 2)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0, "unread_count 应接近常数时间")

    def test_3_inbox_first_page_at_scale(self):
        t0 = time.time()
        items, cursor = self.nc.inbox("m0", limit=25)
        elapsed = time.time() - t0
        self.assertEqual(len(items), 25)
        # 最新的是直发，其后是倒序广播
        self.assertEqual(items[0]["title"], "direct")
        self.assertEqual([i["title"] for i in items[1:]],
                         [f"b{j}" for j in range(BROADCASTS - 1,
                                                 BROADCASTS - 25, -1)])
        self.assertTrue(all(not i["read"] for i in items))
        self.assertIsNotNone(cursor)
        # 第二页接续第一页，无跳过无重复
        items2, _ = self.nc.inbox("m0", cursor=cursor, limit=25)
        self.assertEqual([i["title"] for i in items2],
                         [f"b{j}" for j in range(BROADCASTS - 25,
                                                 BROADCASTS - 50, -1)])
        self.assertLess(elapsed, 1.0, "inbox 首页应接近常数时间")

    def test_4_mark_all_then_new_broadcast(self):
        self.nc.mark_read("late")
        self.assertEqual(self.nc.unread_count("late"), 0)
        (n_reads,) = self.nc._conn.execute(
            "SELECT COUNT(*) FROM reads WHERE user_id='late'").fetchone()
        self.assertEqual(n_reads, 0)  # 全部已读不写逐条记录
        self.nc.send("tag:big", "fresh", "x")
        self.assertEqual(self.nc.unread_count("late"), 1)
        items, _ = self.nc.inbox("late", unread_only=True)
        self.assertEqual([i["title"] for i in items], ["fresh"])


if __name__ == "__main__":
    unittest.main()
