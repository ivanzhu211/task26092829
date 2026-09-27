"""员工品牌大使平台站内通知中心。

以数据库文件路径构造。规模按 10 万用户 / 百万级累计广播设计：
- send 写入量 O(1)，与目标成员数无关（广播只写一行）。
- 可见性通过成员关系区间 [join_seq, leave_seq) 在读取侧判定。
- unread_count / inbox 单页代价与成员数、历史通知总数无关。
"""

from __future__ import annotations

import heapq
import sqlite3
from typing import Iterable, Optional

ROLES = ("Admin", "ProgramManager", "Ambassador")

_INF = 1 << 62

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_tags (
    user_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (user_id, tag)
);
CREATE TABLE IF NOT EXISTS intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    join_seq INTEGER NOT NULL,
    leave_seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_intervals_user ON intervals(user_id, key);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL UNIQUE,
    target_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_key_seq ON notifications(target_key, seq);
CREATE TABLE IF NOT EXISTS reads (
    user_id TEXT NOT NULL,
    notif_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (user_id, notif_id)
);
CREATE TABLE IF NOT EXISTS watermarks (
    user_id TEXT PRIMARY KEY,
    read_seq INTEGER NOT NULL
);
"""


class NotificationCenter:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute(
            "SELECT v FROM meta WHERE k='seq'"
        ).fetchone()
        if row is None:
            self._seq = 0
            self._conn.execute("INSERT INTO meta (k, v) VALUES ('seq', 0)")
            self._conn.commit()
        else:
            self._seq = row[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NotificationCenter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 内部工具 ----

    def _bump_seq(self) -> int:
        self._seq += 1
        self._conn.execute("UPDATE meta SET v=? WHERE k='seq'", (self._seq,))
        return self._seq

    def _current_keys(self, user_id: str) -> Optional[set[str]]:
        row = self._conn.execute(
            "SELECT role FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        keys = {f"role:{row[0]}"}
        for (tag,) in self._conn.execute(
            "SELECT tag FROM user_tags WHERE user_id=?", (user_id,)
        ):
            keys.add(f"tag:{tag}")
        return keys

    # ---- 写操作 ----

    def set_user(self, user_id: str, role: str, tags: Iterable[str]) -> None:
        """创建用户，或整体替换其角色和标签。"""
        if role not in ROLES:
            raise ValueError(f"invalid role: {role!r}")
        new_keys = {f"role:{role}"} | {f"tag:{t}" for t in set(tags)}
        old_keys = self._current_keys(user_id)
        if old_keys is not None and old_keys == new_keys:
            return
        seq = self._bump_seq()
        cur = self._conn
        if old_keys is None:
            cur.execute(
                "INSERT INTO users (user_id, role) VALUES (?, ?)",
                (user_id, role),
            )
        else:
            cur.execute(
                "UPDATE users SET role=? WHERE user_id=?", (role, user_id)
            )
            for key in old_keys - new_keys:
                cur.execute(
                    "UPDATE intervals SET leave_seq=? "
                    "WHERE user_id=? AND key=? AND leave_seq IS NULL",
                    (seq, user_id, key),
                )
        cur.execute("DELETE FROM user_tags WHERE user_id=?", (user_id,))
        cur.executemany(
            "INSERT INTO user_tags (user_id, tag) VALUES (?, ?)",
            [(user_id, t) for t in set(tags)],
        )
        for key in new_keys - (old_keys or set()):
            cur.execute(
                "INSERT INTO intervals (user_id, key, join_seq) VALUES (?, ?, ?)",
                (user_id, key, seq),
            )
        cur.commit()

    def send(self, target: str, title: str, body: str) -> int:
        """target 形如 'user:u1' / 'role:Ambassador' / 'tag:emea'，返回通知 id。"""
        kind, sep, val = target.partition(":")
        if not sep or not val:
            raise ValueError(f"invalid target: {target!r}")
        if kind == "user":
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE user_id=?", (val,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown user: {val!r}")
        elif kind == "role":
            if val not in ROLES:
                raise ValueError(f"invalid role: {val!r}")
        elif kind != "tag":
            raise ValueError(f"invalid target kind: {kind!r}")
        seq = self._bump_seq()
        row = self._conn.execute(
            "SELECT ordinal FROM notifications WHERE target_key=? "
            "ORDER BY seq DESC LIMIT 1",
            (target,),
        ).fetchone()
        ordinal = (row[0] if row else 0) + 1
        cur = self._conn.execute(
            "INSERT INTO notifications (seq, target_key, ordinal, title, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (seq, target, ordinal, title, body),
        )
        self._conn.commit()
        return cur.lastrowid

    # ---- 可见性 ----

    def _watermark(self, user_id: str) -> int:
        row = self._conn.execute(
            "SELECT read_seq FROM watermarks WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else 0

    def _intervals(self, user_id: str) -> list[tuple[str, int, int]]:
        return [
            (key, join, leave if leave is not None else _INF)
            for key, join, leave in self._conn.execute(
                "SELECT key, join_seq, leave_seq FROM intervals WHERE user_id=?",
                (user_id,),
            )
        ]

    def _ord_at(self, key: str, seq: int) -> int:
        """key 上 seq < 给定值的广播条数（前缀计数，O(log)）。"""
        row = self._conn.execute(
            "SELECT ordinal FROM notifications WHERE target_key=? AND seq<? "
            "ORDER BY seq DESC LIMIT 1",
            (key, seq),
        ).fetchone()
        return row[0] if row else 0

    def _count_key(self, key: str, lo: int, hi: int) -> int:
        """key 上 lo <= seq < hi 的条数。"""
        if lo >= hi:
            return 0
        return self._ord_at(key, hi) - self._ord_at(key, lo)

    def _visible_count(self, user_id: str, after_seq: int) -> int:
        """seq > after_seq 的可见通知条数。"""
        total = self._count_key(f"user:{user_id}", after_seq + 1, _INF)
        for key, join, leave in self._intervals(user_id):
            total += self._count_key(key, max(after_seq + 1, join), leave)
        return total

    def _is_visible(self, user_id: str, target_key: str, seq: int) -> bool:
        if target_key == f"user:{user_id}":
            return True
        if not target_key.startswith(("role:", "tag:")):
            return False
        row = self._conn.execute(
            "SELECT 1 FROM intervals WHERE user_id=? AND key=? "
            "AND join_seq<=? AND (leave_seq IS NULL OR leave_seq>?) LIMIT 1",
            (user_id, target_key, seq, seq),
        ).fetchone()
        return row is not None

    def _is_read(self, user_id: str, notif_id: int, seq: int, wm: int) -> bool:
        if seq <= wm:
            return True
        row = self._conn.execute(
            "SELECT 1 FROM reads WHERE user_id=? AND notif_id=?",
            (user_id, notif_id),
        ).fetchone()
        return row is not None

    # ---- 读操作 ----

    def unread_count(self, user_id: str) -> int:
        wm = self._watermark(user_id)
        total = self._visible_count(user_id, wm)
        (read_rows,) = self._conn.execute(
            "SELECT COUNT(*) FROM reads WHERE user_id=?", (user_id,)
        ).fetchone()
        return total - read_rows

    def _stream(self, key: str, lo: int, hi: int, cursor_seq: int):
        """按 seq 从新到旧惰性产出 [lo, min(hi, cursor_seq)) 内的通知行。"""
        upper = min(hi, cursor_seq)
        while lo < upper:
            rows = self._conn.execute(
                "SELECT id, seq, title, body, target_key FROM notifications "
                "WHERE target_key=? AND seq>=? AND seq<? "
                "ORDER BY seq DESC LIMIT 200",
                (key, lo, upper),
            ).fetchall()
            if not rows:
                return
            yield from rows
            upper = rows[-1][1]

    def inbox(
        self,
        user_id: str,
        unread_only: bool = False,
        cursor: Optional[str] = None,
        limit: int = 25,
    ) -> tuple[list[dict], Optional[str]]:
        """按发送先后新到旧返回一页条目和下一页游标。"""
        limit = max(0, min(int(limit), 100))
        if limit == 0:
            return ([], None)
        cursor_seq = int(cursor) if cursor is not None else _INF
        wm = self._watermark(user_id)
        streams = [
            self._stream(f"user:{user_id}", 0, _INF, cursor_seq)
        ]
        for key, join, leave in self._intervals(user_id):
            streams.append(self._stream(key, join, leave, cursor_seq))
        merged = heapq.merge(*streams, key=lambda r: -r[1])
        items: list[dict] = []
        last_seq: Optional[int] = None
        for notif_id, seq, title, body, target_key in merged:
            read = self._is_read(user_id, notif_id, seq, wm)
            if unread_only and read:
                continue
            items.append(
                {"id": notif_id, "title": title, "body": body, "read": read}
            )
            last_seq = seq
            if len(items) == limit:
                break
        next_cursor: Optional[str] = None
        if len(items) == limit:
            for notif_id, seq, _t, _b, _k in merged:
                if unread_only and self._is_read(user_id, notif_id, seq, wm):
                    continue
                next_cursor = str(last_seq)
                break
        return (items, next_cursor)

    # ---- 已读 ----

    def mark_read(self, user_id: str, ids: Optional[Iterable[int]] = None) -> None:
        """ids 为 None 表示把调用时刻可见的全部标为已读。"""
        cur = self._conn
        if ids is None:
            wm = self._seq
            cur.execute(
                "INSERT INTO watermarks (user_id, read_seq) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET read_seq=excluded.read_seq",
                (user_id, wm),
            )
            cur.execute(
                "DELETE FROM reads WHERE user_id=? AND seq<=?",
                (user_id, wm),
            )
            cur.commit()
            return
        wm = self._watermark(user_id)
        for notif_id in ids:
            row = cur.execute(
                "SELECT seq, target_key FROM notifications WHERE id=?",
                (notif_id,),
            ).fetchone()
            if row is None:
                continue
            seq, target_key = row
            if seq <= wm:
                continue
            if not self._is_visible(user_id, target_key, seq):
                continue
            cur.execute(
                "INSERT OR IGNORE INTO reads (user_id, notif_id, seq) "
                "VALUES (?, ?, ?)",
                (user_id, notif_id, seq),
            )
        cur.commit()
