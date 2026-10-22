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
