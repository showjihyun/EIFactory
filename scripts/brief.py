"""브리핑: 사람에게 오는 단일 목록 "나를 기다리는 일" (기획안 6.9 P2).

사용법:
    python scripts/brief.py [--jobs-dir ../<레포>.jobs] [--json | --counts]

모델이 요약하지 않는다. 대시보드(`monitor.py`)와 같은 로더로 작업을 찾고, 다음 명령도
대시보드와 같은 규칙으로 만든다. 승인 대기의 "판단할 것"은 승인 요약과 같은 함수다.

순서: 승인 대기(HIGH 먼저) → BLOCKED(규칙 기반 추천 처리) → 계획 대기 → 사람 직접 항목
→ push 대기 → 경고 → 읽어 볼 작업 → 정리 후보.
작업 상태 파일은 세션이 쓴 값이라 "세션 값"으로 표시한다 (기획안 9장).
`--counts`는 상태줄용 한 줄 (`승인 1 · BLOCKED 1 · push 1`, 없으면 빈 줄). 읽어 볼 작업과
정리 후보는 급하지 않아 상태줄에 넣지 않는다.

읽어 볼 작업: 최근 7일 안에 자동 머지된 LOW 작업 중 작업 id의 해시가 작은 순으로 2건.
같은 기록이면 같은 표본이고, 기간 안에 남은 표본은 다음 날에도 표본이다 (기획안 1.2의 5·8).
정리 후보: 7일보다 오래된 DROPPED·BLOCKED 작업 중 worktree나 브랜치가 남은 것. 삭제는 사람이 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory import judgement_lines, screenshot_paths  # noqa: E402
from factory_config import ConfigError, load_config  # noqa: E402
from monitor import CLOSED, Collector  # noqa: E402
from proc import use_utf8_output  # noqa: E402

KINDS = ("approval", "blocked", "plan", "human", "push", "warning", "review", "cleanup")
STATUS_KINDS = ("approval", "blocked", "plan", "human", "push", "warning")
COUNT_LABELS = {
    "approval": "승인", "blocked": "BLOCKED", "plan": "계획", "human": "사람 직접",
    "push": "push", "warning": "경고", "review": "읽어 볼", "cleanup": "정리",
}  # fmt: skip
RISK_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
DAY = 86_400.0
SAMPLE_DAYS = 7  # 읽어 볼 작업을 고르는 기간
SAMPLES = 2  # 그 기간에 고르는 건수
CLEANUP_DAYS = 7  # 이보다 오래된 DROPPED·BLOCKED 작업이 정리 후보
# BLOCKED 사유별 추천 처리 (사람이 고른다. 브리핑은 제안만 한다)
RECOMMEND = {
    "TIMEOUT": "원인을 보고 resume --extend (상한의 50% 연장, 한 번만) 또는 split",
    "TIMEOUT_EXTENDED": "연장을 이미 썼다: 요구사항을 split 하거나 drop",
    "BUDGET": "비용 상한과 진행 상황을 보고 resume 또는 drop",
    "RETRY_LIMIT": "실패 원인을 보고 요구사항을 나눠 다시 하거나 drop",
    "REPEATED_FAILURE": "같은 실패가 반복됐다: 원인을 보고 drop 후 요구사항을 고쳐 다시",
    "SPEC_INVALID": "수락 테스트를 쓸 수 없는 요구사항: drop 후 요구사항을 고쳐 다시",
    "CONFLICT": "충돌은 사람이 직접 해결한 뒤 resume",
    "MERGE_CHECK_FAILED": "머지 전 재검증 실패: worktree에서 직접 확인",
    "NEEDS_DECISION": "사람의 결정이 필요한 요구사항: 결정한 뒤 resume 또는 drop",
    "SESSION_ENDED": "세션이 작업 도중 끝났다: resume으로 이어서",
}


def _git(root: Path, *args: str) -> str | None:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def push_waiting(root: Path) -> int:
    """기본 checkout의 브랜치가 원격 추적 브랜치보다 앞선 커밋 수. 추적 브랜치가 없으면 0."""
    ahead = _git(root, "rev-list", "--count", "@{upstream}..HEAD")
    return int(ahead) if ahead and ahead.isdigit() else 0


def _screenshots(root: Path, ref: dict[str, Any]) -> list[str]:
    worktree = ref.get("worktree")
    if not worktree:
        return []
    try:
        patterns = load_config(root).screenshots
    except ConfigError:
        return []
    return [f"스크린샷: {Path(worktree) / s}" for s in screenshot_paths(Path(worktree), patterns)]


def _approval(collector: Collector, ref: dict[str, Any]) -> dict[str, Any]:
    job = ref["job"]
    return {
        "kind": "approval",
        "id": ref["id"],
        "key": ref["key"],
        "title": f"{ref['id']} 승인 대기 (위험 {job.get('risk')})",
        "requirement": job.get("requirement") or "",
        "lines": [
            *(f"위험 근거: {r}" for r in job.get("risk_reasons") or []),
            *(line.removeprefix("- ") for line in judgement_lines(job)),
            *_screenshots(collector.root, ref),
        ],  # fmt: skip
        "commands": collector.next_commands(ref),
        "trust": "session",
    }


def _blocked(collector: Collector, ref: dict[str, Any]) -> dict[str, Any]:
    job = ref["job"]
    blocked = job.get("blocked") or {}
    reason = str(blocked.get("reason") or "UNKNOWN")
    extended = bool((job.get("time") or {}).get("extended"))
    key = "TIMEOUT_EXTENDED" if reason == "TIMEOUT" and extended else reason
    commands = collector.next_commands(ref)
    if key == "TIMEOUT_EXTENDED":
        commands = [c for c in commands if "--extend" not in c]
    return {
        "kind": "blocked",
        "id": ref["id"],
        "key": ref["key"],
        "title": f"{ref['id']} BLOCKED ({reason})",
        "requirement": job.get("requirement") or "",
        "lines": [str(blocked.get("detail") or "진단 없음")],
        "recommendation": RECOMMEND.get(key, "진단을 보고 resume 또는 drop"),
        "commands": commands,
        "trust": "session",
    }


def _at(ref: dict[str, Any]) -> float:
    history = ref["job"].get("history") or []
    return float(history[-1].get("at") or 0) if history else 0.0


def _plans(jobs_dir: Path) -> list[dict[str, Any]]:
    plans = []
    for path in sorted((jobs_dir / "plans").glob("P*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(state, dict) and state.get("id") and isinstance(state.get("jobs"), list):
            plans.append(state)
    return plans


def _plan_items(plans: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """계획 대기(CHECKED, PLAN_WAITING 작업이 있는 계획)와 끝나지 않은 계획의 사람 직접 항목."""
    waiting, human = [], []
    for state in plans:
        plan_id = state["id"]
        command = f"python scripts/dispatch.py --run-plan {plan_id}"
        held = state.get("waiting") or {}
        if state.get("status") == "CHECKED":
            waiting.append({"kind": "plan", "id": plan_id, "lines": [], "commands": [command],
                            "title": f"{plan_id} 계획 검사 통과, 실행 전: {state.get('goal', '')}",
                            "trust": "host"})  # fmt: skip
        elif state.get("status") == "RUNNING" and held:
            waiting.append({"kind": "plan", "id": plan_id, "commands": [command],
                            "title": f"{plan_id} 계획 진행 중: {state.get('goal', '')}",
                            "lines": [f"PLAN_WAITING {name}: {why}" for name, why in held.items()],
                            "trust": "host"})  # fmt: skip
        if state.get("status") == "DONE":
            continue
        for item in state["jobs"]:
            if item.get("route") not in {"human", "needs_decision"}:
                continue
            what = "결정 필요" if item["route"] == "needs_decision" else "사람 직접 항목"
            human.append({"kind": "human", "id": f"{plan_id}:{item['id']}",
                          "title": f"{plan_id}:{item['id']} {what}",
                          "requirement": item.get("requirement") or "",
                          "lines": [str(item.get("route_reason") or "")], "commands": [],
                          "trust": "host"})  # fmt: skip
    return waiting, human


def _auto_merged_low(job: dict[str, Any]) -> bool:
    statuses = {h.get("status") for h in job.get("history") or []}
    return (
        job.get("status") == "MERGED"
        and job.get("risk") == "LOW"
        and bool(job.get("merge_commit"))
        and "APPROVAL_REQUIRED" not in statuses
    )


def _read_samples(refs: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """최근 자동 머지된 LOW 작업 중 고정 규칙으로 고른 표본 (기획안 1.2의 5·8)."""
    recent = {
        r["id"]: r for r in refs
        if _auto_merged_low(r["job"]) and now - SAMPLE_DAYS * DAY <= _at(r) <= now
    }  # fmt: skip
    picked = sorted(recent, key=lambda i: hashlib.sha256(i.encode()).hexdigest())[:SAMPLES]
    items = []
    for job_id in sorted(picked):
        job = recent[job_id]["job"]
        items.append({"kind": "review", "id": job_id,
                      "title": f"{job_id} 읽어 볼 작업 (자동 머지 LOW 표본)",
                      "requirement": job.get("requirement") or "",
                      "lines": ["diff를 읽고 결함이 있으면 기록한다 (머지 후 결함 KPI)"],
                      "commands": [f"git show --first-parent {job['merge_commit']}"],
                      "trust": "session"})  # fmt: skip
    return items


def _cleanup(
    root: Path, jobs_dir: Path, refs: list[dict[str, Any]], now: float
) -> list[dict[str, Any]]:
    """오래된 DROPPED·BLOCKED 작업 중 worktree나 브랜치가 남은 것. 삭제는 사람이 한다."""
    items = []
    seen = set()
    for ref in sorted(refs, key=lambda r: r["id"]):
        job, job_id = ref["job"], ref["id"]
        if ref["source"] == "interactive" or job_id in seen:
            continue
        if job.get("status") not in {"DROPPED", "BLOCKED"} or now - _at(ref) < CLEANUP_DAYS * DAY:
            continue
        seen.add(job_id)
        commands = []
        worktree = jobs_dir / job_id
        if worktree.is_dir():
            commands.append(f"git worktree remove {worktree}")
        branch = str(job.get("branch") or f"factory/{job_id}")
        if _git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}") is not None:
            commands.append(f"git branch -D {branch}")
        if not commands:
            continue
        days = int((now - _at(ref)) // DAY)
        lines = [f"{job['status']} 뒤 {days}일 지남"]
        if job["status"] == "BLOCKED":
            lines.append("아직 BLOCKED: 지우기 전에 drop할지 먼저 정한다")
        items.append({"kind": "cleanup", "id": job_id, "title": f"{job_id} 정리 후보",
                      "requirement": job.get("requirement") or "", "lines": lines,
                      "commands": commands, "trust": "host"})  # fmt: skip
    return items


def collect(
    root: Path, jobs_dir: Path, collector: Collector | None = None, now: float | None = None
) -> dict[str, Any]:
    """목록과 종류별 건수. 대시보드는 자기 로더(collector)를 넘겨 파일을 다시 읽지 않는다."""
    collector = collector or Collector(root, jobs_dir)
    now = time.time() if now is None else now
    refs = collector.refs()
    with_job = [r for r in refs if r.get("job")]
    at = _at

    approvals = sorted(
        (r for r in with_job if r["job"].get("status") == "APPROVAL_REQUIRED"),
        key=lambda r: (RISK_ORDER.get(str(r["job"].get("risk")), 3), at(r)),
    )
    blocked = sorted((r for r in with_job if r["job"].get("status") == "BLOCKED"), key=at)
    items = [_approval(collector, r) for r in approvals]
    items += [_blocked(collector, r) for r in blocked]
    plan_waiting, human = _plan_items(_plans(jobs_dir))
    items += plan_waiting + human
    ahead = push_waiting(root)
    if ahead:
        branch = _git(root, "branch", "--show-current") or "HEAD"
        items.append(
            {
                "kind": "push",
                "id": branch,
                "title": f"push 대기: {branch}가 원격보다 커밋 {ahead}개 앞섬",
                "lines": [],
                "commands": [f"git push origin {branch}"],
                "trust": "host",
            }
        )
    for ref in refs:
        if ref.get("source") == "dispatch":
            item = ref.get("dispatch_item") or {}
            items.append(
                {
                    "kind": "warning",
                    "id": ref["id"],
                    "title": f"dispatch {item.get('status')}: {item.get('requirement') or ''}",
                    "lines": [str(item.get("error") or "")],
                    "commands": [],
                    "trust": "host",
                }
            )
    for ref in with_job:
        job = ref["job"]
        expected, actual = job.get("expected_risk"), job.get("risk")
        if expected and actual and expected != actual and job.get("status") not in CLOSED:
            items.append({"kind": "warning", "id": ref["id"],
                          "title": f"{ref['id']} 예상 등급 {expected} → 실제 {actual}",
                          "lines": [f"계획 {job.get('plan_id')}:{job.get('plan_job')}"],
                          "commands": [], "trust": "session"})  # fmt: skip
    items += _read_samples(with_job, now)
    items += _cleanup(root, jobs_dir, with_job, now)
    counts = {kind: sum(1 for i in items if i["kind"] == kind) for kind in KINDS}
    return {"items": items, "counts": counts}


def counts_line(counts: dict[str, int]) -> str:
    return " · ".join(f"{COUNT_LABELS[k]} {counts[k]}" for k in STATUS_KINDS if counts.get(k))


def render(data: dict[str, Any]) -> str:
    if not data["items"]:
        return "나를 기다리는 일이 없습니다."
    head = " · ".join(f"{COUNT_LABELS[k]} {n}" for k, n in data["counts"].items() if n)
    out = [f"# 나를 기다리는 일 ({head})", ""]
    for n, item in enumerate(data["items"], 1):
        trust = " [세션 값]" if item["trust"] == "session" else ""
        out.append(f"{n}. {item['title']}{trust}")
        if item.get("requirement"):
            out.append(f"   요구사항: {item['requirement']}")
        out += [f"   - {line}" for line in item["lines"] if line]
        if item.get("recommendation"):
            out.append(f"   추천: {item['recommendation']}")
        out += [f"   $ {command}" for command in item["commands"]]
        out.append("")
    return "\n".join(out).rstrip()


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    root = root or Path(os.environ.get("FACTORY_REPO") or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(prog="brief", description="나를 기다리는 일")
    parser.add_argument("--jobs-dir", type=Path, default=root.parent / f"{root.name}.jobs")
    form = parser.add_mutually_exclusive_group()
    form.add_argument("--json", action="store_true", help="JSON으로 출력")
    form.add_argument("--counts", action="store_true", help="상태줄용 한 줄")
    args = parser.parse_args(argv)
    data = collect(root, args.jobs_dir)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.counts:
        print(counts_line(data["counts"]))
    else:
        print(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
