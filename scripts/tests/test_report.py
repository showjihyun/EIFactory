import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import report  # noqa: E402

T0 = 1_000_000.0


def job(job_id: str, statuses: list[tuple[str, float]], **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": job_id,
        "requirement": f"요구사항 {job_id}",
        "status": statuses[-1][0],
        "created_at": T0,
        "history": [{"status": s, "at": T0 + at} for s, at in statuses],
        "time": {"active_s": statuses[-1][1], "cap_s": 900},
        "attempts": 0,
        "reviews": [],
        "blocks": [],
        "model_changes": [],
        "agent": "claude",
    }
    data.update(extra)
    return data


PASS_REVIEW = {"reviewer": "code", "verdict": "PASS", "issues": [], "cost_usd": 0.02, "tokens": 900}
FAIL_REVIEW = {"reviewer": "test", "verdict": "FAIL", "issues": ["assertion 약화"]}
J004_FLOW = [("QUEUED", 0), ("BLOCKED", 100), ("BUILDING", 700), ("APPROVED", 900), ("MERGED", 910)]

JOBS = [
    # 자동 머지: 5분에 승인 도달
    job("J001", [("QUEUED", 0), ("VERIFYING", 200), ("APPROVED", 300), ("MERGED", 320)],
        merge_commit="m1", risk="LOW",
        reviews=[PASS_REVIEW]),
    # 사람 승인 대기: 7분
    job("J002", [("QUEUED", 0), ("APPROVAL_REQUIRED", 420)], risk="HIGH"),
    # 막힘: 사람이 풀어야 함
    job("J003", [("QUEUED", 0), ("BLOCKED", 600)], blocks=[{"reason": "TIMEOUT", "at": T0 + 600}],
        attempts=2, model_changes=[{"attempt": 2, "model": "opus"}],
        reviews=[FAIL_REVIEW]),
    # 막혔다가 사람이 resume → 머지
    job("J004", J004_FLOW,
        blocks=[{"reason": "RETRY_LIMIT", "at": T0 + 100}], merge_commit="m4", risk="LOW",
        reviews=[FAIL_REVIEW]),
]  # fmt: skip


def test_metrics_follow_kpi_definitions() -> None:
    m = report.metrics(JOBS)
    assert m["total"] == 4
    assert m["success_rate"] == 0.75  # 승인 대기(또는 자동 승인)에 도달한 비율
    assert m["intervention_rate"] == 0.5  # BLOCKED를 거친 작업 (사람이 풀어야 함)
    assert m["lead_time_min"] == {"min": 5.0, "median": 7.0, "max": 15.0}
    assert m["blocked_reasons"] == {"TIMEOUT": 1, "RETRY_LIMIT": 1}
    assert m["promotions"] == 1
    assert m["by_status"] == {"MERGED": 2, "APPROVAL_REQUIRED": 1, "BLOCKED": 1}
    assert m["review_issues"] == [("[test] assertion 약화", 2)]
    assert m["review_cost_usd"] == 0.02


def codex_usage(n: int) -> dict[str, int]:
    return {"input_tokens": n, "cached_input_tokens": 5, "output_tokens": 2}


def test_session_usage_from_claude_and_codex_logs(tmp_path: Path) -> None:
    claude = tmp_path / "J001.jsonl"
    claude.write_text(
        json.dumps({"type": "system"}) + "\n"
        + json.dumps({"type": "result", "total_cost_usd": 0.6,
                      "usage": {"input_tokens": 30, "cache_creation_input_tokens": 70,
                                "cache_read_input_tokens": 800, "output_tokens": 100}}) + "\n",
        encoding="utf-8",
    )  # fmt: skip
    codex = tmp_path / "J002.jsonl"
    codex.write_text(
        "\n".join(
            json.dumps({"type": "turn.completed", "usage": codex_usage(n)})
            for n in (10, 20)
        ),
        encoding="utf-8",
    )  # fmt: skip
    assert report.session_usage(claude) == {"cost_usd": 0.6, "tokens": 1000}
    assert report.session_usage(codex) == {"cost_usd": None, "tokens": 44}
    assert report.session_usage(tmp_path / "missing.jsonl") == {"cost_usd": None, "tokens": None}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


def test_reverted_merges_count_as_post_merge_defects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    merges = []
    for name in ("x", "y"):
        git(repo, "switch", "-qc", name)
        (repo / f"{name}.txt").write_text(name, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", name)
        git(repo, "switch", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", name, "-m", f"merge {name}")
        merges.append(git(repo, "rev-parse", "HEAD"))
    git(repo, "revert", "--no-edit", "-m", "1", merges[0])
    assert report.reverted(repo, "main", merges) == {merges[0]}


def test_render_lists_kpis_and_candidates() -> None:
    text = report.render(report.metrics(JOBS), defects=1, merged=2)
    assert "작업 성공률 | 75%" in text
    assert "사람 개입률 | 50%" in text
    assert "머지 후 결함 | 50% (1/2)" in text
    assert "오판율" in text and "정답표" in text  # 자동으로 셀 수 없어 사람이 확인
    assert "[test] assertion 약화 | 2" in text
    assert "TIMEOUT | 1" in text


def test_main_reads_records_dir(tmp_path: Path, capsys: Any) -> None:
    records = tmp_path / "records"
    records.mkdir()
    for item in JOBS:
        (records / f"{item['id']}.json").write_text(json.dumps(item), encoding="utf-8")
    out = tmp_path / "report.md"
    assert report.main(["--jobs-dir", str(tmp_path), "--out", str(out), "--no-git"]) == 0
    assert "작업 4건" in out.read_text(encoding="utf-8")


# ---------- 실시간 사용량 (대시보드 선행 P1·P3) ----------

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def assistant(msg_id: str, model: str, usage: dict[str, int], parent: str | None = None) -> str:
    return json.dumps({"type": "assistant", "parent_tool_use_id": parent, "timestamp": "t",
                       "message": {"id": msg_id, "model": model, "usage": usage}})  # fmt: skip


def u(i: int, o: int, cr: int = 0, cw: int = 0) -> dict[str, int]:
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw}  # fmt: skip


def write_log(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_live_usage_counts_each_response_once(tmp_path: Path) -> None:
    # stream-json은 한 응답(message.id)을 여러 이벤트로 보내며 사용량을 반복한다
    # (E02 실측: 이벤트 49개, 응답 26개)
    log = write_log(tmp_path / "J001.jsonl", [
        assistant("m1", OPUS, u(10, 5, 1000, 100)),
        assistant("m1", OPUS, u(10, 5, 1000, 100)),
        assistant("m1", OPUS, u(10, 5, 1000, 100)),
        assistant("m2", OPUS, u(2, 7, 1100, 0)),
        assistant("b1", SONNET, u(3, 4, 200, 50), parent="toolu_1"),
        "not json",
    ])  # fmt: skip
    live = report.live_usage(log, {OPUS: 1e-6, SONNET: 5e-7})
    assert live["basis"] == "stream" and live["estimated"] is True
    orch, builder = live["roles"]["orchestrator"], live["roles"]["builder"]
    assert (orch["in"], orch["out"], orch["cr"], orch["cw"]) == (12, 12, 2100, 100)
    assert orch["model"] == OPUS
    assert (builder["in"], builder["out"], builder["cr"], builder["cw"]) == (3, 4, 200, 50)
    assert orch["usd"] == round((12 + 12 + 2100 + 100) * 1e-6, 6)


def test_live_usage_builder_progress_fills_stream_gap(tmp_path: Path) -> None:
    progress = json.dumps({"type": "system", "subtype": "task_progress", "task_id": "a1",
                           "subagent_type": "builder",
                           "usage": {"total_tokens": 1000}})  # fmt: skip
    other = json.dumps({"type": "system", "subtype": "task_progress", "task_id": "x",
                        "subagent_type": "general", "usage": {"total_tokens": 99999}})  # fmt: skip
    log = write_log(tmp_path / "J001.jsonl", [assistant("b1", SONNET, u(3, 4, 200, 50), "t"),
                                              progress, other])  # fmt: skip
    builder = report.live_usage(log, {})["roles"]["builder"]
    # 스트림 합계 257 < 서브에이전트 누적 1000 → 차이는 나눌 수 없는 토큰으로
    assert builder["unattributed"] == 1000 - 257


def test_live_usage_confirms_from_result(tmp_path: Path) -> None:
    result = json.dumps({"type": "result", "total_cost_usd": 0.798, "modelUsage": {
        OPUS: {"inputTokens": 40, "outputTokens": 13925, "cacheReadInputTokens": 1000000,
               "cacheCreationInputTokens": 60000, "costUSD": 0.734},
        SONNET: {"inputTokens": 12, "outputTokens": 4100, "cacheReadInputTokens": 80000,
                 "cacheCreationInputTokens": 9000, "costUSD": 0.064}}})  # fmt: skip
    log = write_log(tmp_path / "J001.jsonl", [assistant("m1", OPUS, u(1, 1)), result])
    live = report.live_usage(log, {}, builder_model=SONNET)
    assert live["basis"] == "result" and live["estimated"] is False
    assert live["roles"]["orchestrator"]["usd"] == 0.734
    assert live["roles"]["builder"]["cr"] == 80000
    assert live["total"]["usd"] == 0.798


def test_live_usage_codex_turns(tmp_path: Path) -> None:
    log = write_log(tmp_path / "J001.jsonl", [
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100,
                    "cached_input_tokens": 800, "output_tokens": 30}}),
    ])  # fmt: skip
    live = report.live_usage(log, {})
    orch = live["roles"]["orchestrator"]
    assert live["basis"] == "turns"
    assert (orch["in"], orch["cr"], orch["out"], orch["cw"]) == (100, 800, 30, None)
    assert orch["usd"] is None  # 보정 단가가 없으면 금액을 지어내지 않는다


def test_live_usage_reads_subscription_rate_limit(tmp_path: Path) -> None:
    event = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.26, "resetsAt": 1792773000},
        "seven_day": {"utilization": 0.12, "resetsAt": 1793000000}}}})  # fmt: skip
    live = report.live_usage(write_log(tmp_path / "J001.jsonl", [event]), {})
    assert live["subscription"] == {"five_hour": 0.26, "seven_day": 0.12,
                                    "five_hour_resets_at": 1792773000}  # fmt: skip


def test_live_usage_missing_log(tmp_path: Path) -> None:
    live = report.live_usage(tmp_path / "none.jsonl", {})
    assert live["basis"] is None and live["roles"] == {}


def test_unit_prices_from_confirmed_sessions(tmp_path: Path) -> None:
    for n, (usd, tokens) in enumerate([(1.0, 1_000_000), (3.0, 1_000_000)]):
        write_log(tmp_path / f"J00{n}.jsonl", [json.dumps({"type": "result", "modelUsage": {
            OPUS: {"inputTokens": tokens, "outputTokens": 0, "cacheReadInputTokens": 0,
                   "cacheCreationInputTokens": 0, "costUSD": usd}}})])  # fmt: skip
    prices = report.unit_prices(sorted(tmp_path.glob("*.jsonl")))
    assert prices[OPUS] == 2e-6


def test_job_cost_adds_reviewers() -> None:
    reviews = [{"reviewer": r, "cost_usd": 0.094} for r in ("code", "security", "test")]
    cost = report.job_cost({"reviews": reviews}, {"total": {"usd": 0.8}, "estimated": False})
    assert cost == {"session_usd": 0.8, "reviewers_usd": 0.282, "total_usd": 1.082,
                    "estimated": False, "missing": []}  # fmt: skip
    partial = report.job_cost({"reviews": [{"reviewer": "code", "cost_usd": None}]}, None)
    assert partial["missing"] == ["code"] and partial["estimated"] is True
    assert partial["session_usd"] is None and partial["total_usd"] == 0.0


def test_live_usage_result_without_model_breakdown(tmp_path: Path) -> None:
    result = json.dumps({"type": "result", "total_cost_usd": 0.5,
                         "usage": {"input_tokens": 100, "output_tokens": 20}})  # fmt: skip
    live = report.live_usage(
        write_log(tmp_path / "J001.jsonl", [assistant("m1", OPUS, u(9, 9)), result]), {}
    )
    assert live["basis"] == "result" and live["estimated"] is False
    assert live["total"]["usd"] == 0.5
    assert live["roles"]["orchestrator"]["in"] == 9  # 역할 분해는 스트림 값


# ---------- Git 확인 (대시보드 선행 P4-2) ----------


def gitrepo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "t"],
                 ["config", "user.email", "t@t"]):  # fmt: skip
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.txt").write_text("1\n2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()  # fmt: skip


def test_merged_into(tmp_path: Path) -> None:
    repo = gitrepo(tmp_path)
    base = head(repo)
    subprocess.run(["git", "switch", "-qc", "side"], cwd=repo, check=True)
    (repo / "b.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "side"], cwd=repo, check=True)
    side = head(repo)
    assert report.merged_into(repo, base, "main") is True
    assert report.merged_into(repo, side, "main") is False
    assert report.merged_into(repo, "0" * 40, "main") is False


def test_changed_files_with_untracked_and_binary(tmp_path: Path) -> None:
    repo = gitrepo(tmp_path)
    base = head(repo)
    (repo / "a.txt").write_text("1\nzwei\n3\n", encoding="utf-8")
    (repo / "new.txt").write_text("n1\nn2\n", encoding="utf-8")
    (repo / "bin.dat").write_bytes(bytes(range(256)))
    # 세션이 쓴 .gitattributes로 외부 diff를 걸어도 실행되지 않는다 (--no-ext-diff --no-textconv)
    (repo / ".gitattributes").write_text("*.txt diff=evil\n", encoding="utf-8")
    files = {f["path"]: f for f in report.changed_files(repo, base)}
    assert (files["a.txt"]["added"], files["a.txt"]["deleted"]) == (2, 1)
    assert files["new.txt"]["added"] == 2 and files["new.txt"]["untracked"] is True
    assert files["bin.dat"]["binary"] is True
