import hmac
import secrets

from bookmarks.settings import Settings


class SessionStore:
    """메모리 기반 세션. 서버를 재시작하면 모든 토큰이 무효가 된다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tokens: set[str] = set()

    def login(self, username: str, password: str) -> str | None:
        valid_user = hmac.compare_digest(username, self._settings.username)
        valid_password = hmac.compare_digest(password, self._settings.password)
        if not (valid_user and valid_password):
            return None
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def logout(self, token: str) -> None:
        self._tokens.discard(token)

    def is_valid(self, token: str) -> bool:
        return token in self._tokens
