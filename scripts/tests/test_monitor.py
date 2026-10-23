import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import factory  # noqa: E402
import monitor  # noqa: E402

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
NOW = 1_800_000_000.0


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def state_job(job_id: str, status: str, **extra: Any) -> dict[str, Any]:
    history = extra.pop("history", [("QUEUED", NOW - 600), (status, NOW - 60)])
    job: dict[str, Any] = {
        "id": job_id,
        "requirement": f"요구사항 {job_id}",
        "status": status,
        "created_at": history[0][1],
        "history": [{"status": s, "at": at} for s, at in history],
        "time": {"active_s": 300.0, "cap_s": 900},
        "risk": "LOW",
        "risk_reasons": ["contract 선언: LOW"],
        "acceptance_tests": [f"backend/tests/acceptance/test_{job_id}.py"],
        "changed_tests": [],
        "red": [{"verdict": "RED_OK"}],
        "validation": [],
        "reviews": [],
        "attempts": 0,
        "agent": "claude",
        "builder_model": "sonnet",
        "models": {
            "builder": {
                "alias": "sonnet",
                "model": SONNET,
                "source": "auto",
                "reason": "Builder 기본",
            }
        },  # fmt: skip
        "model_changes": [],
        "blocked": None,
        "blocks": [],
        "budgets_applied": {
            "session": 5.0,
            "reviewer_call": 0.5,
            "reviewer_total": 1.5,
            "job_total": 6.5,
        },  # fmt: skip
        "base_branch": "main",
        "branch": f"factory/{job_id}",
    }
    job.update(extra)
    return job


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def assistant(msg_id: str, model: str, usage: dict[str, int], parent: str | None = None,
              content: list[dict[str, Any]] | None = None,
              ts: str = "2027-01-15T08:00:00Z") -> str:  # fmt: skip
    return json.dumps({"type": "assistant", "parent_tool_use_id": parent, "timestamp": ts,
                       "message": {"id": msg_id, "model": model, "usage": usage,
                                   "content": content or []}})  # fmt: skip


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    """기본 checkout(root)과 작업 폴더(jobs): 진행 중·승인 대기·기록만 남은 작업·dispatch 오류."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "factory").mkdir()
    (root / "factory" / "config.json").write_text(
        (SCRIPTS.parent / "factory" / "config.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    jobs = tmp_path / "repo.jobs"
    write_json(jobs / "J001" / "factory" / "state.json",
               {"active": "J001", "jobs": {"J001": state_job("J001", "BUILDING")}})  # fmt: skip
    reviews = [{"round": 1, "reviewer": r, "verdict": "PASS", "issues": [], "cost_usd": 0.09,
                "tokens": 35000, "usage": {"in": 8, "out": 1200, "cr": 30000, "cw": 3900},
                "path": f"factory/reviews/J002-1-{r}.txt"}
               for r in ("code", "security", "test")]  # fmt: skip
    write_json(jobs / "J002" / "factory" / "state.json", {"active": "J002", "jobs": {
        "J002": state_job("J002", "APPROVAL_REQUIRED", risk="MEDIUM", reviews=reviews,
                          risk_reasons=["MEDIUM: DB 스키마 변경 (repository.py)"])}})  # fmt: skip
    (jobs / "J002" / "factory" / "reviews").mkdir(parents=True)
    (jobs / "J002" / "factory" / "reviews" / "J002-1-code.txt").write_text(
        "VERDICT: PASS <script>alert(1)</script>", encoding="utf-8"
    )
    write_json(jobs / "records" / "J000.json", state_job(
        "J000", "MERGED", merge_commit="abc1234", history=[("QUEUED", NOW - 3600),
        ("APPROVED", NOW - 3000), ("MERGED", NOW - 2990)]))  # fmt: skip
    write_json(jobs / "meta" / "J002" / "session.json", {"isolation": "host", "agent": "claude",
               "orchestrator": {"model": OPUS, "alias": "opus", "source": "auto",
                                "reason": "오케스트레이터 기본",
                                "auth": "subscription"}})  # fmt: skip
    write_json(jobs / "dispatch-20270115-080000.json", {
        "started": "20270115-080000", "base": "main", "agent": "claude", "max_parallel": 3,
        "jobs": [{"job_id": "J001", "status": "BUILDING"}, {"job_id": "J002", "status": "X"},
                 {"job_id": None, "status": "SETUP_FAILED", "requirement": "태그 색",
                  "error": "npm ci 실패"}]})  # fmt: skip
    logs = jobs / "logs"
    logs.mkdir()
    (logs / "J001.jsonl").write_text("\n".join([
        assistant("m1", OPUS, {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 1000,
                               "cache_creation_input_tokens": 100},
                  content=[{"type": "tool_use", "name": "Bash",
                            "input": {"command": "python scripts/factory.py red J001"}}]),
        assistant("m1", OPUS, {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 1000,
                               "cache_creation_input_tokens": 100}),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.26, "resetsAt": 1},
            "seven_day": {"utilization": 0.12}}}}),
        assistant("m2", OPUS, {"input_tokens": 1, "output_tokens": 1},
                  content=[{"type": "tool_use", "name": "Bash",
                            "input": {"command": "echo sk-ant-api03-SECRETSECRETSECRET"}}]),
    ]) + "\n", encoding="utf-8")  # fmt: skip
    return {"root": root, "jobs": jobs}


def collector(world: dict[str, Path]) -> "monitor.Collector":
    return monitor.Collector(world["root"], world["jobs"], now=lambda: NOW)


# ---------- D4 상태 → 컬럼 ----------


def test_every_factory_status_has_a_column() -> None:
    statuses = set(factory.ACTIVE_STATUSES) | set(factory.CLOSED_STATUSES) | {
        "APPROVAL_REQUIRED", "APPROVED", "BLOCKED"}  # fmt: skip
    for status in statuses:
        assert status in monitor.COLUMN_OF, status
    assert monitor.column_of("SETUP_FAILED") == "error"
    assert monitor.column_of("WEIRD") is None
    assert monitor.column_of("DROPPED") == "hidden"


# ---------- D1 작업 찾기 ----------


def test_discover_finds_all_sources(world: dict[str, Path]) -> None:
    refs = {r["key"]: r for r in monitor.discover(world["root"], world["jobs"])}
    assert refs["J001"]["source"] == "worktree"
    assert refs["J000"]["source"] == "record"
    assert any(r["source"] == "dispatch" and r["status"] == "SETUP_FAILED" for r in refs.values())


def test_discover_ignores_non_job_folders(world: dict[str, Path]) -> None:
    write_json(world["jobs"] / "evil" / "factory" / "state.json", {"jobs": {}})
    write_json(world["jobs"] / "J001x" / "factory" / "state.json", {"jobs": {}})
    keys = {r["key"] for r in monitor.discover(world["root"], world["jobs"])}
    assert "evil" not in keys and "J001x" not in keys


def test_worktree_wins_over_record(world: dict[str, Path]) -> None:
    write_json(world["jobs"] / "records" / "J001.json", state_job("J001", "QUEUED"))
    refs = [r for r in monitor.discover(world["root"], world["jobs"]) if r["key"] == "J001"]
    assert len(refs) == 1 and refs[0]["source"] == "worktree"


def test_interactive_job_with_same_id_is_kept_apart(world: dict[str, Path]) -> None:
    write_json(world["root"] / "factory" / "state.json",
               {"active": "J001", "jobs": {"J001": state_job("J001", "SPEC_READY")}})  # fmt: skip
    refs = monitor.discover(world["root"], world["jobs"])
    keys = {r["key"] for r in refs if r["id"] == "J001"}
    assert keys == {"J001", "local-J001"}


# ---------- D2 파일 캐시, D3 로그 이어 읽기 ----------


def test_file_cache_rereads_only_changed_and_keeps_last_good(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    cache = monitor.FileCache()
    assert cache.read_json(path) == {"a": 1}
    reads = cache.reads
    assert cache.read_json(path) == {"a": 1} and cache.reads == reads
    time.sleep(0.01)
    path.write_text('{"a": ', encoding="utf-8")  # 쓰는 중
    assert cache.read_json(path) == {"a": 1}
    assert cache.errors(path) == 1


def test_log_tail_reads_complete_lines_only(tmp_path: Path) -> None:
    log = tmp_path / "J001.jsonl"
    log.write_bytes(b'{"n": 1}\n{"n": ')
    tail = monitor.LogTail()
    assert [e["n"] for e in tail.read_new(log)] == [1]
    with log.open("ab") as f:
        f.write(b'2}\n{"n": 3}\n')
    assert [e["n"] for e in tail.read_new(log)] == [2, 3]
    log.write_bytes(b'{"n": 9}\n')  # 새로 시작 (작아짐)
    assert [e["n"] for e in tail.read_new(log)] == [9]


# ---------- D5 스냅샷 ----------


def test_snapshot_shape(world: dict[str, Path]) -> None:
    snap = collector(world).snapshot()
    assert snap["repo"]["base"] == "main"
    assert snap["dispatch"]["stamp"] == "20270115-080000"
    assert snap["subscription"]["five_hour"] == 0.26
    cards = {c["key"]: c for c in snap["jobs"]}
    assert cards["J001"]["column"] == "build"
    assert cards["J002"]["column"] == "approval"
    assert cards["J000"]["column"] == "merged"
    assert cards["J001"]["tokens"]["cr"] == 1000  # 같은 응답은 한 번만
    assert cards["J002"]["reviewers"] == {"code": "PASS", "security": "PASS", "test": "PASS"}
    # 주의 줄: 승인 대기 → 오류 순서
    kinds = [a["status"] for a in snap["attention"]]
    assert kinds[0] == "APPROVAL_REQUIRED" and "SETUP_FAILED" in kinds


def test_snapshot_warns_on_unknown_status(world: dict[str, Path]) -> None:
    write_json(world["jobs"] / "J009" / "factory" / "state.json",
               {"jobs": {"J009": state_job("J009", "WEIRD_STATE")}})  # fmt: skip
    snap = collector(world).snapshot()
    assert any("WEIRD_STATE" in w for w in snap["warnings"])
    assert "J009" not in {c["key"] for c in snap["jobs"]}


def test_snapshot_kpi_matches_report(world: dict[str, Path]) -> None:
    import report

    snap = collector(world).snapshot()
    jobs = [r["job"] for r in monitor.discover(world["root"], world["jobs"]) if r.get("job")]
    assert snap["kpi"]["success_rate"] == report.metrics(jobs)["success_rate"]


# ---------- D6 작업 상세 ----------


def test_detail_next_commands_and_trust(world: dict[str, Path]) -> None:
    detail = collector(world).detail("J002")
    assert detail["next"][0] == "python scripts/dispatch.py --approve J002"
    assert detail["evidence"]["trust"] == "session"
    assert detail["orchestrator"]["auth"] == "subscription"
    assert detail["orchestrator"]["trust"] == "host"
    roles = {r["role"]: r for r in detail["cost"]["roles"]}
    assert roles["reviewer_code"]["usage"]["cr"] == 30000
    assert detail["cost"]["job"]["reviewers_usd"] == 0.27


def test_detail_history_groups_repeats(world: dict[str, Path]) -> None:
    write_json(world["jobs"] / "J003" / "factory" / "state.json", {"jobs": {"J003": state_job(
        "J003", "BUILDING", history=[("QUEUED", 1), ("SPEC_READY", 2), ("SPEC_READY", 3),
                                     ("SPEC_READY", 4), ("BUILDING", 5)])}})  # fmt: skip
    hist = collector(world).detail("J003")["history"]
    assert [(h["status"], h["count"]) for h in hist] == [("QUEUED", 1), ("SPEC_READY", 3),
                                                         ("BUILDING", 1)]  # fmt: skip


def test_detail_commands_never_contain_free_text(world: dict[str, Path]) -> None:
    write_json(world["jobs"] / "J004" / "factory" / "state.json", {"jobs": {"J004": state_job(
        "J004", "BLOCKED", requirement="; rm -rf / #", blocked={"reason": "CONFLICT",
        "detail": "x"})}})  # fmt: skip
    for cmd in collector(world).detail("J004")["next"]:
        assert "rm -rf" not in cmd


def test_detail_unknown_key(world: dict[str, Path]) -> None:
    assert collector(world).detail("J777") is None
    assert collector(world).detail("../etc") is None


# ---------- D7 최근 활동 ----------


def test_activity_lines_and_secret_masking(world: dict[str, Path]) -> None:
    items = collector(world).activity("J001", after=0)
    texts = [i["text"] for i in items]
    assert any("python scripts/factory.py red J001" in t for t in texts)
    assert not any("SECRETSECRET" in t for t in texts)
    assert any("[가림]" in t for t in texts)
    later = collector(world).activity("J001", after=items[0]["n"])
    assert all(i["n"] > items[0]["n"] for i in later)


# ---------- D11a 레인·충돌 예고 ----------


def test_lanes_and_conflicts() -> None:
    jobs = [
        {"key": "J001", "status": "BUILDING", "history": [{"status": "QUEUED", "at": 0},
                                                          {"status": "BUILDING", "at": 60}]},
        {"key": "J002", "status": "APPROVAL_REQUIRED",
         "history": [{"status": "QUEUED", "at": 0}, {"status": "APPROVAL_REQUIRED", "at": 30}]},
    ]  # fmt: skip
    changed = {
        "J001": ["frontend/src/List.tsx", "factory/specs/J001.md"],
        "J002": ["frontend/src/List.tsx", "factory/specs/J002.md", "factory/state.json"],
    }
    lanes, conflicts = monitor.lanes(jobs, changed, now=100)
    j1 = next(lane for lane in lanes if lane["key"] == "J001")
    assert [(s["stage"], s["end"]) for s in j1["segments"]] == [("spec", 60), ("build", None)]
    j2 = next(lane for lane in lanes if lane["key"] == "J002")
    assert j2["segments"][-1]["stage"] == "wait"
    # 팩토리 산출물은 충돌로 보지 않는다
    assert conflicts == [{"a": "J001", "b": "J002", "files": ["frontend/src/List.tsx"]}]
