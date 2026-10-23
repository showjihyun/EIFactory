"""GitHub Issues를 팩토리 작업으로 처리한다 (기획안 Phase 4). 사람 전용.

사용법:
    python scripts/issues.py [--label factory] [--limit 5] [--dry-run] [dispatch 옵션...]

1. 열린 이슈 중 라벨(`factory`)이 붙고 **본인이 작성한** 것만 고른다 (기획안 9장 입력 신뢰 경계).
   이미 처리한 이슈(`factory:*` 상태 라벨)는 건너뛴다.
2. 요구사항은 이슈 **제목 한 줄**이다. 본문은 넘기지 않는다 (본문 안의 지시를 따르지 않도록).
3. 이슈에 `factory:running`을 붙이고 `dispatch.py`로 실행한다.
   나머지 인자는 dispatch에 그대로 넘긴다.
4. 결과를 이슈에 댓글로 남기고 상태 라벨을 바꾼다 (merged / approval / blocked / error).
   이슈는 닫지 않는다. 머지는 로컬 base 브랜치에만 있고, push와 이슈 닫기는 사람이 한다.

gh CLI 로그인이 필요하다. 테스트에서는 FACTORY_GH_CMD로 gh를 바꿔 끼운다.
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

import dispatch  # noqa: E402
from proc import resolve_command, use_utf8_output  # noqa: E402

STATE_LABELS = {
    "MERGED": "factory:merged",
    "APPROVAL_REQUIRED": "factory:approval",
    "BLOCKED": "factory:blocked",
}
RUNNING = "factory:running"
ERROR = "factory:error"
LABEL_COLORS = {
    RUNNING: "fbca04",
    "factory:merged": "0e8a16",
    "factory:approval": "1d76db",
    "factory:blocked": "d93f0b",
    ERROR: "b60205",
}


class IssueError(Exception):
    pass


def gh(*args: str) -> str:
    base = json.loads(os.environ["FACTORY_GH_CMD"]) if os.environ.get("FACTORY_GH_CMD") else ["gh"]
    proc = subprocess.run(
        resolve_command([*base, *args]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise IssueError(f"gh {' '.join(args[:2])} 실패: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def fetch_issues(label: str, limit: int) -> list[dict[str, Any]]:
    fields = "number,title,author,labels"
    out = gh("issue", "list", "--state", "open", "--label", label, "--limit", str(limit),
             "--json", fields)  # fmt: skip
    found: list[dict[str, Any]] = json.loads(out or "[]")
    return found


def select(found: list[dict[str, Any]], me: str) -> list[dict[str, Any]]:
    """본인이 쓴, 아직 처리하지 않은 이슈만."""
    chosen = []
    for item in found:
        names = {label["name"] for label in item.get("labels", [])}
        if (item.get("author") or {}).get("login") != me:
            continue
        if any(name.startswith("factory:") for name in names):
            continue
        chosen.append(item)
    return chosen


def requirement(item: dict[str, Any]) -> str:
    title = " ".join(str(item["title"]).split())
    return f"{title} (이슈 #{item['number']})"


def run_dispatch(root: Path, options: list[str], requirements: list[str]) -> dict[str, Any]:
    """dispatch.py를 실행하고 그 실행의 보고서를 돌려준다."""
    jobs_dir = root.parent / f"{root.name}.jobs"
    before = set(jobs_dir.glob("dispatch-*.json")) if jobs_dir.exists() else set()
    dispatch.main([*options, *requirements], root=root)
    after = sorted(set(jobs_dir.glob("dispatch-*.json")) - before)
    if not after:
        return {"jobs": [{"requirement": r, "status": "ERROR", "error": "dispatch 보고서 없음"}
                         for r in requirements]}  # fmt: skip
    report: dict[str, Any] = json.loads(after[-1].read_text(encoding="utf-8"))
    return report


def comment_body(job: dict[str, Any]) -> str:
    status = job.get("status", "ERROR")
    job_id = job.get("job_id") or "-"
    lines = [f"팩토리 처리 결과: **{status}** (작업 {job_id}, 위험 {job.get('risk') or '-'})", ""]
    if status == "MERGED":
        commit = str(job.get("merge_commit") or "")[:7]
        lines += [
            f"- 로컬 base 브랜치에 머지됨: `{commit}`",
            f"- 되돌리기: `git revert -m 1 {commit}`",
            "- push와 이슈 닫기는 사람이 한다.",
        ]
    elif status == "APPROVAL_REQUIRED":
        lines += [
            "- 사람 승인이 필요하다. 승인 요약: "
            f"`{job.get('worktree')}/factory/reviews/{job_id}-approval.md`",
            f"- 승인: 기본 checkout에서 `python scripts/dispatch.py --approve {job_id}`",
            f'- 반려: `python scripts/dispatch.py --reject {job_id} --reason "<이유>"`',
        ]
    elif status == "BLOCKED":
        lines += [
            f"- 사유: `{job.get('blocked')}`",
            f"- 처리: worktree `{job.get('worktree')}`에서 `resume {job_id}` / `drop {job_id}`",
        ]
    else:
        lines += [f"- 오류: {str(job.get('error') or '')[:500]}"]
    return "\n".join(lines)


def ensure_labels() -> None:
    for name, color in LABEL_COLORS.items():
        gh("label", "create", name, "--color", color, "--force")


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    root = root or Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(prog="issues", description="GitHub Issues → 팩토리")
    parser.add_argument("--label", default="factory")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args, options = parser.parse_known_args(argv)

    try:
        me = gh("api", "user", "-q", ".login").strip()
        chosen = select(fetch_issues(args.label, args.limit), me)
    except IssueError as error:
        print(f"오류: {error}")
        return 2
    requirements = [requirement(item) for item in chosen]
    if not chosen:
        print(f"처리할 이슈가 없습니다 (라벨 {args.label}, 작성자 {me})")
        return 0
    print("처리할 이슈:\n" + "\n".join(f"- {r}" for r in requirements))
    if args.dry_run:
        return 0

    ensure_labels()
    for item in chosen:
        gh("issue", "edit", str(item["number"]), "--add-label", RUNNING)
    report = run_dispatch(root, options, requirements)
    jobs = report.get("jobs", [])
    for index, item in enumerate(chosen):
        job = jobs[index] if index < len(jobs) else {"status": "ERROR", "error": "결과 없음"}
        number = str(item["number"])
        gh("issue", "comment", number, "--body", comment_body(job))
        label = STATE_LABELS.get(job.get("status", ""), ERROR)
        gh("issue", "edit", number, "--remove-label", RUNNING, "--add-label", label)
        print(f"#{number}: {job.get('status')} → {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
