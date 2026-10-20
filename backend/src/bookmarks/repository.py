import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bookmarks.schemas import Bookmark, BookmarkCreate, BookmarkUpdate

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
)
"""


class BookmarkRepository:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def search(self, q: str | None = None, tag: str | None = None) -> list[Bookmark]:
        sql = "SELECT * FROM bookmarks"
        params: list[str] = []
        if q:
            sql += " WHERE title GLOB ?"
            params.append(f"*{q}*")
        sql += " ORDER BY id DESC"
        bookmarks = [_to_bookmark(row) for row in self._conn.execute(sql, params)]
        if tag:
            bookmarks = [b for b in bookmarks if tag.lower() in b.tags]
        return bookmarks

    def get(self, bookmark_id: int) -> Bookmark | None:
        row = self._conn.execute("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone()
        return _to_bookmark(row) if row else None

    def create(self, data: BookmarkCreate) -> Bookmark:
        cursor = self._conn.execute(
            "INSERT INTO bookmarks (url, title, tags, created_at) VALUES (?, ?, ?, ?)",
            (data.url, data.title, json.dumps(data.tags), datetime.now(UTC).isoformat()),
        )
        self._conn.commit()
        created = self.get(_last_id(cursor))
        assert created is not None
        return created

    def update(self, bookmark_id: int, data: BookmarkUpdate) -> Bookmark | None:
        current = self.get(bookmark_id)
        if current is None:
            return None
        self._conn.execute(
            "UPDATE bookmarks SET url = ?, title = ?, tags = ? WHERE id = ?",
            (
                data.url if data.url is not None else current.url,
                data.title if data.title is not None else current.title,
                json.dumps(data.tags if data.tags is not None else current.tags),
                bookmark_id,
            ),
        )
        self._conn.commit()
        return self.get(bookmark_id)

    def delete(self, bookmark_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def tags(self) -> list[str]:
        found: set[str] = set()
        for row in self._conn.execute("SELECT tags FROM bookmarks"):
            found.update(json.loads(row["tags"]))
        return sorted(found)


def _last_id(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _to_bookmark(row: sqlite3.Row) -> Bookmark:
    return Bookmark(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        tags=json.loads(row["tags"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
