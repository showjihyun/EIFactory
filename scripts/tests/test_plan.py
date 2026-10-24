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


# ---------- 계획자 세션 (dispatch.py --plan, 6단계) ----------

FAKE_PLANNER = r"""
import json, os, sys
sys.stdin.reconfigure(encoding="utf-8")
prompt = sys.stdin.read()
with open(os.environ["FAKE_PLANNER_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}, ensure_ascii=False) + "\n")
mode = os.environ.get("FAKE_PLANNER_MODE", "ok")
if mode == "crash":
    sys.exit(1)
plan = json.loads(os.environ["FAKE_PLANNER_PLAN"])
print(json.dumps({"type": "result", "result": "계획을 세웠다", "structured_output": plan,
                  "total_cost_usd": 0.3}, ensure_ascii=False))
"""


@pytest.fixture
def planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "planner.py"
    script.write_text(FAKE_PLANNER, encoding="utf-8")
    log = tmp_path / "planner.log"
    monkeypatch.setenv("FACTORY_PLANNER_CMD", json.dumps([sys.executable, str(script)]))
    monkeypatch.setenv("FAKE_PLANNER_LOG", str(log))
    return log


def set_planner_output(monkeypatch: pytest.MonkeyPatch, jobs: list[dict[str, Any]]) -> None:
    monkeypatch.setenv("FAKE_PLANNER_PLAN", json.dumps({"goal": "목표", "jobs": jobs}))


def test_dispatch_plan_writes_a_checked_plan(
    root: Path, planner: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    import dispatch

    set_planner_output(monkeypatch, [draft("A"), draft("B", depends_on=["A"])])
    assert dispatch.main(["--plan", "노트 정리 흐름 완성"], root=root) == 0
    out = capsys.readouterr().out
    assert (root / "factory" / "plans" / "P001.md").is_file()
    assert state(root)["status"] == "CHECKED"
    assert "dispatch.py --run-plan P001" in out
    call = json.loads(planner.read_text(encoding="utf-8").splitlines()[0])
    # 계획자는 목표와 규칙을 받는다. 코드를 쓰지 않고 읽기만 한다 (6.8의 9번)
    assert "노트 정리 흐름 완성" in call["prompt"]
    assert "data_delete" in call["prompt"] and "design" in call["prompt"]
    set_planner_output(monkeypatch, [draft("A")])
    assert dispatch.main(["--plan", "두 번째 목표"], root=root) == 0
    assert (root / "factory" / "plans" / "P002.md").is_file()


def test_planner_command_is_read_only_with_schema(root: Path) -> None:
    cmd = plan.planner_command(root)
    assert cmd[:2] == ["claude", "-p"]
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert set(schema["properties"]["jobs"]["items"]["required"]) == set(plan.FIELDS)
    tools = cmd[cmd.index("--allowedTools") + 1 :]
    assert tools == ["Read", "Glob", "Grep"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"  # 계획자 기본 (사람 결정)
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.0"


def test_bad_planner_draft_is_kept_for_the_human_but_not_checked(
    root: Path, planner: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    import dispatch

    set_planner_output(monkeypatch, [draft("A", size="large")])
    assert dispatch.main(["--plan", "목표"], root=root) == 1
    assert "분할" in capsys.readouterr().out
    assert (root / "factory" / "plans" / "P001.md").is_file()  # 사람이 고쳐서 plan.py check
    assert not state_path(root).exists()


def test_planner_failure_writes_nothing(
    root: Path, planner: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    import dispatch

    monkeypatch.setenv("FAKE_PLANNER_MODE", "crash")
    assert dispatch.main(["--plan", "목표"], root=root) == 1
    assert "계획자" in capsys.readouterr().out
    assert not (root / "factory" / "plans").exists()


def test_missing_plan_is_an_error(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = check(root, capsys)
    assert code == 2
    assert "P001" in out
