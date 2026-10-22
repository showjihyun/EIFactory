"""팩토리 운영 보고서 (기획안 3장 KPI, 10장 관측성, 11장 개선 루프).

사용법:
    python scripts/report.py [--jobs-dir ../<레포>.jobs] [--out 파일] [--branch main] [--no-git]

dispatch가 남긴 작업 기록(`<jobs>/records/*.json`)과 아직 남아 있는 worktree의 상태를 읽어
KPI(작업 성공률, 사람 개입률, 리드타임, 머지 후 결함, 비용)와 개선 후보(BLOCKED 사유, 리뷰 지적)를
마크다운 표로 낸다. 오판율은 정답표와 대조해야 해서 사람이 확인한다.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proc import use_utf8_output  # noqa: E402

REACHED = {"APPROVAL_REQUIRED", "APPROVED", "MERGED"}
REVERT = re.compile(r"This reverts commit ([0-9a-f]{40})")


def _tokens(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    return sum(int(usage.get(k) or 0) for k in keys)


def session_usage(log: Path) -> dict[str, Any]:
    """세션 로그의 비용·토큰.

    Claude(stream-json)는 result 이벤트, Codex(--json)는 turn.completed 이벤트의 합이다.
    """
    if not log.exists():
        return {"cost_usd": None, "tokens": None}
    cost: float | None = None
    tokens = 0
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        usage = event.get("usage") or {}
        if event.get("type") == "result":
            cost = event.get("total_cost_usd")
            tokens = _tokens(
                usage,
                ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                 "output_tokens"),
            )  # fmt: skip
        elif event.get("type") == "turn.completed":
            tokens += _tokens(usage, ("input_tokens", "cached_input_tokens", "output_tokens"))
    return {"cost_usd": cost, "tokens": tokens}


def _lead_minutes(job: dict[str, Any]) -> float | None:
    """요구사항 입력 → 승인 대기(또는 자동 승인) 도달 시간 (KPI 리드타임)."""
    for entry in job.get("history", []):
        if entry["status"] in {"APPROVAL_REQUIRED", "APPROVED"}:
            return round((entry["at"] - job["created_at"]) / 60, 1)
    return None


def metrics(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(jobs)
    reached = [j for j in jobs if any(h["status"] in REACHED for h in j.get("history", []))]
    intervened = [
        j
        for j in jobs
        if any(h["status"] == "BLOCKED" for h in j.get("history", []))
        or (j.get("approval") or {}).get("decision") == "rejected"
    ]
    leads = [m for m in (_lead_minutes(j) for j in jobs) if m is not None]
    issues: Counter[str] = Counter(
        f"[{r.get('reviewer', 'validator')}] {issue}"
        for j in jobs
        for r in j.get("reviews", [])
        if r.get("verdict") == "FAIL"
        for issue in r.get("issues", [])
    )
    review_cost = sum(float(r.get("cost_usd") or 0) for j in jobs for r in j.get("reviews", []))
    session = [j.get("session_usage") or {} for j in jobs]
    return {
        "total": total,
        "by_status": dict(Counter(j["status"] for j in jobs)),
        "success_rate": len(reached) / total if total else 0.0,
        "intervention_rate": len(intervened) / total if total else 0.0,
        "lead_time_min": (
            {"min": min(leads), "median": statistics.median(leads), "max": max(leads)}
            if leads
            else None
        ),
        "active_min": [round(j["time"]["active_s"] / 60, 1) for j in jobs],
        "blocked_reasons": dict(Counter(b["reason"] for j in jobs for b in j.get("blocks", []))),
        "promotions": sum(len(j.get("model_changes", [])) for j in jobs),
        "review_issues": issues.most_common(10),
        "review_cost_usd": round(review_cost, 4),
        "session_cost_usd": round(sum(float(s.get("cost_usd") or 0) for s in session), 4),
        "session_tokens": sum(int(s.get("tokens") or 0) for s in session),
        "merges": [j["merge_commit"] for j in jobs if j.get("merge_commit")],
    }


def reverted(repo: Path, branch: str, merges: list[str]) -> set[str]:
    """base 브랜치에서 되돌려진 머지 커밋 (KPI 머지 후 결함)."""
    log = subprocess.run(
        ["git", "log", branch, "--format=%B"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout
    full = {}
    for sha in merges:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            full[proc.stdout.strip()] = sha
    return {full[s] for s in REVERT.findall(log) if s in full}


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def render(m: dict[str, Any], defects: int | None, merged: int) -> str:
    lead = m["lead_time_min"]
    lead_text = f"{lead['min']} / {lead['median']} / {lead['max']}분" if lead else "-"
    defect_text = (
        "미확인 (--no-git)"
        if defects is None
        else (f"{_pct(defects / merged)} ({defects}/{merged})" if merged else "- (머지 없음)")
    )
    status = ", ".join(f"{k} {v}" for k, v in sorted(m["by_status"].items()))
    lines = [
        "# 팩토리 운영 보고서",
        "",
        f"- 작업 {m['total']}건: {status}",
        f"- Builder 모델 승격 {m['promotions']}회",
        "",
        "## KPI (기획안 3장)",
        "",
        "| 지표 | 값 | 목표 |",
        "|---|---|---|",
        "| 오판율 | 사람이 확인 (평가 과제는 정답표와 대조) | 0% |",
        f"| 작업 성공률 | {_pct(m['success_rate'])} | 70% 이상 |",
        f"| 사람 개입률 | {_pct(m['intervention_rate'])} | 30% 이하 |",
        f"| 리드타임 (최소 / 중앙 / 최대) | {lead_text} | 소형 10분 이내 |",
        f"| 머지 후 결함 | {defect_text} | 10% 이하 |",
        f"| 비용 (세션 + 리뷰어, API 환산) | ${m['session_cost_usd'] + m['review_cost_usd']:.2f} "
        f"(세션 토큰 {m['session_tokens']:,}) | 측정 후 상한 설정 |",
        "",
        "## 개선 후보 (기획안 11장, 사람이 검토해 반영)",
        "",
        "| BLOCKED 사유 | 건수 |",
        "|---|---|",
        *(
            [f"| {k} | {v} |" for k, v in sorted(m["blocked_reasons"].items(), key=lambda x: -x[1])]
            or ["| 없음 | - |"]
        ),
        "",
        "| 리뷰 지적 (FAIL) | 건수 |",
        "|---|---|",
        *([f"| {issue} | {count} |" for issue, count in m["review_issues"]] or ["| 없음 | - |"]),
        "",
    ]
    return "\n".join(lines)


def load_jobs(jobs_dir: Path) -> list[dict[str, Any]]:
    """기록(머지 후 지운 worktree 포함)과 아직 남은 worktree의 상태. 겹치면 worktree 쪽이 최신."""
    jobs: dict[str, dict[str, Any]] = {}
    for path in sorted((jobs_dir / "records").glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        jobs[item["id"]] = item
    for state_path in sorted(jobs_dir.glob("*/factory/state.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        jobs.update(state.get("jobs", {}))
    for job_id, item in jobs.items():
        item["session_usage"] = session_usage(jobs_dir / "logs" / f"{job_id}.jsonl")
    return list(jobs.values())


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    root = root or Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(prog="report", description="팩토리 운영 보고서")
    parser.add_argument("--jobs-dir", type=Path, default=root.parent / f"{root.name}.jobs")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--branch", default="HEAD")
    parser.add_argument("--no-git", action="store_true", help="머지 후 결함을 세지 않음")
    args = parser.parse_args(argv)

    m = metrics(load_jobs(args.jobs_dir))
    defects = None if args.no_git else len(reverted(root, args.branch, m["merges"]))
    text = render(m, defects, len(m["merges"]))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
