"""In-app notification center backed by SQLite (stdlib sqlite3 only).

Design notes (scale: 100k users, broadcasts with 100k+ recipients):

- A send writes exactly ONE row, regardless of audience size. Broadcasts
  are never fanned out per member, neither at send time nor at read time.
- Visibility is captured by membership intervals: each (user, kind, value)
  membership is stored as a half-open interval over a global event
  sequence. A broadcast with seq s is visible to a member iff the member
  has an interval with lo < s and (hi IS NULL or s < hi).
- Read state is a per-user watermark (everything with seq <= watermark is
  read) plus a small table of individually-read ids. mark_read(ids=None)
  only bumps the watermark; it writes no per-notification rows.
- Per-target cumulative broadcast counts make unread_count an O(log n)
  lookup per membership interval instead of a range scan.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

ROLES = ("Admin", "ProgramManager", "Ambassador")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS membership (
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    lo INTEGER NOT NULL,
    hi INTEGER
);
CREATE INDEX IF NOT EXISTS idx_membership_user
    ON membership(user_id, kind, value);
CREATE TABLE IF NOT EXISTS broadcasts (
    seq INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    cum INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broadcasts_target
    ON broadcasts(kind, value, seq);
CREATE TABLE IF NOT EXISTS read_ids (
    user_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (user_id, seq)
);
CREATE TABLE IF NOT EXISTS read_state (
    user_id TEXT PRIMARY KEY,
    watermark INTEGER NOT NULL
);
"""


class NotificationCenter:
    """A notification center persisted in a single SQLite database file."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('seq', 0)"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NotificationCenter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internal helpers ----------------------------------------------------

    def _next_seq(self) -> int:
        self._conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'seq'")
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
        return row[0]

    def _current_seq(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
        return row[0]

    def _require_user(self, user_id: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown user: {user_id!r}")

    def _open_interval(self, user_id: str, kind: str, value: str, lo: int) -> None:
        self._conn.execute(
            "INSERT INTO membership(user_id, kind, value, lo, hi) "
            "VALUES (?, ?, ?, ?, NULL)",
            (user_id, kind, value, lo),
        )

    def _close_interval(self, user_id: str, kind: str, value: str, hi: int) -> None:
        self._conn.execute(
            "UPDATE membership SET hi = ? "
            "WHERE user_id = ? AND kind = ? AND value = ? AND hi IS NULL",
            (hi, user_id, kind, value),
        )

    def _cum_at(self, kind: str, value: str, seq: Optional[int]) -> int:
        """Broadcasts for (kind, value) with seq <= the given bound.

        ``seq=None`` means no upper bound. O(log n) via the target index.
        """
        if seq is None:
            row = self._conn.execute(
                "SELECT cum FROM broadcasts WHERE kind = ? AND value = ? "
                "ORDER BY seq DESC LIMIT 1",
                (kind, value),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT cum FROM broadcasts WHERE kind = ? AND value = ? "
                "AND seq <= ? ORDER BY seq DESC LIMIT 1",
                (kind, value, seq),
            ).fetchone()
        return row[0] if row else 0

    def _watermark(self, user_id: str) -> int:
        row = self._conn.execute(
            "SELECT watermark FROM read_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else 0

    def _intervals(self, user_id: str) -> list[tuple[str, str, int, Optional[int]]]:
        return list(
            self._conn.execute(
                "SELECT kind, value, lo, hi FROM membership WHERE user_id = ?",
                (user_id,),
            )
        )

    def _is_visible(self, user_id: str, seq: int, kind: str, value: str) -> bool:
        if kind == "user":
            return value == user_id
        row = self._conn.execute(
            "SELECT 1 FROM membership "
            "WHERE user_id = ? AND kind = ? AND value = ? "
            "AND lo < ? AND (hi IS NULL OR ? < hi) LIMIT 1",
            (user_id, kind, value, seq, seq),
        ).fetchone()
        return row is not None

    # -- user management ------------------------------------------------------

    def set_user(self, user_id: str, role: str, tags: Iterable[str]) -> None:
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id must be a non-empty string")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {role!r}")
        tags = set(tags)
        for tag in tags:
            if not isinstance(tag, str) or not tag:
                raise ValueError("tags must be non-empty strings")

        conn = self._conn
        with conn:
            row = conn.execute(
                "SELECT role FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                seq = self._next_seq()
                conn.execute(
                    "INSERT INTO users(user_id, role) VALUES (?, ?)",
                    (user_id, role),
                )
                self._open_interval(user_id, "role", role, seq)
                for tag in tags:
                    self._open_interval(user_id, "tag", tag, seq)
                return

            old_role = row[0]
            current_tags = {
                r[0]
                for r in conn.execute(
                    "SELECT value FROM membership "
                    "WHERE user_id = ? AND kind = 'tag' AND hi IS NULL",
                    (user_id,),
                )
            }
            removed = current_tags - tags
            added = tags - current_tags
            if old_role == role and not removed and not added:
                return

            seq = self._next_seq()
            if old_role != role:
                self._close_interval(user_id, "role", old_role, seq)
                self._open_interval(user_id, "role", role, seq)
            for tag in removed:
                self._close_interval(user_id, "tag", tag, seq)
            for tag in added:
                self._open_interval(user_id, "tag", tag, seq)
            conn.execute(
                "UPDATE users SET role = ? WHERE user_id = ?", (role, user_id)
            )

    # -- sending ---------------------------------------------------------------

    def send(self, target: str, title: str, body: str) -> int:
        kind, sep, value = target.partition(":")
        if not sep or kind not in ("user", "role", "tag") or not value:
            raise ValueError(f"invalid target: {target!r}")
        if kind == "user":
            self._require_user(value)
        elif kind == "role" and value not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {value!r}")

        conn = self._conn
        with conn:
            seq = self._next_seq()
            row = conn.execute(
                "SELECT cum FROM broadcasts WHERE kind = ? AND value = ? "
                "ORDER BY seq DESC LIMIT 1",
                (kind, value),
            ).fetchone()
            cum = (row[0] if row else 0) + 1
            conn.execute(
                "INSERT INTO broadcasts(seq, kind, value, cum, title, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (seq, kind, value, cum, title, body),
            )
        return seq

    # -- inbox ------------------------------------------------------------------

    _INF = 1 << 62

    def _stream_next(
        self, kind: str, value: str, lo: int, hi: int
    ) -> Optional[tuple[int, str, str]]:
        """Newest broadcast for (kind, value) with lo < seq < hi."""
        return self._conn.execute(
            "SELECT seq, title, body FROM broadcasts "
            "WHERE kind = ? AND value = ? AND seq > ? AND seq < ? "
            "ORDER BY seq DESC LIMIT 1",
            (kind, value, lo, hi),
        ).fetchone()

    def _is_individually_read(self, user_id: str, seq: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM read_ids WHERE user_id = ? AND seq = ?",
            (user_id, seq),
        ).fetchone()
        return row is not None

    def inbox(
        self,
        user_id: str,
        unread_only: bool = False,
        cursor: Optional[int] = None,
        limit: int = 25,
    ) -> tuple[list[dict], Optional[int]]:
        self._require_user(user_id)
        limit = max(1, min(int(limit), 100))
        wm = self._watermark(user_id)
        upper = self._INF if cursor is None else int(cursor)

        # One sorted stream per visibility source: direct messages plus one
        # stream per membership interval. Each stream yields its broadcasts
        # newest-first via indexed seeks; we merge them lazily so a page
        # costs O((intervals + limit) * log n), independent of audience size
        # and of the user's total visible history.
        streams = []
        # (kind, value, lower bound, stream-local upper bound)
        streams.append(["user", user_id, wm if unread_only else 0, upper])
        for kind, value, lo, hi in self._intervals(user_id):
            if unread_only:
                lo = max(lo, wm)
            stream_hi = min(hi, upper) if hi is not None else upper
            streams.append([kind, value, lo, stream_hi])

        heads = [
            self._stream_next(kind, value, lo, hi)
            for kind, value, lo, hi in streams
        ]

        accepted: list[dict] = []
        while len(accepted) < limit + 1:
            best = -1
            for i, head in enumerate(heads):
                if head is not None and (best < 0 or head[0] > heads[best][0]):
                    best = i
            if best < 0:
                break
            seq, title, body = heads[best]
            streams[best][3] = seq  # advance: next fetch is strictly older
            heads[best] = self._stream_next(*streams[best])

            individually_read = self._is_individually_read(user_id, seq)
            if unread_only:
                if individually_read:
                    continue
                read = False
            else:
                read = seq <= wm or individually_read
            accepted.append(
                {"id": seq, "title": title, "body": body, "read": read}
            )

        items = accepted[:limit]
        next_cursor = items[-1]["id"] if len(accepted) > limit else None
        return items, next_cursor

    # -- reading ---------------------------------------------------------------

    def unread_count(self, user_id: str) -> int:
        self._require_user(user_id)
        wm = self._watermark(user_id)

        # Direct messages: visible regardless of membership.
        total = self._cum_at("user", user_id, None) - self._cum_at(
            "user", user_id, wm
        )
        # Broadcasts visible via each membership interval, above the watermark.
        for kind, value, lo, hi in self._intervals(user_id):
            lower = max(lo, wm)
            upper = None if hi is None else hi - 1
            if upper is not None and upper <= lower:
                continue
            total += self._cum_at(kind, value, upper) - self._cum_at(
                kind, value, lower
            )

        # Individually-read notifications above the watermark.
        row = self._conn.execute(
            "SELECT COUNT(*) FROM read_ids WHERE user_id = ? AND seq > ?",
            (user_id, wm),
        ).fetchone()
        return total - row[0]

    # -- marking read -----------------------------------------------------------

    def mark_read(self, user_id: str, ids: Optional[Iterable[int]] = None) -> None:
        self._require_user(user_id)
        conn = self._conn
        if ids is None:
            # One row total: everything visible right now has seq <= the
            # current event counter, so bumping the watermark marks it read.
            # Later sends get higher seqs and stay unread.
            with conn:
                wm = self._current_seq()
                conn.execute(
                    "INSERT INTO read_state(user_id, watermark) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "watermark = MAX(watermark, excluded.watermark)",
                    (user_id, wm),
                )
                conn.execute(
                    "DELETE FROM read_ids WHERE user_id = ? AND seq <= ?",
                    (user_id, wm),
                )
            return

        with conn:
            for nid in ids:
                row = conn.execute(
                    "SELECT kind, value FROM broadcasts WHERE seq = ?",
                    (int(nid),),
                ).fetchone()
                if row is None:
                    continue  # unknown id: silently ignore
                kind, value = row
                if not self._is_visible(user_id, int(nid), kind, value):
                    continue  # not visible to this user: silently ignore
                conn.execute(
                    "INSERT OR IGNORE INTO read_ids(user_id, seq) VALUES (?, ?)",
                    (user_id, int(nid)),
                )
