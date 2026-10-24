"""브리핑: 사람에게 오는 단일 목록 "나를 기다리는 일" (기획안 6.9 P2).

사용법:
    python scripts/brief.py [--jobs-dir ../<레포>.jobs] [--json | --counts]

모델이 요약하지 않는다. 대시보드(`monitor.py`)와 같은 로더로 작업을 찾고, 다음 명령도
대시보드와 같은 규칙으로 만든다. 승인 대기의 "판단할 것"은 승인 요약과 같은 함수다.

순서: 승인 대기(HIGH 먼저) → BLOCKED(규칙 기반 추천 처리) → push 대기 → 경고.
작업 상태 파일은 세션이 쓴 값이라 "세션 값"으로 표시한다 (기획안 9장).
`--counts`는 상태줄용 한 줄 (`승인 1 · BLOCKED 1 · push 1`, 없으면 빈 줄).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory import judgement_lines  # noqa: E402
from monitor import Collector  # noqa: E402
from proc import use_utf8_output  # noqa: E402

KINDS = ("approval", "blocked", "push", "warning")
COUNT_LABELS = {"approval": "승인", "blocked": "BLOCKED", "push": "push", "warning": "경고"}
RISK_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
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


def collect(root: Path, jobs_dir: Path, collector: Collector | None = None) -> dict[str, Any]:
    """목록과 종류별 건수. 대시보드는 자기 로더(collector)를 넘겨 파일을 다시 읽지 않는다."""
    collector = collector or Collector(root, jobs_dir)
    refs = collector.refs()
    with_job = [r for r in refs if r.get("job")]

    def at(ref: dict[str, Any]) -> float:
        history = ref["job"].get("history") or []
        return float(history[-1].get("at") or 0) if history else 0.0

    approvals = sorted(
        (r for r in with_job if r["job"].get("status") == "APPROVAL_REQUIRED"),
        key=lambda r: (RISK_ORDER.get(str(r["job"].get("risk")), 3), at(r)),
    )
    blocked = sorted((r for r in with_job if r["job"].get("status") == "BLOCKED"), key=at)
    items = [_approval(collector, r) for r in approvals]
    items += [_blocked(collector, r) for r in blocked]
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
    counts = {kind: sum(1 for i in items if i["kind"] == kind) for kind in KINDS}
    return {"items": items, "counts": counts}


def counts_line(counts: dict[str, int]) -> str:
    return " · ".join(f"{COUNT_LABELS[k]} {counts[k]}" for k in KINDS if counts.get(k))


def render(data: dict[str, Any]) -> str:
    if not data["items"]:
        return "나를 기다리는 일이 없습니다."
    out = [f"# 나를 기다리는 일 ({counts_line(data['counts'])})", ""]
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
