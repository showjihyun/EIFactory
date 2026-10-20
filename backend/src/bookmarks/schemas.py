from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

MAX_TITLE_LENGTH = 200
MAX_TAGS = 10
MAX_TAG_LENGTH = 30


def _check_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")) or len(value) <= len("https://"):
        raise ValueError("URL은 http:// 또는 https://로 시작해야 합니다")
    return value


def _check_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("제목을 입력하세요")
    if len(value) > MAX_TITLE_LENGTH:
        raise ValueError(f"제목은 {MAX_TITLE_LENGTH}자 이하여야 합니다")
    return value


def _normalize_tags(values: list[str]) -> list[str]:
    tags: list[str] = []
    for raw in values:
        tag = raw.strip().lower()
        if not tag or tag in tags:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"태그는 {MAX_TAG_LENGTH}자 이하여야 합니다")
        tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"태그는 최대 {MAX_TAGS}개입니다")
    return tags


Url = Annotated[str, AfterValidator(_check_url)]
Title = Annotated[str, AfterValidator(_check_title)]
Tags = Annotated[list[str], AfterValidator(_normalize_tags)]


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str


class BookmarkCreate(BaseModel):
    url: Url
    title: Title
    tags: Tags = Field(default_factory=list)


class BookmarkUpdate(BaseModel):
    url: Url | None = None
    title: Title | None = None
    tags: Tags | None = None


class Bookmark(BaseModel):
    id: int
    url: str
    title: str
    tags: list[str]
    created_at: datetime
