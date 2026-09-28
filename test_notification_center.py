"""Tests for NotificationCenter: randomized differential + scale tests."""

import os
import random
import tempfile
import unittest
from collections import defaultdict

from notification_center import NotificationCenter, ROLES

TAGS = ["emea", "apac", "vip", "beta"]


class RefModel:
    """Naive per-member reference model: expands every broadcast to its
    recipients at send time. Visibility is fixed at send time, so a user's
    inbox list only ever grows."""

    def __init__(self):
        self.users = {}  # uid -> (role, set(tags))
        self.notifs = {}  # id -> (title, body)
        self.inboxes = defaultdict(list)  # uid -> [notif ids] in send order
        self.read = defaultdict(set)  # uid -> {notif ids}
        self._next_id = 0

    def set_user(self, uid, role, tags):
        self.users[uid] = (role, set(tags))

    def send(self, target, title, body):
        kind, value = target.split(":", 1)
        if kind == "user" and value not in self.users:
            raise ValueError("unknown user")
        self._next_id += 1
        nid = self._next_id
        self.notifs[nid] = (title, body)
        for uid, (role, tags) in self.users.items():
            if (
                (kind == "user" and value == uid)
                or (kind == "role" and value == role)
                or (kind == "tag" and value in tags)
            ):
                self.inboxes[uid].append(nid)
        return nid

    def unread_count(self, uid):
        return sum(1 for nid in self.inboxes[uid] if nid not in self.read[uid])

    def mark_read(self, uid, ids=None):
        if ids is None:
            self.read[uid] |= set(self.inboxes[uid])
        else:
            self.read[uid] |= set(ids) & set(self.inboxes[uid])

    def inbox(self, uid, unread_only=False, cursor=None, limit=25):
        items = []
        for nid in reversed(self.inboxes[uid]):
            if cursor is not None and nid >= cursor:
                continue
            read = nid in self.read[uid]
            if unread_only and read:
                continue
            title, body = self.notifs[nid]
            items.append({"id": nid, "title": title, "body": body, "read": read})
            if len(items) == limit + 1:
                break
        next_cursor = items[limit - 1]["id"] if len(items) > limit else None
        return items[:limit], next_cursor


class RefModelTest(unittest.TestCase):
    def test_model_sanity(self):
        m = RefModel()
        m.set_user("u1", "Ambassador", {"emea"})
        m.send("tag:emea", "t", "b")
        self.assertEqual(m.unread_count("u1"), 1)
        items, cur = m.inbox("u1")
        self.assertEqual([i["title"] for i in items], ["t"])
        self.assertIsNone(cur)


class DifferentialTest(unittest.TestCase):
    """Interleave random membership changes, sends and read operations on
    the SQLite implementation and the per-member reference model; compare
    unread_count after every step and do lockstep cursor traversals with
    new sends injected mid-pagination."""

    def test_random_differential(self):
        for seed in range(3):
            with self.subTest(seed=seed):
                self._run_seed(seed)

    def _run_seed(self, seed):
        rng = random.Random(seed)
        center = NotificationCenter(":memory:")
        ref = RefModel()
        user_pool = [f"u{i}" for i in range(8)]
        created = []
        id_map = {}  # ref id -> real id
        send_count = 0

        def do_set_user():
            uid = rng.choice(user_pool)
            role = rng.choice(ROLES)
            tags = set(rng.sample(TAGS, rng.randint(0, len(TAGS))))
            center.set_user(uid, role, tags)
            ref.set_user(uid, role, tags)
            if uid not in created:
                created.append(uid)

        def do_send():
            nonlocal send_count
            kind = rng.choice(["user", "role", "tag"])
            if kind == "user":
                if not created:
                    do_set_user()
                target = "user:" + rng.choice(created)
            elif kind == "role":
                target = "role:" + rng.choice(ROLES)
            else:
                target = "tag:" + rng.choice(TAGS)
            send_count += 1
            title = f"n{send_count}"
            real_id = center.send(target, title, "body-" + title)
            ref_id = ref.send(target, title, "body-" + title)
            id_map[ref_id] = real_id

        def do_mark_some():
            if not created:
                return
            uid = rng.choice(created)
            visible = ref.inboxes[uid]
            picks = rng.sample(visible, min(len(visible), rng.randint(0, 4)))
            real_ids = [id_map[r] for r in picks]
            real_ids.append(10**9)  # bogus id: must be silently ignored
            center.mark_read(uid, real_ids)
            ref.mark_read(uid, picks + [10**9])

        def do_mark_all():
            if not created:
                return
            uid = rng.choice(created)
            center.mark_read(uid)
            ref.mark_read(uid)

        def do_traverse():
            if not created:
                return
            uid = rng.choice(created)
            unread_only = rng.random() < 0.5
            limit = rng.randint(1, 6)
            cur_real = cur_ref = None
            seen_real, seen_ref = [], []
            while True:
                page_r, cur_real = center.inbox(
                    uid, unread_only=unread_only, cursor=cur_real, limit=limit
                )
                page_f, cur_ref = ref.inbox(
                    uid, unread_only=unread_only, cursor=cur_ref, limit=limit
                )
                self.assertEqual(
                    [(i["title"], i["read"]) for i in page_r],
                    [(i["title"], i["read"]) for i in page_f],
                    f"page mismatch uid={uid} unread_only={unread_only}",
                )
                self.assertEqual(cur_real is None, cur_ref is None)
                seen_real.extend(page_r)
                seen_ref.extend(page_f)
                if cur_real is None:
                    break
                # Inject new sends mid-pagination: they must not appear in
                # later pages and must not disturb the traversal.
                for _ in range(rng.randint(0, 2)):
                    do_send()
            # No duplicates within a traversal.
            ids = [i["id"] for i in seen_real]
            self.assertEqual(len(ids), len(set(ids)))

        for _ in range(600):
            roll = rng.random()
            if roll < 0.30:
                do_set_user()
            elif roll < 0.65:
                do_send()
            elif roll < 0.78:
                do_mark_some()
            elif roll < 0.88:
                do_mark_all()
            else:
                do_traverse()
            for uid in created:
                self.assertEqual(
                    center.unread_count(uid),
                    ref.unread_count(uid),
                    f"unread_count mismatch for {uid}",
                )

        center.close()


class ScaleTest(unittest.TestCase):
    """20k members in one tag, 10k broadcasts: storage must not grow with
    members x broadcasts, and unread_count / first inbox page must stay
    correct (and cheap) at that scale."""

    MEMBERS = 20_000
    BROADCASTS = 10_000

    def test_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scale.db")
            center = NotificationCenter(path)

            for i in range(self.MEMBERS):
                center.set_user(f"m{i}", "Ambassador", ["emea"])

            for i in range(1, self.BROADCASTS + 1):
                center.send("tag:emea", f"b{i}", "body")

            # Storage: one row per broadcast, one interval per member.
            conn = center._conn
            counts = {
                name: conn.execute(
                    f"SELECT COUNT(*) FROM {name}"
                ).fetchone()[0]
                for name in ("broadcasts", "membership", "read_ids", "users")
            }
            self.assertEqual(counts["broadcasts"], self.BROADCASTS)
            self.assertEqual(counts["membership"], 2 * self.MEMBERS)  # role+tag
            self.assertEqual(counts["read_ids"], 0)
            total_rows = sum(counts.values())
            self.assertLess(
                total_rows,
                200_000,
                f"rows grew with members x broadcasts: {counts}",
            )

            # A member since the beginning sees all 10k as unread.
            self.assertEqual(center.unread_count("m0"), self.BROADCASTS)

            # First page: newest 25, strictly descending send order, unread.
            page, cursor = center.inbox("m0")
            self.assertEqual(len(page), 25)
            self.assertEqual(
                [item["title"] for item in page],
                [f"b{i}" for i in range(self.BROADCASTS, self.BROADCASTS - 25, -1)],
            )
            self.assertTrue(all(not item["read"] for item in page))
            self.assertIsNotNone(cursor)

            # A user who joined halfway only sees the second half.
            center.set_user("late", "Ambassador", [])
            center.set_user("late", "Ambassador", ["emea"])
            self.assertEqual(center.unread_count("late"), 0)

            # A user who left the tag keeps what was received before leaving.
            center.set_user("gone", "Ambassador", ["emea"])
            center.send("tag:emea", "extra", "body")
            center.set_user("gone", "Ambassador", [])
            center.send("tag:emea", "after-leave", "body")
            self.assertEqual(center.unread_count("gone"), 1)

            # mark_read(ids=None) writes no per-notification rows.
            center.mark_read("m0")
            self.assertEqual(center.unread_count("m0"), 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM read_ids").fetchone()[0], 0
            )
            # New broadcasts after mark-all are unread again.
            center.send("tag:emea", "fresh", "body")
            self.assertEqual(center.unread_count("m0"), 1)

            # State survives close + reopen of the same file.
            center.close()
            center = NotificationCenter(path)
            self.assertEqual(center.unread_count("m0"), 1)
            self.assertEqual(center.unread_count("m1"), self.BROADCASTS + 3)
            page, _ = center.inbox("m0", unread_only=True)
            self.assertEqual([item["title"] for item in page], ["fresh"])
            center.close()


class ApiEdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.center = NotificationCenter(":memory:")
        self.center.set_user("u1", "Ambassador", ["emea"])

    def tearDown(self):
        self.center.close()

    def test_send_to_unknown_user_raises(self):
        with self.assertRaises(ValueError):
            self.center.send("user:ghost", "t", "b")

    def test_invalid_target_raises(self):
        for bad in ["", "u1", "group:emea", "role:Superuser", "tag:"]:
            with self.assertRaises(ValueError, msg=bad):
                self.center.send(bad, "t", "b")

    def test_invalid_role_raises(self):
        with self.assertRaises(ValueError):
            self.center.set_user("u2", "Superuser", [])

    def test_unknown_user_operations_raise(self):
        with self.assertRaises(ValueError):
            self.center.inbox("ghost")
        with self.assertRaises(ValueError):
            self.center.unread_count("ghost")
        with self.assertRaises(ValueError):
            self.center.mark_read("ghost")

    def test_limit_clamped_to_100(self):
        for i in range(150):
            self.center.send("tag:emea", f"t{i}", "b")
        page, cursor = self.center.inbox("u1", limit=500)
        self.assertEqual(len(page), 100)
        self.assertIsNotNone(cursor)
        page2, cursor2 = self.center.inbox("u1", cursor=cursor, limit=500)
        self.assertEqual(len(page2), 50)
        self.assertIsNone(cursor2)

    def test_new_send_does_not_appear_mid_pagination(self):
        for i in range(10):
            self.center.send("tag:emea", f"t{i}", "b")
        page1, cursor = self.center.inbox("u1", limit=5)
        self.center.send("tag:emea", "late", "b")
        page2, cursor2 = self.center.inbox("u1", cursor=cursor, limit=5)
        titles = [i["title"] for i in page1 + page2]
        self.assertEqual(titles, [f"t{i}" for i in range(9, -1, -1)])
        self.assertIsNone(cursor2)

    def test_rejoin_tag_sees_only_new(self):
        self.center.send("tag:emea", "old", "b")
        self.center.set_user("u1", "Ambassador", [])
        self.center.set_user("u1", "Ambassador", ["emea"])
        self.center.send("tag:emea", "new", "b")
        page, _ = self.center.inbox("u1", unread_only=True)
        # "old" was visible during the first membership and stays visible.
        self.assertEqual([i["title"] for i in page], ["new", "old"])


if __name__ == "__main__":
    unittest.main()
