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


# ---------- 실행 순서 (--run-plan의 결정 규칙, 기획안 6.9) ----------


def checked(*jobs: dict[str, Any]) -> dict[str, Any]:
    """검사된 계획 상태 (route 기본 dispatch)."""
    items = [{"route": "dispatch", "expected_risk": "LOW", **j} for j in jobs]
    return {"id": "P001", "status": "CHECKED", "jobs": items}


def test_schedule_starts_only_jobs_whose_dependencies_merged() -> None:
    state = checked(draft("A"), draft("B", depends_on=["A"], paths=["b.py"]),
                    draft("C", paths=["c.py"]))  # fmt: skip
    start, waiting = plan.schedule(state, {}, set())
    assert start == ["A", "C"]
    assert waiting == {"B": "A 진행 전"}
    start, waiting = plan.schedule(state, {"A": "MERGED", "C": "MERGED"}, set())
    assert start == ["B"] and waiting == {}


def test_schedule_waits_only_for_dependents_of_a_job_awaiting_approval() -> None:
    state = checked(draft("A"), draft("B", depends_on=["A"], paths=["b.py"]),
                    draft("C", paths=["c.py"]))  # fmt: skip
    start, waiting = plan.schedule(state, {"A": "APPROVAL_REQUIRED"}, set())
    assert start == ["C"]
    assert waiting == {"B": "A 승인 대기"}


def test_schedule_never_restarts_running_merged_or_waiting_jobs() -> None:
    state = checked(draft("A"), draft("B", paths=["b.py"]), draft("C", paths=["c.py"]),
                    draft("D", paths=["d.py"]))  # fmt: skip
    statuses = {"A": "MERGED", "B": "APPROVAL_REQUIRED", "C": "BLOCKED"}
    assert plan.schedule(state, statuses, {"D"}) == ([], {})


def test_schedule_restarts_dropped_or_failed_jobs_unless_tried_this_run() -> None:
    state = checked(draft("A"), draft("B", paths=["b.py"]))
    statuses = {"A": "DROPPED", "B": "SETUP_FAILED"}
    assert plan.schedule(state, statuses, set())[0] == ["A", "B"]
    assert plan.schedule(state, statuses, {"A", "B"})[0] == []


def test_schedule_runs_jobs_with_overlapping_paths_one_at_a_time() -> None:
    state = checked(
        draft("A", paths=["frontend/src/App.tsx"]),
        draft("B", paths=["frontend/src/App.tsx", "frontend/src/x.ts"]),
        draft("C", paths=["frontend/src/components/*.tsx"]),
        draft("D", paths=["frontend/src/components/List.tsx"]),
    )
    assert plan.schedule(state, {}, set())[0] == ["A", "C"]
    # A가 도는 동안 B는 기다리고, A가 끝나면 시작한다
    assert plan.schedule(state, {}, {"A", "C"})[0] == []
    assert plan.schedule(state, {"A": "MERGED", "C": "MERGED"}, set())[0] == ["B", "D"]


def test_schedule_does_not_dispatch_human_items_and_holds_their_dependents() -> None:
    state = checked(
        {**draft("H", paths=["scripts/eval.py"]), "route": "human"},
        draft("A", depends_on=["H"]),
    )
    start, waiting = plan.schedule(state, {}, set())
    assert start == []
    assert waiting == {"A": "H 사람 직접 항목"}


def test_schedule_reports_blocked_upstream() -> None:
    state = checked(draft("A"), draft("B", depends_on=["A"], paths=["b.py"]))
    assert plan.schedule(state, {"A": "BLOCKED"}, set()) == ([], {"B": "A BLOCKED"})


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({"A": "MERGED", "B": "MERGED"}, "DONE"),
        ({"A": "APPROVAL_REQUIRED"}, "RUNNING"),
        ({"A": "BLOCKED"}, "STOPPED"),
        ({"A": "MERGED", "B": "SETUP_FAILED"}, "STOPPED"),
    ],
)
def test_plan_status(statuses: dict[str, str], expected: str) -> None:
    state = checked(draft("A"), draft("B", depends_on=["A"], paths=["b.py"]))
    assert plan.plan_status(state, statuses) == expected


def test_plan_with_a_human_item_holding_dependents_is_stopped() -> None:
    state = checked({**draft("H"), "route": "human"}, draft("A", depends_on=["H"]))
    assert plan.plan_status(state, {}) == "STOPPED"
    # 사람 직접 항목에 기대는 작업이 없으면, 팩토리 작업이 다 머지됐을 때 DONE
    state = checked({**draft("H"), "route": "human"}, draft("A"))
    assert plan.plan_status(state, {"A": "MERGED"}) == "DONE"


# ---------- 실행 전 확인 ----------


def test_load_checked_requires_a_check_record(root: Path) -> None:
    write_plan(root, [draft("A")])
    with pytest.raises(plan.PlanError, match="check"):
        plan.load_checked(root, "P001")


def test_load_checked_refuses_a_plan_edited_after_the_check(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plan(root, [draft("A")])
    assert check(root, capsys)[0] == 0
    assert plan.load_checked(root, "P001")["status"] == "CHECKED"
    write_plan(root, [draft("A", requirement="바뀐 요구사항")])
    with pytest.raises(plan.PlanError, match="바뀌었"):
        plan.load_checked(root, "P001")


def test_recheck_keeps_runs_of_jobs_still_in_the_plan(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plan(root, [draft("A"), draft("B", paths=["b.py"])])
    assert check(root, capsys)[0] == 0
    saved = state(root)
    saved["runs"] = {"A": {"job_id": "J001", "status": "MERGED"},
                     "B": {"job_id": "J002", "status": "BLOCKED"}}  # fmt: skip
    saved["status"] = "STOPPED"
    state_path(root).write_text(json.dumps(saved), encoding="utf-8")
    write_plan(root, [draft("A"), draft("C", paths=["c.py"])])  # 사람이 B를 C로 바꿈
    assert check(root, capsys)[0] == 0
    assert state(root)["runs"] == {"A": {"job_id": "J001", "status": "MERGED"}}
    assert state(root)["status"] == "CHECKED"


def test_plan_context_carries_design_and_assumptions_into_the_session() -> None:
    item = {**draft("A", size="medium", design="note 열을 추가한다",
                    assumptions=["500자 제한"]), "expected_risk": "MEDIUM"}  # fmt: skip
    text = plan.plan_context("P001", item)
    assert "P001" in text and "note 열을 추가한다" in text and "500자 제한" in text
    assert "무언가가 보인다" in text  # 수락 기준 힌트


# ---------- 사람 직접 항목 완료 (factory-v4) ----------


def test_done_human_item_releases_its_dependents() -> None:
    state = checked({**draft("H"), "route": "human"}, draft("A", depends_on=["H"]))
    start, waiting = plan.schedule(state, {"H": "DONE"}, set())
    assert (start, waiting) == (["A"], {})
    assert plan.plan_status(state, {"H": "DONE"}) == "RUNNING"
    assert plan.plan_status(state, {"H": "DONE", "A": "MERGED"}) == "DONE"


def test_human_statuses_come_from_done_marks() -> None:
    state = {**checked({**draft("H"), "route": "human"}, draft("A")), "human_done": {"H": 1.0}}
    assert plan.human_statuses(state) == {"H": "DONE"}


def test_done_command_marks_a_human_item(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_plan(root, [draft("A"), draft("H", paths=["scripts/x.py"])])
    assert plan.main(["check", "P001"], root=root) == 0
    assert plan.main(["done", "P001", "H"], root=root) == 0
    state = plan.load_state(root, "P001")
    assert state is not None and "H" in state["human_done"]
    assert "H" in capsys.readouterr().out
    # 다시 검사해도 완료 표시는 남는다
    assert plan.main(["check", "P001"], root=root) == 0
    state = plan.load_state(root, "P001")
    assert state is not None and "H" in state["human_done"]


@pytest.mark.parametrize("name", ["A", "Z"])
def test_done_command_refuses_factory_items_and_unknown_ids(root: Path, name: str) -> None:
    write_plan(root, [draft("A"), draft("H", paths=["scripts/x.py"])])
    plan.main(["check", "P001"], root=root)
    assert plan.main(["done", "P001", name], root=root) == 1
    state = plan.load_state(root, "P001")
    assert state is not None and not state.get("human_done")


def test_done_command_requires_a_checked_plan(root: Path) -> None:
    write_plan(root, [draft("H", paths=["scripts/x.py"])])
    assert plan.main(["done", "P001", "H"], root=root) == 1


# ---------- 지시문 (factory-v4, randomy P001 실측 후속) ----------

PROMPTS = SCRIPTS.parent / "factory" / "prompts"


def test_reviewers_are_told_they_only_have_read_tools() -> None:
    # 리뷰어가 쓸 수 없는 Bash를 시도하다 hook에 막혀 교차 확인을 못 했다 (J008)
    text = (PROMPTS / "validator.md").read_text(encoding="utf-8")
    assert "Read·Glob·Grep" in text and "Bash" in text


def test_planner_is_told_about_screen_states_and_skill_eval_cases() -> None:
    text = (PROMPTS / "planner.md").read_text(encoding="utf-8")
    assert "요청 중" in text and "다시 시도" in text  # J009·J013·J014에서 반복된 누락
    assert "evals/" in text and "평가 케이스" in text  # 스킬 작업의 평가 케이스는 사람 직접
