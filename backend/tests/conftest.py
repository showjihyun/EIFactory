from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bookmarks.main import create_app
from bookmarks.settings import Settings

TEST_USER = "tester"
TEST_PASSWORD = "test-password"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        db_path=tmp_path / "test.db",
        username=TEST_USER,
        password=TEST_PASSWORD,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}
