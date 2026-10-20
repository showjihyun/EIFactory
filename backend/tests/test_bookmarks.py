from typing import Any

from fastapi.testclient import TestClient


def create(client: TestClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": "https://example.com/docs",
        "title": "Example Docs",
        "tags": ["docs"],
    }
    payload.update(overrides)
    response = client.post("/api/bookmarks", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def test_list_is_empty_initially(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/bookmarks", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_create_returns_saved_bookmark(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = create(client, auth_headers)
    assert body["id"] > 0
    assert body["url"] == "https://example.com/docs"
    assert body["title"] == "Example Docs"
    assert body["tags"] == ["docs"]
    assert body["created_at"]


def test_created_bookmark_appears_in_list(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = create(client, auth_headers)
    listed = client.get("/api/bookmarks", headers=auth_headers).json()
    assert [b["id"] for b in listed] == [created["id"]]


def test_list_is_newest_first(client: TestClient, auth_headers: dict[str, str]) -> None:
    first = create(client, auth_headers, title="First")
    second = create(client, auth_headers, title="Second")
    listed = client.get("/api/bookmarks", headers=auth_headers).json()
    assert [b["id"] for b in listed] == [second["id"], first["id"]]


def test_url_must_be_http_or_https(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/bookmarks",
        json={"url": "ftp://example.com", "title": "FTP", "tags": []},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_title_must_not_be_blank(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/bookmarks",
        json={"url": "https://example.com", "title": "   ", "tags": []},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_title_is_limited_to_200_characters(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/bookmarks",
        json={"url": "https://example.com", "title": "a" * 201, "tags": []},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_tags_are_normalized_to_lowercase_and_deduplicated(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = create(client, auth_headers, tags=["Docs", "docs", " Python "])
    assert body["tags"] == ["docs", "python"]


def test_at_most_10_tags(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/bookmarks",
        json={"url": "https://example.com", "title": "Many", "tags": [f"t{i}" for i in range(11)]},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_search_matches_title(client: TestClient, auth_headers: dict[str, str]) -> None:
    match = create(client, auth_headers, title="FastAPI Tutorial")
    create(client, auth_headers, title="React Guide")
    listed = client.get("/api/bookmarks", params={"q": "FastAPI"}, headers=auth_headers).json()
    assert [b["id"] for b in listed] == [match["id"]]


def test_filter_by_tag(client: TestClient, auth_headers: dict[str, str]) -> None:
    python = create(client, auth_headers, title="Python", tags=["python"])
    create(client, auth_headers, title="React", tags=["react"])
    listed = client.get("/api/bookmarks", params={"tag": "python"}, headers=auth_headers).json()
    assert [b["id"] for b in listed] == [python["id"]]


def test_get_single_bookmark(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = create(client, auth_headers)
    response = client.get(f"/api/bookmarks/{created['id']}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == created


def test_get_missing_bookmark_returns_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.get("/api/bookmarks/999", headers=auth_headers).status_code == 404


def test_update_changes_only_given_fields(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = create(client, auth_headers)
    response = client.patch(
        f"/api/bookmarks/{created['id']}", json={"title": "Renamed"}, headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed"
    assert body["url"] == created["url"]
    assert body["tags"] == created["tags"]


def test_update_validates_input(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = create(client, auth_headers)
    response = client.patch(
        f"/api/bookmarks/{created['id']}", json={"url": "not-a-url"}, headers=auth_headers
    )
    assert response.status_code == 422


def test_delete_removes_bookmark(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = create(client, auth_headers)
    response = client.delete(f"/api/bookmarks/{created['id']}", headers=auth_headers)
    assert response.status_code == 204
    assert client.get(f"/api/bookmarks/{created['id']}", headers=auth_headers).status_code == 404


def test_delete_missing_bookmark_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.delete("/api/bookmarks/999", headers=auth_headers).status_code == 404


def test_tags_endpoint_lists_distinct_tags_sorted(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    create(client, auth_headers, tags=["react", "docs"])
    create(client, auth_headers, tags=["docs", "python"])
    response = client.get("/api/tags", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == ["docs", "python", "react"]
