"""여러 요구사항을 worktree별 headless 세션으로 동시에 처리한다 (기획안 Phase 3).

사용법:
    python scripts/dispatch.py [--max-parallel 3] [--wall-timeout 40] [--model M]
                               [--require-api-key] [--keep-merged] "<요구사항>" ...

요구사항마다:
1. 기본 checkout의 base 브랜치 끝에서 worktree를 만들고 의존성을 설치한다.
2. 그 worktree에서 `factory.py new --base <base>`로 작업을 만든다 (상태는 worktree마다 따로).
3. headless `claude -p`로 factory 스킬을 실행한다. 실행 래퍼가 벽시계 시간 상한을 건다 (7.6).
4. 끝나면 상태를 읽는다. MERGED면 worktree를 지우고, 그 밖의 상태는 사람이 보도록 남긴다.

머지는 각 세션의 `factory.py merge`가 merge lock을 잡고 한 번에 하나씩 기본 checkout에 한다.
인증은 ANTHROPIC_API_KEY 환경변수가 있으면 API 키, 없으면 로그인 세션(구독)을 쓴다. 키 값은
어디에도 출력하거나 기록하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proc import resolve_command, use_utf8_output  # noqa: E402

BASE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Skill", "Agent", "Task"]
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_WALL_TIMEOUT_MIN = 40.0  # 중형 상한 30분 + 정리 여유
SETUP_STEPS = [
    ("backend", ["uv", "sync", "-q"]),
    ("frontend", ["npm", "ci", "--no-audit", "--no-fund", "--loglevel=error"]),
]
ACTIVE = {"QUEUED", "SPEC_READY", "BUILDING", "RETRY_REQUIRED", "VERIFYING", "APPROVED"}

PROMPT = (
    "factory 스킬로 배정된 작업 {job_id}를 처리해. 요구사항: {requirement}\n"
    "작업은 dispatcher가 이미 `new`로 만들어 QUEUED 상태다. recover 없이 "
    "factory/PROCESS.md 3번(spec·수락 테스트 작성)부터 진행해."
)

_git_lock = threading.Lock()  # worktree add/move/remove는 Git 공통 디렉터리를 바꾸므로 한 번에 하나


class DispatchError(Exception):
    pass


def _run(cmd: list[str], cwd: Path, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        resolve_command(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **kwargs,
    )


def _git(root: Path, *args: str) -> str:
    proc = _run(["git", *args], root)
    if proc.returncode != 0:
        raise DispatchError(f"git {' '.join(args)} 실패: {proc.stderr.strip()}")
    return proc.stdout.strip()


def auth_mode() -> str:
    return "API 키 (환경변수)" if os.environ.get("ANTHROPIC_API_KEY") else "로그인 세션 (구독)"


def allowed_tools(root: Path) -> list[str]:
    """프로젝트 허용 목록을 그대로 넘긴다.

    새 worktree는 신뢰 승인 전이라 settings.json의 allow가 무시되기 때문이다 (hook은 동작).
    """
    try:
        settings = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    allow = [str(t) for t in settings.get("permissions", {}).get("allow", [])]
    return BASE_TOOLS + allow


def agent_command(root: Path, prompt: str, model: str) -> list[str]:
    override = os.environ.get("FACTORY_AGENT_CMD")
    if override:
        return [*json.loads(override), prompt]
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        *allowed_tools(root),
    ]


def setup_steps() -> list[tuple[str, list[str]]]:
    override = os.environ.get("FACTORY_SETUP_CMD")
    if override is not None:
        return [(str(area), list(cmd)) for area, cmd in json.loads(override)]
    return SETUP_STEPS


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _load_job(worktree: Path, job_id: str) -> dict[str, Any]:
    state = json.loads((worktree / "factory/state.json").read_text(encoding="utf-8"))
    job: dict[str, Any] = state["jobs"][job_id]
    return job


def run_job(
    root: Path, base: str, requirement: str, index: int, args: argparse.Namespace, stamp: str
) -> dict[str, Any]:
    jobs_dir = root.parent / f"{root.name}.jobs"
    logs = jobs_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    slot = jobs_dir / f"slot-{stamp}-{index}"
    result: dict[str, Any] = {"requirement": requirement, "job_id": None, "status": "SETUP_FAILED"}
    started = time.time()
    with _git_lock:
        _git(root, "worktree", "add", "-q", "--detach", str(slot), base)
    created = _run([sys.executable, "scripts/factory.py", "new", requirement, "--base", base], slot)
    if created.returncode != 0:
        result["error"] = f"new 실패: {created.stdout.strip()[-500:]}"
        result["worktree"] = str(slot)
        return result
    state = json.loads((slot / "factory/state.json").read_text(encoding="utf-8"))
    job_id = str(state["active"])
    worktree = jobs_dir / job_id
    with _git_lock:
        _git(root, "worktree", "move", str(slot), str(worktree))
    result.update(job_id=job_id, worktree=str(worktree))
    # 가상환경은 절대 경로를 품으므로 worktree를 옮긴 뒤에 설치한다
    for area, cmd in setup_steps():
        done = _run(cmd, worktree / area)
        if done.returncode != 0:
            result["error"] = f"{area} 설치 실패: {(done.stdout + done.stderr)[-500:]}"
            return result

    prompt = PROMPT.format(job_id=job_id, requirement=requirement)
    log_path = logs / f"{job_id}.jsonl"
    with open(log_path, "wb") as out, open(logs / f"{job_id}.stderr", "wb") as err:
        proc = subprocess.Popen(
            resolve_command(agent_command(worktree, prompt, args.model)),
            cwd=worktree,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        try:
            result["exit_code"] = proc.wait(timeout=args.wall_timeout * 60)
            result["timed_out"] = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            result["exit_code"] = None
            result["timed_out"] = True

    job = _load_job(worktree, job_id)
    if result["timed_out"] and job["status"] in ACTIVE - {"APPROVED"}:
        _run(
            [
                sys.executable,
                "scripts/factory.py",
                "block",
                job_id,
                "--reason",
                "TIMEOUT",
                "--detail",
                f"실행 래퍼 벽시계 상한 {args.wall_timeout}분 초과로 세션을 종료함",
            ],
            worktree,
        )
        job = _load_job(worktree, job_id)
    result.update(
        status=job["status"],
        risk=job.get("risk"),
        active_min=round(job["time"]["active_s"] / 60, 1),
        wall_min=round((time.time() - started) / 60, 1),
        blocked=(job.get("blocked") or {}).get("reason"),
        merge_commit=job.get("merge_commit"),
        log=str(log_path),
    )
    # 작업 상태는 worktree 안에 있으므로, worktree를 지우기 전에 기록으로 남긴다
    record = jobs_dir / "records" / f"{job_id}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["record"] = str(record)
    if job["status"] == "MERGED" and not args.keep_merged:
        with _git_lock:
            _git(root, "worktree", "remove", "--force", str(worktree))
        result["worktree_removed"] = True
    return result


def _summary(report: dict[str, Any]) -> str:
    lines = [
        f"인증: {report['auth']}, 동시 실행 {report['max_parallel']}개, base {report['base']}",
        "",
        "| 작업 | 상태 | 위험 | 활성/경과(분) | 비고 | worktree |",
        "|---|---|---|---|---|---|",
    ]
    for job in report["jobs"]:
        note = job.get("blocked") or job.get("error") or ""
        if job.get("timed_out"):
            note = f"벽시계 상한 초과 {note}"
        if job.get("merge_commit"):
            note = f"머지 {job['merge_commit'][:7]}"
        where = "(삭제)" if job.get("worktree_removed") else job.get("worktree", "")
        lines.append(
            f"| {job.get('job_id') or '-'} | {job['status']} | {job.get('risk') or '-'} | "
            f"{job.get('active_min', '-')}/{job.get('wall_min', '-')} | {note} | {where} |"
        )
    lines += [
        "",
        "APPROVAL_REQUIRED·BLOCKED 작업은 해당 worktree에서 사람이 처리한다 "
        "(`python scripts/factory.py approve|resume|drop <ID>`).",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    root = root or Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(prog="dispatch", description="팩토리 병렬 실행")
    parser.add_argument("requirements", nargs="+")
    parser.add_argument("--max-parallel", type=int, default=3)
    parser.add_argument("--wall-timeout", type=float, default=DEFAULT_WALL_TIMEOUT_MIN, help="분")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--require-api-key", action="store_true")
    parser.add_argument("--keep-merged", action="store_true", help="머지된 worktree도 남김")
    args = parser.parse_args(argv)

    try:
        if args.require_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            raise DispatchError("--require-api-key: ANTHROPIC_API_KEY 환경변수가 없습니다")
        base = _git(root, "branch", "--show-current")
        if not base:
            raise DispatchError("기본 checkout이 브랜치 위에 있어야 합니다 (머지 대상)")
        if _git(root, "status", "--porcelain", "--untracked-files=no"):
            raise DispatchError(
                "기본 checkout에 커밋되지 않은 변경이 있습니다 (머지가 이곳에서 일어남)"
            )
    except DispatchError as error:
        print(f"오류: {error}")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    print(f"인증: {auth_mode()}, 작업 {len(args.requirements)}개, 동시 {args.max_parallel}개")
    with ThreadPoolExecutor(max_workers=max(1, args.max_parallel)) as pool:
        futures = [
            pool.submit(run_job, root, base, req, i, args, stamp)
            for i, req in enumerate(args.requirements)
        ]
        jobs = []
        for future, req in zip(futures, args.requirements, strict=True):
            try:
                jobs.append(future.result())
            except DispatchError as error:
                jobs.append(
                    {"requirement": req, "job_id": None, "status": "ERROR", "error": str(error)}
                )
    report = {
        "started": stamp,
        "auth": auth_mode(),
        "base": base,
        "max_parallel": args.max_parallel,
        "model": args.model,
        "jobs": jobs,
    }
    jobs_dir = root.parent / f"{root.name}.jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"dispatch-{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(_summary(report))
    return 0 if all(j["status"] not in {"ERROR", "SETUP_FAILED"} for j in jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
