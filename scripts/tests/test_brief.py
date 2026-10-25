import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brief  # noqa: E402

NOW = 1_792_843_200.0  # 2026-10-24. 정리 후보·표본이 실제 시각에 따라 바뀌지 않게 고정


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
        "history": [{"status": status, "at": fields.pop("at", NOW)}],
        "time": {"extended": False},
        **fields,
    }
    path = root.parent / "repo.jobs" / job_id / "factory" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": {job_id: job}}, ensure_ascii=False), encoding="utf-8")


def collect(root: Path) -> dict[str, Any]:
    return brief.collect(root, root.parent / "repo.jobs", now=NOW)


def test_nothing_waiting(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_job(root, "J001", "MERGED")
    assert collect(root)["items"] == []
    assert brief.main([], root=root) == 0
    assert "기다리는 일이 없습니다" in capsys.readouterr().out


def test_approval_comes_first_with_judgement_and_commands(root: Path) -> None:
    add_job(root, "J001", "BLOCKED", blocked={"reason": "TIMEOUT", "detail": "15분"}, at=NOW - 1)
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


def test_approval_shows_screenshots_of_the_job_worktree(root: Path) -> None:
    (root / "factory").mkdir()
    (root / "factory" / "config.json").write_text(
        json.dumps({"screenshots": ["frontend/test-results/**/*.png"]}), encoding="utf-8"
    )
    add_job(root, "J001", "APPROVAL_REQUIRED")
    shot = root.parent / "repo.jobs" / "J001" / "frontend/test-results/a/test-finished-1.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG")
    lines = collect(root)["items"][0]["lines"]
    assert any("스크린샷" in line and "test-finished-1.png" in line for line in lines)


def test_high_risk_approval_before_medium(root: Path) -> None:
    add_job(root, "J001", "APPROVAL_REQUIRED", risk="MEDIUM", at=NOW - 2)
    add_job(root, "J002", "APPROVAL_REQUIRED", risk="HIGH", at=NOW - 1)
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
    assert data["counts"] == {"approval": 1, "blocked": 0, "plan": 0, "human": 0, "push": 0,
                              "warning": 0, "review": 0, "cleanup": 0}  # fmt: skip
    assert data["items"][0]["id"] == "J001"


# ---------------------------------------------------------------- factory-v3 9단계

DAY = 86_400.0


def add_record(root: Path, job_id: str, status: str, **fields: Any) -> None:
    """dispatch가 끝난 작업의 상태 복사본 (records/). worktree는 없다."""
    job = {"id": job_id, "status": status, "risk": "LOW", "requirement": f"{job_id} 요구사항",
           "base_branch": "main", "history": [{"status": status, "at": NOW}], **fields}  # fmt: skip
    path = root.parent / "repo.jobs" / "records" / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


def auto_merged(at: float) -> list[dict[str, Any]]:
    return [{"status": s, "at": at} for s in ("BUILDING", "VERIFYING", "APPROVED", "MERGED")]


def add_plan(root: Path, plan_id: str, status: str, jobs: list[dict[str, Any]],
             waiting: dict[str, str] | None = None) -> None:  # fmt: skip
    state = {"id": plan_id, "goal": f"{plan_id} 목표", "status": status, "jobs": jobs,
             "runs": {}, "waiting": waiting or {}}  # fmt: skip
    path = root.parent / "repo.jobs" / "plans" / f"{plan_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def plan_job(name: str, route: str = "dispatch", reason: str = "") -> dict[str, Any]:
    return {"id": name, "requirement": f"{name} 할 일", "paths": [f"src/{name}.py"],
            "depends_on": [], "expected_risk": "LOW", "route": route,
            "route_reason": reason}  # fmt: skip


def kinds(root: Path, now: float = NOW) -> list[tuple[str, str]]:
    data = brief.collect(root, root.parent / "repo.jobs", now=now)
    return [(i["kind"], i["id"]) for i in data["items"]]


def items_of(root: Path, kind: str, now: float = NOW) -> list[dict[str, Any]]:
    data = brief.collect(root, root.parent / "repo.jobs", now=now)
    return [i for i in data["items"] if i["kind"] == kind]


def test_checked_plan_waits_for_run_plan(root: Path) -> None:
    add_plan(root, "P001", "CHECKED", [plan_job("a")])
    add_plan(root, "P002", "DONE", [plan_job("a")])
    [item] = items_of(root, "plan")
    assert item["id"] == "P001"
    assert "P001 목표" in item["title"]
    assert item["commands"] == ["python scripts/dispatch.py --run-plan P001"]
    assert item["trust"] == "host"


def test_running_plan_lists_plan_waiting_jobs(root: Path) -> None:
    add_plan(root, "P001", "RUNNING", [plan_job("a"), plan_job("b")], {"b": "a 승인 대기"})
    [item] = items_of(root, "plan")
    assert "PLAN_WAITING" in "\n".join(item["lines"])
    assert "b: a 승인 대기" in "\n".join(item["lines"])
    assert item["commands"] == ["python scripts/dispatch.py --run-plan P001"]


def test_running_plan_without_waiting_jobs_is_not_listed(root: Path) -> None:
    add_plan(root, "P001", "RUNNING", [plan_job("a")])
    assert items_of(root, "plan") == []


def test_human_items_of_unfinished_plans(root: Path) -> None:
    jobs = [plan_job("a"), plan_job("b", "human", "잠긴 경로: scripts/x.py"),
            plan_job("c", "needs_decision", "검증 약화")]  # fmt: skip
    add_plan(root, "P001", "STOPPED", jobs)
    add_plan(root, "P002", "DONE", [plan_job("z", "human", "잠긴 경로")])
    human = items_of(root, "human")
    assert [i["id"] for i in human] == ["P001:b", "P001:c"]
    assert "잠긴 경로: scripts/x.py" in "\n".join(human[0]["lines"])
    assert "결정" in human[1]["title"]
    assert human[0]["requirement"] == "b 할 일"


def test_plan_items_come_after_blocked_and_before_push(root: Path) -> None:
    add_job(root, "J001", "BLOCKED", blocked={"reason": "BUDGET", "detail": "$5"})
    add_plan(root, "P001", "CHECKED", [plan_job("a"), plan_job("b", "human", "잠김")])
    git(root, "commit", "-q", "--allow-empty", "-m", "x")
    assert [k for k, _ in kinds(root)] == ["blocked", "plan", "human", "push"]


def test_warning_when_actual_risk_differs_from_plan(root: Path) -> None:
    add_job(root, "J001", "APPROVAL_REQUIRED", risk="MEDIUM", expected_risk="LOW",
            plan_id="P001", plan_job="a")  # fmt: skip
    add_job(root, "J002", "APPROVAL_REQUIRED", risk="MEDIUM", expected_risk="MEDIUM")
    [item] = items_of(root, "warning")
    assert item["id"] == "J001"
    assert "LOW" in item["title"] and "MEDIUM" in item["title"]


def test_read_samples_are_recent_auto_merged_low_jobs(root: Path) -> None:
    for n in range(1, 5):
        add_record(root, f"J00{n}", "MERGED", merge_commit=f"{n}" * 40,
                   history=auto_merged(NOW - n * DAY))  # fmt: skip
    add_record(root, "J005", "MERGED", merge_commit="5" * 40, history=auto_merged(NOW - 8 * DAY))
    add_record(root, "J006", "MERGED", risk="MEDIUM", merge_commit="6" * 40,
               history=auto_merged(NOW - DAY))  # fmt: skip
    approved = [{"status": "APPROVAL_REQUIRED", "at": NOW - DAY}, *auto_merged(NOW - DAY)]
    add_record(root, "J007", "MERGED", merge_commit="7" * 40, history=approved)
    samples = items_of(root, "review")
    assert len(samples) == 2  # 기본: 주 2건
    assert {i["id"] for i in samples} <= {"J001", "J002", "J003", "J004"}
    sample = samples[0]
    commit = f"{sample['id'][-1]}" * 40
    assert sample["commands"] == [f"git show --first-parent {commit}"]
    # 같은 기록과 같은 날이면 같은 표본 (재현 가능한 고정 규칙)
    assert items_of(root, "review") == samples


def test_read_sample_does_not_change_when_other_jobs_age_out(root: Path) -> None:
    for n in range(1, 6):
        add_record(root, f"J00{n}", "MERGED", merge_commit=f"{n}" * 40,
                   history=auto_merged(NOW - n * DAY))  # fmt: skip
    today = {i["id"] for i in items_of(root, "review")}
    tomorrow = {i["id"] for i in items_of(root, "review", now=NOW + DAY)}
    # 표본은 작업 id로 정해진다: 기간 안에 남아 있는 표본은 다음 날에도 표본이다
    kept = {j for j in today if int(j[-1]) + 1 < 7}
    assert kept <= tomorrow


def test_review_and_cleanup_are_not_in_status_line(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_record(root, "J001", "MERGED", merge_commit="1" * 40, history=auto_merged(NOW))
    brief.main(["--counts"], root=root)
    assert capsys.readouterr().out.strip() == ""


def test_old_dropped_job_with_worktree_and_branch_is_cleanup_candidate(root: Path) -> None:
    git(root, "branch", "factory/J001")
    add_job(root, "J001", "DROPPED", at=NOW - 10 * DAY, branch="factory/J001")
    add_job(root, "J002", "DROPPED", at=NOW - 2 * DAY, branch="factory/J002")  # 아직 최근
    [item] = items_of(root, "cleanup")
    assert item["id"] == "J001"
    worktree = root.parent / "repo.jobs" / "J001"
    assert item["commands"] == [f"git worktree remove {worktree}", "git branch -D factory/J001"]


def test_cleanup_skips_jobs_with_nothing_left(root: Path) -> None:
    add_record(root, "J001", "DROPPED", history=[{"status": "DROPPED", "at": NOW - 30 * DAY}],
               branch="factory/J001")  # fmt: skip
    assert items_of(root, "cleanup") == []
    git(root, "branch", "factory/J001")
    [item] = items_of(root, "cleanup")
    assert item["commands"] == ["git branch -D factory/J001"]


def test_old_blocked_job_is_cleanup_candidate_after_drop(root: Path) -> None:
    add_job(root, "J001", "BLOCKED", at=NOW - 10 * DAY, branch="factory/J001",
            blocked={"reason": "TIMEOUT", "detail": "15분"})  # fmt: skip
    assert kinds(root) == [("blocked", "J001"), ("cleanup", "J001")]
    cleanup = items_of(root, "cleanup")[0]
    assert "drop" in "\n".join(cleanup["lines"])
    # 화면 카드는 키로 붙는다: 정리 후보가 BLOCKED 항목을 덮어쓰지 않게 키를 두지 않는다
    assert "key" not in cleanup
