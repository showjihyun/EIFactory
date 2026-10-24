import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brief  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """원격(bare)에 push된 기본 checkout. jobs 폴더는 옆에 둔다 (dispatch와 같은 배치)."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    (tmp_path / "repo.jobs").mkdir()
    return repo


def add_job(root: Path, job_id: str, status: str, **fields: Any) -> None:
    job = {
        "id": job_id,
        "status": status,
        "risk": "MEDIUM",
        "requirement": f"{job_id} 요구사항",
        "base_branch": "main",
        "assumptions": [],
        "reviews": [],
        "history": [{"status": status, "at": fields.pop("at", 100.0)}],
        "time": {"extended": False},
        **fields,
    }
    path = root.parent / "repo.jobs" / job_id / "factory" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": {job_id: job}}, ensure_ascii=False), encoding="utf-8")


def collect(root: Path) -> dict[str, Any]:
    return brief.collect(root, root.parent / "repo.jobs")


def test_nothing_waiting(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_job(root, "J001", "MERGED")
    assert collect(root)["items"] == []
    assert brief.main([], root=root) == 0
    assert "기다리는 일이 없습니다" in capsys.readouterr().out


def test_approval_comes_first_with_judgement_and_commands(root: Path) -> None:
    add_job(root, "J001", "BLOCKED", blocked={"reason": "TIMEOUT", "detail": "15분"}, at=1.0)
    review = {"round": 1, "reviewer": "security", "verdict": "PASS",
              "notes_for_human": ["토큰 비교가 상수 시간이 아니다"], "risk_up": None}  # fmt: skip
    add_job(root, "J002", "APPROVAL_REQUIRED", assumptions=["만료 토큰은 401"], reviews=[review])
    items = collect(root)["items"]
    assert [(i["kind"], i["id"]) for i in items] == [("approval", "J002"), ("blocked", "J001")]
    approval = items[0]
    assert "가정: 만료 토큰은 401" in "\n".join(approval["lines"])
    assert "[security] 토큰 비교가 상수 시간이 아니다" in "\n".join(approval["lines"])
    assert "python scripts/dispatch.py --approve J002" in approval["commands"]
    # 작업 상태 파일은 세션이 쓴 값이다 (기획안 9장)
    assert approval["trust"] == "session"


def test_high_risk_approval_before_medium(root: Path) -> None:
    add_job(root, "J001", "APPROVAL_REQUIRED", risk="MEDIUM", at=1.0)
    add_job(root, "J002", "APPROVAL_REQUIRED", risk="HIGH", at=2.0)
    assert [i["id"] for i in collect(root)["items"]] == ["J002", "J001"]


@pytest.mark.parametrize(
    ("reason", "extended", "expected"),
    [
        ("TIMEOUT", False, "--extend"),
        ("TIMEOUT", True, "split"),
        ("CONFLICT", False, "직접"),
        ("NEEDS_DECISION", False, "결정"),
    ],
)
def test_blocked_item_recommends_by_rule(
    root: Path, reason: str, extended: bool, expected: str
) -> None:
    add_job(
        root, "J001", "BLOCKED",
        blocked={"reason": reason, "detail": "진단"}, time={"extended": extended},
    )  # fmt: skip
    item = collect(root)["items"][0]
    assert item["kind"] == "blocked"
    assert reason in item["title"]
    assert expected in item["recommendation"]
    if reason == "TIMEOUT" and extended:
        assert not any("--extend" in c for c in item["commands"])


def test_push_waiting_when_base_is_ahead_of_upstream(root: Path) -> None:
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "머지된 작업")
    items = collect(root)["items"]
    assert [i["kind"] for i in items] == ["push"]
    assert "1" in items[0]["title"]
    assert items[0]["trust"] == "host"


def test_no_push_item_without_upstream(root: Path) -> None:
    git(root, "branch", "--unset-upstream")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "로컬만")
    assert collect(root)["items"] == []


def test_dispatch_failures_are_warnings(root: Path) -> None:
    report = {"started": "s1", "jobs": [{"status": "SETUP_FAILED", "error": "npm ci 실패"}]}
    (root.parent / "repo.jobs" / "dispatch-s1.json").write_text(json.dumps(report), "utf-8")
    items = collect(root)["items"]
    assert [i["kind"] for i in items] == ["warning"]
    assert "npm ci 실패" in "\n".join(items[0]["lines"])


def test_counts_line_for_status_bars(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_job(root, "J001", "APPROVAL_REQUIRED")
    add_job(root, "J002", "BLOCKED", blocked={"reason": "BUDGET", "detail": "$5"})
    assert brief.main(["--counts"], root=root) == 0
    assert capsys.readouterr().out.strip() == "승인 1 · BLOCKED 1"
    git(root, "commit", "-q", "--allow-empty", "-m", "x")
    brief.main(["--counts"], root=root)
    assert capsys.readouterr().out.strip() == "승인 1 · BLOCKED 1 · push 1"


def test_counts_line_is_empty_when_nothing_waits(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert brief.main(["--counts"], root=root) == 0
    assert capsys.readouterr().out.strip() == ""


def test_json_output(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_job(root, "J001", "APPROVAL_REQUIRED")
    assert brief.main(["--json"], root=root) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"] == {"approval": 1, "blocked": 0, "push": 0, "warning": 0}
    assert data["items"][0]["id"] == "J001"
