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


# ---------------------------------------------------------------- 실시간 사용량 (대시보드)

STREAM_KEYS = {
    "in": "input_tokens",
    "out": "output_tokens",
    "cr": "cache_read_input_tokens",
    "cw": "cache_creation_input_tokens",
}
RESULT_KEYS = {
    "in": "inputTokens",
    "out": "outputTokens",
    "cr": "cacheReadInputTokens",
    "cw": "cacheCreationInputTokens",
}


def _events(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    events = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _role(model: str | None) -> dict[str, Any]:
    return {"model": model, "in": 0, "out": 0, "cr": 0, "cw": 0, "usd": None, "unattributed": 0}


def _tokens_of(role: dict[str, Any]) -> int:
    return sum(int(role[k] or 0) for k in ("in", "out", "cr", "cw")) + int(role["unattributed"])


def _price(role: dict[str, Any], prices: dict[str, float]) -> None:
    unit = prices.get(role["model"] or "")
    role["usd"] = round(_tokens_of(role) * unit, 6) if unit is not None else None


def live_usage(
    log: Path, prices: dict[str, float], builder_model: str | None = None
) -> dict[str, Any]:
    """세션 로그의 역할별 토큰(입력, 출력, 캐시 읽기, 캐시 쓰기)과 비용 (설계안 10.1, 10.4).

    - 결과 이벤트가 있으면 modelUsage로 확정 (basis "result").
    - 진행 중이면 응답(message.id)마다 한 번만 센 스트림 값 × 보정 단가 (basis "stream", 추정).
      stream-json은 한 응답을 여러 이벤트로 보내며 사용량을 반복하므로 단순히 더하면 약 2배가 된다.
    - Codex는 turn.completed의 합 (basis "turns", 캐시 쓰기는 제공 안 됨).
    builder_model: 결과 이벤트에서 Builder 몫을 고를 모델 ID.
    """
    events = _events(log)
    out: dict[str, Any] = {"basis": None, "estimated": True, "roles": {}, "subscription": None}
    responses: dict[str, tuple[str, str | None, dict[str, Any]]] = {}
    progress: dict[str, int] = {}
    turns = _role(None)
    has_turns = False
    result = None
    for event in events:
        kind = event.get("type")
        if kind == "assistant":
            msg = event.get("message") or {}
            role = "builder" if event.get("parent_tool_use_id") else "orchestrator"
            responses[str(msg.get("id"))] = (role, msg.get("model"), msg.get("usage") or {})
        elif kind == "system" and event.get("subtype") == "task_progress":
            if event.get("subagent_type") == "builder":
                total = int((event.get("usage") or {}).get("total_tokens") or 0)
                tid = str(event.get("task_id"))
                progress[tid] = max(progress.get(tid, 0), total)
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            has_turns = True
            turns["in"] += int(usage.get("input_tokens") or 0)
            turns["cr"] += int(usage.get("cached_input_tokens") or 0)
            turns["out"] += int(usage.get("output_tokens") or 0)
        elif kind == "rate_limit_event":
            windows = (event.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            five, seven = windows.get("five_hour") or {}, windows.get("seven_day") or {}
            out["subscription"] = {
                "five_hour": five.get("utilization"),
                "seven_day": seven.get("utilization"),
                "five_hour_resets_at": five.get("resetsAt"),
            }
        elif kind == "result":
            result = event

    roles: dict[str, dict[str, Any]] = {}
    for role_name, model, usage in responses.values():
        role = roles.setdefault(role_name, _role(model))
        role["model"] = role["model"] or model
        for key, field in STREAM_KEYS.items():
            role[key] += int(usage.get(field) or 0)
    if progress and "builder" in roles:
        gap = sum(progress.values()) - _tokens_of(roles["builder"])
        roles["builder"]["unattributed"] = max(0, gap)

    if result and result.get("modelUsage"):
        roles = {}
        for model, mu in result["modelUsage"].items():
            name = "builder" if builder_model and model == builder_model else "orchestrator"
            if name in roles:  # 같은 모델이 두 역할에 쓰였으면 합친다 (나눌 근거가 없음)
                name = "orchestrator"
            role = roles.setdefault(name, _role(model))
            for key, field in RESULT_KEYS.items():
                role[key] += int(mu.get(field) or 0)
            role["usd"] = round((role["usd"] or 0) + float(mu.get("costUSD") or 0), 6)
        out.update(basis="result", estimated=False)
        total_usd = result.get("total_cost_usd")
    elif result and result.get("total_cost_usd") is not None:
        out.update(basis="result", estimated=False)  # 모델별 분해가 없으면 역할은 스트림 값
        total_usd = float(result["total_cost_usd"])
    elif has_turns:
        roles = {"orchestrator": {**turns, "cw": None}}
        unit = prices.get("codex")
        roles["orchestrator"]["usd"] = (
            None if unit is None else round((turns["in"] + turns["out"] + turns["cr"]) * unit, 6)
        )
        out["basis"] = "turns"
        total_usd = roles["orchestrator"]["usd"]
    elif roles:
        for role in roles.values():
            _price(role, prices)
        out["basis"] = "stream"
        known = [r["usd"] for r in roles.values() if r["usd"] is not None]
        total_usd = round(sum(known), 6) if known else None
    else:
        total_usd = None

    out["roles"] = roles
    total = {key: sum(int(r.get(key) or 0) for r in roles.values()) for key in STREAM_KEYS}
    out["total"] = {**total, "usd": total_usd}
    return out


def unit_prices(logs: list[Path], recent: int = 20) -> dict[str, float]:
    """모델별 보정 단가 = 확정 세션(결과 이벤트)의 Σ비용 / Σ토큰, 최근 recent개 (설계안 10.1).

    손으로 관리하는 가격표를 두지 않는다. 모델이 바뀌어도 첫 세션이 끝나면 단가가 생긴다.
    """
    usd: Counter[str] = Counter()
    tokens: Counter[str] = Counter()
    used = 0
    for log in sorted(logs, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        result = next((e for e in _events(log) if e.get("type") == "result"), None)
        if not result or not result.get("modelUsage"):
            continue
        for model, mu in result["modelUsage"].items():
            usd[model] += float(mu.get("costUSD") or 0)
            tokens[model] += sum(int(mu.get(f) or 0) for f in RESULT_KEYS.values())
        used += 1
        if used >= recent:
            break
    return {m: usd[m] / tokens[m] for m in tokens if tokens[m]}


def job_cost(job: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
    """작업 비용 = 세션 + 리뷰어 (설계안 4장 P1). 리뷰어는 별도 프로세스라 세션 비용에 없다."""
    reviews = job.get("reviews") or []
    missing = [str(r.get("reviewer")) for r in reviews if r.get("cost_usd") is None]
    reviewers = round(sum(float(r.get("cost_usd") or 0) for r in reviews), 6)
    session = (live or {}).get("total", {}).get("usd") if live else None
    estimated = live is None or bool(live.get("estimated")) or session is None or bool(missing)
    return {
        "session_usd": session,
        "reviewers_usd": reviewers,
        "total_usd": round((session or 0) + reviewers, 6),
        "estimated": estimated,
        "missing": missing,
    }


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


# ---------------------------------------------------------------- Git 확인 (대시보드)


def _git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def merged_into(repo: Path, commit: str, branch: str) -> bool:
    """commit이 branch에 들어 있는가 (대시보드의 "Git 확인" 값)."""
    if _git_run(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}").returncode:
        return False
    return _git_run(repo, "merge-base", "--is-ancestor", commit, branch).returncode == 0


def changed_files(worktree: Path, fork_point: str) -> list[dict[str, Any]]:
    """기준점 대비 바뀐 파일과 줄 수 (추적 안 되는 새 파일 포함).

    worktree 안 .gitattributes는 세션이 쓸 수 있으므로 외부 diff·textconv를 끈다
    (--no-ext-diff --no-textconv). clean 필터는 Git 설정(호스트의 공통 .git)에 정의가 있어야
    실행되고, 세션은 그 설정을 바꿀 수 없다 (컨테이너는 읽기 전용 마운트).
    """
    diff = _git_run(worktree, "diff", "--numstat", "--no-ext-diff", "--no-textconv",
                    "--no-renames", fork_point)  # fmt: skip
    files: list[dict[str, Any]] = []
    for line in diff.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        binary = added == "-"
        files.append({"path": path, "added": 0 if binary else int(added),
                      "deleted": 0 if binary else int(deleted), "binary": binary,
                      "untracked": False})  # fmt: skip
    others = _git_run(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    for path in filter(None, others.stdout.split("\0")):
        try:
            data = (worktree / path).read_bytes()
        except OSError:
            continue
        binary = b"\0" in data[:8192]
        added = 0 if binary else len(data.decode("utf-8", errors="replace").splitlines())
        files.append({"path": path, "added": added, "deleted": 0, "binary": binary,
                      "untracked": True})  # fmt: skip
    return files


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
