from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bookmarks.auth import SessionStore
from bookmarks.repository import BookmarkRepository
from bookmarks.schemas import (
    Bookmark,
    BookmarkCreate,
    BookmarkUpdate,
    LoginRequest,
    LoginResponse,
)
from bookmarks.settings import Settings

bearer = HTTPBearer(auto_error=False)


def get_repo(request: Request) -> BookmarkRepository:
    repo: BookmarkRepository = request.app.state.repo
    return repo


def get_sessions(request: Request) -> SessionStore:
    sessions: SessionStore = request.app.state.sessions
    return sessions


def require_token(
    sessions: Annotated[SessionStore, Depends(get_sessions)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None or not sessions.is_valid(credentials.credentials):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다")
    return credentials.credentials


Repo = Annotated[BookmarkRepository, Depends(get_repo)]
Sessions = Annotated[SessionStore, Depends(get_sessions)]
Token = Annotated[str, Depends(require_token)]

public = APIRouter(prefix="/api")
protected = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@public.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@public.post("/auth/login")
def login(body: LoginRequest, sessions: Sessions) -> LoginResponse:
    token = sessions.login(body.username, body.password)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "아이디 또는 비밀번호가 틀렸습니다")
    return LoginResponse(token=token)


@protected.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(token: Token, sessions: Sessions) -> Response:
    sessions.logout(token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@protected.get("/bookmarks")
def list_bookmarks(repo: Repo, q: str | None = None, tag: str | None = None) -> list[Bookmark]:
    return repo.search(q=q, tag=tag)


@protected.post("/bookmarks", status_code=status.HTTP_201_CREATED)
def create_bookmark(body: BookmarkCreate, repo: Repo) -> Bookmark:
    return repo.create(body)


@protected.get("/bookmarks/{bookmark_id}")
def get_bookmark(bookmark_id: int, repo: Repo) -> Bookmark:
    return _found(repo.get(bookmark_id))


@protected.patch("/bookmarks/{bookmark_id}")
def update_bookmark(bookmark_id: int, body: BookmarkUpdate, repo: Repo) -> Bookmark:
    return _found(repo.update(bookmark_id, body))


@protected.delete("/bookmarks/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bookmark(bookmark_id: int, repo: Repo) -> Response:
    if not repo.delete(bookmark_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "북마크를 찾을 수 없습니다")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@protected.get("/tags")
def list_tags(repo: Repo) -> list[str]:
    return repo.tags()


def _found(bookmark: Bookmark | None) -> Bookmark:
    if bookmark is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "북마크를 찾을 수 없습니다")
    return bookmark


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.repo = BookmarkRepository(settings.db_path)
        app.state.sessions = SessionStore(settings)
        yield
        app.state.repo.close()

    app = FastAPI(title="Bookmarks", lifespan=lifespan)
    app.include_router(public)
    app.include_router(protected)
    return app


def app_from_env() -> FastAPI:
    return create_app(Settings.from_env())
