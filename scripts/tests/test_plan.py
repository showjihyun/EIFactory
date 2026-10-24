import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import factory  # noqa: E402
import plan  # noqa: E402


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
    """샘플 레포 설정을 쓰는 기본 checkout (위험 경로·잠금 대상이 샘플과 같다)."""
    repo = tmp_path / "repo"
    (repo / "factory").mkdir(parents=True)
    shutil.copy(SCRIPTS.parent / "factory" / "config.json", repo / "factory" / "config.json")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def draft(job_id: str = "A", **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": job_id,
        "requirement": f"{job_id} 요구사항",
        "type": "feature",
        "size": "small",
        "paths": ["frontend/src/components/BookmarkList.tsx"],
        "touches": [],
        "depends_on": [],
        "assumptions": [],
        "design": None,
        "acceptance_hints": ["무언가가 보인다"],
    }
    item.update(fields)
    return item


def write_plan(root: Path, jobs: list[dict[str, Any]], plan_id: str = "P001") -> None:
    body = {"goal": "목표", "jobs": jobs}
    path = root / "factory" / "plans" / f"{plan_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {plan_id}\n\n```json\n{json.dumps(body, ensure_ascii=False)}\n```\n", "utf-8"
    )


def check(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = plan.main(["check", "P001"], root=root)
    return code, capsys.readouterr().out


def state_path(root: Path) -> Path:
    # 계획 실행 상태는 호스트 기록(jobs 폴더)에 둔다. 레포에는 사람이 보는 계획 문서만 (기획안 9장)
    return root.parent / "repo.jobs" / "plans" / "P001.json"


def state(root: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(state_path(root).read_text(encoding="utf-8"))
    return loaded


def test_check_predicts_risk_with_the_same_rules_as_verify(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plan(root, [
        draft("A"),
        draft("B", paths=["backend/src/bookmarks/main.py"], touches=["api"]),
        draft("C", paths=["backend/src/bookmarks/auth.py"]),
        draft("D", paths=["backend/src/bookmarks/repository.py"], touches=["data_delete"]),
    ])  # fmt: skip
    code, out = check(root, capsys)
    assert code == 0, out
    jobs = {j["id"]: j for j in state(root)["jobs"]}
    assert state(root)["status"] == "CHECKED"
    assert jobs["A"]["expected_risk"] == "LOW"
    assert jobs["B"]["expected_risk"] == "MEDIUM"
    assert "MEDIUM: API 경로 추가·삭제" in jobs["B"]["risk_reasons"]
    assert jobs["C"]["expected_risk"] == "HIGH"
    assert "HIGH: backend/src/bookmarks/auth.py 변경" in jobs["C"]["risk_reasons"]
    assert jobs["D"]["expected_risk"] == "HIGH"
    assert all(j["route"] == "dispatch" for j in jobs.values())


def test_path_rule_is_shared_with_verify(root: Path) -> None:
    cfg = factory.load_config(root)
    assert factory.path_risk(cfg, "backend/pyproject.toml") == (
        "MEDIUM", "backend/pyproject.toml 변경"
    )  # fmt: skip
    assert factory.path_risk(cfg, "frontend/src/App.tsx") is None


def test_refactor_type_is_medium(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_plan(root, [draft("A", type="refactor", size="medium", design="BookmarkItem 분리")])
    assert check(root, capsys)[0] == 0
    assert state(root)["jobs"][0]["expected_risk"] == "MEDIUM"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"size": "large"}, "분할"),
        ({"size": "medium", "design": None}, "design"),
        ({"type": "chore"}, "type"),
        ({"assumptions": "없음"}, "assumptions"),
        ({"requirement": ""}, "requirement"),
        ({"touches": ["magic"]}, "touches"),
        ({"paths": []}, "paths"),
    ],
)
def test_check_rejects_bad_drafts(
    root: Path, capsys: pytest.CaptureFixture[str], fields: dict[str, Any], message: str
) -> None:
    write_plan(root, [draft("A", **fields)])
    code, out = check(root, capsys)
    assert code == 1
    assert message in out
    assert not state_path(root).exists()


def test_locked_non_test_paths_go_to_the_human(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 검증 설정·정책 파일·scripts/는 팩토리 작업이 바꿀 수 없다 → 사람 직접 (기존 테스트는 아님)
    write_plan(root, [
        draft("A", paths=["Makefile"]),
        draft("B", paths=["scripts/verify.py"]),
        draft("C", paths=["backend/tests/test_bookmarks.py", "backend/src/bookmarks/main.py"]),
    ])  # fmt: skip
    assert check(root, capsys)[0] == 0
    routes = {j["id"]: j["route"] for j in state(root)["jobs"]}
    assert routes == {"A": "human", "B": "human", "C": "dispatch"}


def test_verification_weakening_needs_a_decision(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plan(root, [draft("A", touches=["test_skip"])])
    assert check(root, capsys)[0] == 0
    job = state(root)["jobs"][0]
    assert job["route"] == "needs_decision"


@pytest.mark.parametrize(
    "jobs",
    [
        [draft("A", depends_on=["Z"])],
        [draft("A", depends_on=["B"]), draft("B", depends_on=["A"])],
        [draft("A"), draft("A")],
    ],
)
def test_check_rejects_broken_dependencies(
    root: Path, capsys: pytest.CaptureFixture[str], jobs: list[dict[str, Any]]
) -> None:
    write_plan(root, jobs)
    code, out = check(root, capsys)
    assert code == 1
    assert "depends_on" in out or "중복" in out


def test_check_prints_a_table_for_the_human(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_plan(root, [
        draft("A"),
        draft("B", paths=["backend/src/bookmarks/main.py"], touches=["api"], depends_on=["A"]),
    ])  # fmt: skip
    code, out = check(root, capsys)
    assert code == 0
    assert "| A |" in out and "| B |" in out and "MEDIUM" in out
    assert "dispatch.py --run-plan P001" in out


def test_missing_plan_is_an_error(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = check(root, capsys)
    assert code == 2
    assert "P001" in out
