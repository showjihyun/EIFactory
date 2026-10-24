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


def test_card_active_time_counts_running_segment(world: dict[str, Path]) -> None:
    # 상태 파일의 active_s는 상태가 바뀔 때만 갱신된다. 화면은 지금 시각까지 센다 (D17에서 발견)
    job = state_job("J010", "BUILDING")
    job["time"] = {"segments": [[NOW - 400, NOW - 300], [NOW - 120, None]],
                   "active_s": 100.0, "cap_s": 900}  # fmt: skip
    write_json(world["jobs"] / "J010" / "factory" / "state.json", {"jobs": {"J010": job}})
    cards = {c["key"]: c for c in collector(world).snapshot()["jobs"]}
    assert cards["J010"]["time"]["active_s"] == 220.0
    assert cards["J002"]["time"]["active_s"] == 300.0  # 구간이 없으면 기록값


def test_usage_reprices_when_another_session_finishes(world: dict[str, Path]) -> None:
    # D17: J001 로그가 그대로여도(Builder 실행 중) 다른 세션이 끝나 단가가 생기면 추정이 보여야 한다
    clock = [NOW]
    col = monitor.Collector(world["root"], world["jobs"], now=lambda: clock[0])
    ref = next(r for r in col.refs() if r["key"] == "J001")
    assert col.usage(ref)["total"]["usd"] is None
    result = {"type": "result", "total_cost_usd": 1.0,
              "modelUsage": {OPUS: {"inputTokens": 1000, "outputTokens": 0,
                                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                                    "costUSD": 1.0}}}  # fmt: skip
    (world["jobs"] / "logs" / "J099.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    clock[0] = NOW + 31
    assert col.usage(ref)["total"]["usd"] is not None


def test_usage_adds_continued_sessions(world: dict[str, Path]) -> None:
    # 충돌을 풀러 이어서 처리한 세션(<ID>.r2.jsonl)도 그 작업의 비용이다
    def result(usd: float) -> str:
        return json.dumps({"type": "result", "total_cost_usd": usd,
                           "modelUsage": {OPUS: {"inputTokens": 100, "outputTokens": 10,
                                                 "cacheReadInputTokens": 0,
                                                 "cacheCreationInputTokens": 0,
                                                 "costUSD": usd}}}) + "\n"  # fmt: skip

    logs = world["jobs"] / "logs"
    (logs / "J001.jsonl").write_text(result(0.5), encoding="utf-8")
    (logs / "J001.r2.jsonl").write_text(result(0.25), encoding="utf-8")
    col = collector(world)
    ref = next(r for r in col.refs() if r["key"] == "J001")
    usage = col.usage(ref)
    assert usage is not None
    assert usage["total"]["usd"] == pytest.approx(0.75)
    assert usage["total"]["in"] == 200
    assert usage["estimated"] is False
    # 이어서 처리한 로그는 다른 작업으로 보이지 않는다
    assert "J001.r2" not in {r["key"] for r in col.refs()}


def test_snapshot_counts_unpriced_jobs(world: dict[str, Path]) -> None:
    # 확정 세션 로그가 없으면 단가가 없다. 진행 중 J001은 토큰만 있고 비용은 "모름"
    snap = collector(world).snapshot()
    cards = {c["key"]: c for c in snap["jobs"]}
    assert cards["J001"]["cost"]["unpriced"] is True
    assert snap["kpi"]["cost_unpriced"] == sum(
        1 for c in snap["jobs"] if c["cost"] and c["cost"].get("unpriced")
    )


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
    assert conflicts == [
        {"a": "J001", "b": "J002", "files": ["frontend/src/List.tsx"], "kind": "running"}
    ]


def test_lanes_warn_on_files_merged_to_base_after_fork() -> None:
    # D17: 먼저 끝난 J001이 머지되면 진행 중 쌍 예고는 사라진다. 늦은 J002의 위험은 그때부터다
    jobs = [
        {"key": "J001", "status": "MERGED", "history": []},
        {"key": "J002", "status": "BUILDING", "history": []},
    ]
    changed = {"J002": ["frontend/src/List.tsx", "factory/specs/J002.md"]}
    base = {"J002": {"J001": ["frontend/src/List.tsx", "factory/specs/J001.md", "README.md"]}}
    _, conflicts = monitor.lanes(jobs, changed, now=100, base_changed=base)
    assert conflicts == [
        {"a": "J001", "b": "J002", "files": ["frontend/src/List.tsx"], "kind": "base"}
    ]
    # 겹치는 파일이 없거나 base 변경이 없으면 예고하지 않는다
    assert monitor.lanes(jobs, changed, now=100, base_changed={"J002": {"J001": ["x"]}})[1] == []
    assert monitor.lanes(jobs, changed, now=100)[1] == []


def test_snapshot_detects_base_overlap_with_real_worktree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "List.tsx").write_text("a\n", encoding="utf-8")
    (root / "src" / "Form.tsx").write_text("f\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, *ident, "commit", "-qm", "base")
    jobs = tmp_path / "repo.jobs"
    git(root, "worktree", "add", "-q", "-b", "factory/J002", str(jobs / "J002"))
    write_json(jobs / "J002" / "factory" / "state.json",
               {"active": "J002", "jobs": {"J002": state_job("J002", "BUILDING")}})  # fmt: skip
    (jobs / "J002" / "src" / "List.tsx").write_text("a\nj002\n", encoding="utf-8")
    # J002가 갈라진 뒤 main에 J001이 머지됨 (같은 파일 + 다른 파일)
    (root / "src" / "List.tsx").write_text("j001\na\n", encoding="utf-8")
    (root / "src" / "Form.tsx").write_text("f2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, *ident, "commit", "-qm", "factory J001: 빈 목록 문구")
    snap = monitor.Collector(root, jobs, now=lambda: NOW).snapshot()
    assert snap["conflicts"] == [
        {"a": "J001", "b": "J002", "files": ["src/List.tsx"], "kind": "base"}
    ]


# ---------- D8 서버 ----------

import http.client  # noqa: E402
import threading  # noqa: E402


@pytest.fixture
def server(world: dict[str, Path]):  # type: ignore[no-untyped-def]
    srv = monitor.make_server(world["root"], world["jobs"], port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def req(srv: Any, path: str, method: str = "GET", headers: dict[str, str] | None = None
        ) -> tuple[int, dict[str, str], bytes]:  # fmt: skip
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body


def test_server_binds_loopback_only(server: Any) -> None:
    assert server.server_address[0] == "127.0.0.1"


def test_server_rejects_non_get(server: Any) -> None:
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert req(server, "/api/snapshot", method)[0] == 405


def test_server_checks_host_and_origin(server: Any) -> None:
    assert req(server, "/api/snapshot", headers={"Host": "evil.com"})[0] == 421
    assert req(server, "/api/snapshot", headers={"Origin": "http://evil.com"})[0] == 403
    assert req(server, "/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    port = server.server_address[1]
    ok = req(server, "/api/snapshot", headers={"Origin": f"http://127.0.0.1:{port}"})
    assert ok[0] == 200


def test_server_routes_and_security_headers(server: Any) -> None:
    status, headers, body = req(server, "/api/snapshot")
    assert status == 200
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in headers
    assert json.loads(body)["repo"]["base"] == "main"
    assert req(server, "/api/job/J002")[0] == 200
    assert req(server, "/api/job/J777")[0] == 404
    assert req(server, "/api/job/..%2F..%2Fetc")[0] == 404
    assert req(server, "/api/nope")[0] == 404
    assert req(server, "/")[0] == 200
    settings = json.loads(req(server, "/api/settings")[2])
    assert "reviewer_test" in settings["stages"] and "job_total" in settings["budgets"]


# ---------- API 키 입력 (설정 화면, 쓰기는 이것 하나) ----------

FAKE_KEY = "sk-ant-api03-" + "A1b2C3d4" * 10


class FakeKeyStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def is_set(self, name: str) -> bool:
        return name in self.values

    def save(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> FakeKeyStore:
    store = FakeKeyStore()
    monkeypatch.setattr(monitor, "KEY_STORE", store)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return store


def post_key(srv: Any, data: Any, token: str | None = None, origin: str | None = "same",
             content_type: str = "application/json") -> tuple[int, bytes]:  # fmt: skip
    port = srv.server_address[1]
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": content_type}
    if origin == "same":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    elif origin:
        headers["Origin"] = origin
    if token is not None:
        headers["X-Factory-Token"] = token
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/settings/api-key", body=json.dumps(data), headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def key_token(srv: Any) -> str:
    return str(json.loads(req(srv, "/api/settings")[2])["api_key_input"]["token"])


def test_settings_shows_only_whether_keys_are_set(
    server: Any, keys: FakeKeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys.values["ANTHROPIC_API_KEY"] = FAKE_KEY
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-" + "Z" * 40)
    body = req(server, "/api/settings")[2]
    info = json.loads(body)["api_key_input"]
    assert info["keys"]["ANTHROPIC_API_KEY"]["set"] is True
    assert info["keys"]["OPENAI_API_KEY"]["set"] is True  # 지금 환경에 있음
    assert info["keys"]["CODEX_API_KEY"]["set"] is False
    # 값은 일부라도 내보내지 않는다
    assert FAKE_KEY[-8:].encode() not in body and b"ZZZZ" not in body


def test_api_key_input_saves_with_token_and_never_echoes(server: Any, keys: FakeKeyStore) -> None:
    status, body = post_key(server, {"name": "ANTHROPIC_API_KEY", "value": FAKE_KEY},
                            token=key_token(server))  # fmt: skip
    assert status == 200
    assert keys.values == {"ANTHROPIC_API_KEY": FAKE_KEY}
    assert FAKE_KEY[-8:].encode() not in body
    assert json.loads(body)["set"] is True


def test_api_key_input_rejects_cross_site_and_missing_token(
    server: Any, keys: FakeKeyStore
) -> None:
    token = key_token(server)
    data = {"name": "ANTHROPIC_API_KEY", "value": FAKE_KEY}
    assert post_key(server, data)[0] == 403  # 토큰 없음
    assert post_key(server, data, token="wrong")[0] == 403
    assert post_key(server, data, token=token, origin=None)[0] == 403  # 출처 없음
    assert post_key(server, data, token=token, origin="http://evil.com")[0] == 403
    assert post_key(server, data, token=token, content_type="text/plain")[0] == 415
    assert keys.values == {}


def test_api_key_input_validates_name_and_value(server: Any, keys: FakeKeyStore) -> None:
    token = key_token(server)
    bad: list[dict[str, Any]] = [
        {"name": "PATH", "value": FAKE_KEY},
        {"name": "ANTHROPIC_API_KEY", "value": "not-a-key"},
        {"name": "ANTHROPIC_API_KEY", "value": FAKE_KEY + " \n"},
        {"name": "ANTHROPIC_API_KEY", "value": "sk-ant-" + "a" * 600},
        {"name": "ANTHROPIC_API_KEY", "value": 123},
    ]
    for data in bad:
        status, body = post_key(server, data, token=token)
        assert status == 400, data
        value = data["value"]
        assert not (isinstance(value, str) and value[-12:].strip().encode() in body)
    assert keys.values == {}


def test_api_key_input_can_delete(server: Any, keys: FakeKeyStore) -> None:
    keys.values["ANTHROPIC_API_KEY"] = FAKE_KEY
    status, body = post_key(server, {"name": "ANTHROPIC_API_KEY", "value": None},
                            token=key_token(server))  # fmt: skip
    assert status == 200 and json.loads(body)["set"] is False
    assert keys.values == {}


def test_server_etag_304(server: Any) -> None:
    _, headers, _ = req(server, "/api/job/J002")
    status, _, body = req(server, "/api/job/J002", headers={"If-None-Match": headers["etag"]})
    assert status == 304 and body == b""


def test_server_review_file_is_text_and_limited(server: Any, world: dict[str, Path]) -> None:
    status, headers, body = req(server, "/api/job/J002/review/J002-1-code.txt")
    assert status == 200 and headers["content-type"].startswith("text/plain")
    assert b"<script>" in body  # 원문 그대로 (화면이 textContent로 넣는다)
    assert req(server, "/api/job/J002/review/..%2Fstate.json")[0] == 404
    assert req(server, "/api/job/J002/review/J001-1-code.txt")[0] == 404  # 다른 작업 파일
    big = world["jobs"] / "J002" / "factory" / "reviews" / "J002-1-test.txt"
    big.write_text("x" * 100_000, encoding="utf-8")
    assert len(req(server, "/api/job/J002/review/J002-1-test.txt")[2]) <= 64 * 1024 + 100


def test_server_internal_error_hides_details(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Any) -> None:
        raise RuntimeError("C:/secret/path")

    monkeypatch.setattr(monitor.Collector, "snapshot", boom)
    status, _, body = req(server, "/api/snapshot")
    assert status == 500 and b"secret" not in body


def test_activity_endpoint(server: Any) -> None:
    items = json.loads(req(server, "/api/job/J001/activity?after=0")[2])
    assert items and all("[가림]" in i["text"] or "SECRET" not in i["text"] for i in items)


def test_stage_shares_split_by_history() -> None:
    def ev(i: str, at: float, tokens: int) -> dict[str, Any]:
        return {"type": "assistant", "timestamp": at,
                "message": {"id": i, "usage": {"input_tokens": tokens}}}  # fmt: skip

    history = [{"status": "QUEUED", "at": 0}, {"status": "BUILDING", "at": 100},
               {"status": "VERIFYING", "at": 200}]  # fmt: skip
    events = [ev("a", 10, 100), ev("a", 11, 100), ev("b", 150, 300), ev("c", 250, 100)]
    shares = dict(monitor.stage_shares(events, history, reviewers_tokens=500))
    # spec 100, build 300, review 100 + 리뷰어 500 → 합 1000
    assert shares == {"spec": 0.1, "build": 0.3, "review": 0.6}


def test_mascot_assets_are_whitelisted(server: Any) -> None:
    status, headers, body = req(server, "/assets/bolt_MERGED.png")
    assert status == 200 and headers["content-type"] == "image/png" and body[:4] == b"\x89PNG"
    assert req(server, "/assets/bolt_NOPE.png")[0] == 404
    assert req(server, "/assets/..%2Fmonitor.py")[0] == 404


def test_reviewer_usage_missing_is_not_zero(world: dict[str, Path]) -> None:
    write_json(world["jobs"] / "J005" / "factory" / "state.json", {"jobs": {"J005": state_job(
        "J005", "VERIFYING", reviews=[{"round": 1, "reviewer": "code", "verdict": "PASS",
                                       "cost_usd": 0.09, "tokens": 35000}])}})  # fmt: skip
    roles = {r["role"]: r for r in collector(world).detail("J005")["cost"]["roles"]}
    assert roles["reviewer_code"]["usage"] is None  # 예전 기록: 항목별 토큰 없음 (0이 아님)
