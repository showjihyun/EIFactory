from fastapi.testclient import TestClient

from tests.conftest import TEST_PASSWORD, TEST_USER


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_with_valid_credentials_returns_token(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["token"]


def test_login_with_wrong_password_is_rejected(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": TEST_USER, "password": "wrong"})
    assert response.status_code == 401


def test_bookmarks_require_authentication(client: TestClient) -> None:
    assert client.get("/api/bookmarks").status_code == 401


def test_invalid_token_is_rejected(client: TestClient) -> None:
    response = client.get("/api/bookmarks", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_logout_invalidates_token(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.post("/api/auth/logout", headers=auth_headers).status_code == 204
    assert client.get("/api/bookmarks", headers=auth_headers).status_code == 401
