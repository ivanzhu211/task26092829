"""随机差分测试：与逐成员展开的内存参考模型逐步比对。"""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from notification_center import NotificationCenter, ROLES

TAGS = ["emea", "apac", "vip", "beta", "retail"]


class RefModel:
    """逐成员展开的参考模型：发送时物化接收者集合。"""

    def __init__(self):
        self.users = {}          # uid -> (role, set(tags))
        self.notifs = []         # (id, target, title, recipients:set[uid]) 按发送顺序
        self.read = {}           # uid -> set[notif_id]
        self._next_id = 1

    def set_user(self, uid, role, tags):
        self.users[uid] = (role, set(tags))

    def send(self, target, title):
        kind, val = target.split(":", 1)
        if kind == "user":
            if val not in self.users:
                raise KeyError(val)
            recipients = {val}
        elif kind == "role":
            recipients = {u for u, (r, _) in self.users.items() if r == val}
        else:
            recipients = {u for u, (_, ts) in self.users.items() if val in ts}
        nid = self._next_id
        self._next_id += 1
        self.notifs.append((nid, title, recipients))
        return nid

    def visible(self, uid):
        return [(nid, title) for nid, title, rec in self.notifs if uid in rec]

    def unread_count(self, uid):
        read = self.read.get(uid, set())
        return sum(1 for nid, _ in self.visible(uid) if nid not in read)

    def mark_read(self, uid, ids=None):
        read = self.read.setdefault(uid, set())
        if ids is None:
            read.update(nid for nid, _ in self.visible(uid))
        else:
            known = {nid for nid, _ in self.visible(uid)}
            read.update(i for i in ids if i in known)

    def inbox_titles(self, uid, unread_only=False):
        read = self.read.get(uid, set())
        out = []
        for nid, title in reversed(self.visible(uid)):
            if unread_only and nid in read:
                continue
            out.append(title)
        return out


def drain(nc, uid, unread_only=False, page=7, inject=None):
    """完整遍历 inbox；inject 为 (在第1页后执行的回调, 回调后模型侧忽略新通知)。"""
    titles = []
    cursor = None
    first = True
    while True:
        items, cursor = nc.inbox(uid, unread_only=unread_only, cursor=cursor,
                                 limit=page)
        titles.extend(i["title"] for i in items)
        if first and inject is not None:
            inject()
        first = False
        if cursor is None:
            return titles


class RandomDiffTest(unittest.TestCase):
    def run_seed(self, seed, steps=400):
        rng = random.Random(seed)
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        nc = NotificationCenter(path)
        model = RefModel()
        users = [f"u{i}" for i in range(12)]
        try:
            for step in range(steps):
                op = rng.random()
                if op < 0.30 or not model.users:
                    uid = rng.choice(users)
                    role = rng.choice(ROLES)
                    tags = [t for t in TAGS if rng.random() < 0.4]
                    nc.set_user(uid, role, tags)
                    model.set_user(uid, role, tags)
                elif op < 0.70:
                    kind = rng.choice(["user", "role", "tag"])
                    if kind == "user":
                        target = f"user:{rng.choice(users)}"
                    elif kind == "role":
                        target = f"role:{rng.choice(ROLES)}"
                    else:
                        target = f"tag:{rng.choice(TAGS)}"
                    title = f"s{seed}-{step}"
                    try:
                        nid = nc.send(target, title, "body")
                    except KeyError:
                        with self.assertRaises(KeyError):
                            model.send(target, title)
                        continue
                    mid = model.send(target, title)
                    self.assertEqual(nid, mid)
                elif op < 0.85 and model.users:
                    uid = rng.choice(users)
                    if uid not in model.users:
                        continue
                    if rng.random() < 0.5:
                        vis = model.visible(uid)
                        ids = [nid for nid, _ in vis
                               if rng.random() < 0.3]
                        # 混入不存在 / 不可见的 id，应被静默忽略
                        ids += [10 ** 9 + rng.randint(0, 5)]
                        nc.mark_read(uid, ids)
                        model.mark_read(uid, ids)
                    else:
                        nc.mark_read(uid)
                        model.mark_read(uid)
                elif model.users:
                    uid = rng.choice(users)
                    if uid not in model.users:
                        continue
                    unread_only = rng.random() < 0.5
                    injected = f"inject-{seed}-{step}"
                    def inject():
                        tgt = f"tag:{rng.choice(TAGS)}"
                        nc.send(tgt, injected, "b")
                        model.send(tgt, injected)
                    do_inject = rng.random() < 0.5
                    got = drain(nc, uid, unread_only=unread_only,
                                inject=inject if do_inject else None)
                    want = model.inbox_titles(uid, unread_only=unread_only)
                    if do_inject:
                        # 本次翻页途中新发的通知不应出现在遍历结果中
                        self.assertNotIn(injected, got)
                        want = [t for t in want if t != injected]
                    self.assertEqual(got, want,
                                     f"seed={seed} step={step} uid={uid}")
                for uid in list(model.users)[:4]:
                    self.assertEqual(
                        nc.unread_count(uid), model.unread_count(uid),
                        f"seed={seed} step={step} uid={uid}")
        finally:
            nc.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)

    def test_seeds(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                self.run_seed(seed)

    def test_reopen_persistence(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        nc = NotificationCenter(path)
        nc.set_user("u1", "Ambassador", ["emea"])
        nc.send("tag:emea", "hello", "b")
        nc.send("user:u1", "direct", "b")
        nc.mark_read("u1", [1])
        nc.close()
        nc2 = NotificationCenter(path)
        self.assertEqual(nc2.unread_count("u1"), 1)
        items, _ = nc2.inbox("u1")
        self.assertEqual([(i["title"], i["read"]) for i in items],
                         [("direct", False), ("hello", True)])
        nc2.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


if __name__ == "__main__":
    unittest.main()
