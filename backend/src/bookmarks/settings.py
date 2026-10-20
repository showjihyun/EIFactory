import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    username: str
    password: str

    @classmethod
    def from_env(cls) -> "Settings":
        # 샘플 앱용 더미 기본값. 실제 비밀번호를 넣지 않는다.
        return cls(
            db_path=Path(os.environ.get("BOOKMARKS_DB_PATH", "bookmarks.db")),
            username=os.environ.get("BOOKMARKS_USERNAME", "demo"),
            password=os.environ.get("BOOKMARKS_PASSWORD", "demo"),
        )
