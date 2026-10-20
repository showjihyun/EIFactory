"""PreToolUse 보호 hook (기획안 6.8, 7.3, 7.6).

도구 호출 직전에 실행되어, 현재 팩토리 작업 단계에 맞지 않는 수정을 막는다.
stdin으로 hook 입력 JSON을 받고, 막을 때는 이유를 stderr에 쓰고 종료 코드 2를 낸다.

이 hook은 마크다운 규칙을 대신하는 강제 장치다. 같은 판단을 factory.py verify가
파일 해시로 한 번 더 확인한다 (이중 방어).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import (  # noqa: E402
    LOCK_PATTERNS,
    STATE_PATH,
    SUMMARY_PATH,
    _matches,
    active_seconds,
    now,
)
from proc import use_utf8_output  # noqa: E402

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
STATE_FILES = {STATE_PATH, SUMMARY_PATH}
TEST_PATTERNS = ["backend/tests/*", "frontend/src/*.test.ts", "frontend/src/*.test.tsx"]
POLICY_AND_CONFIG = [p for p in LOCK_PATTERNS if not _matches(p, TEST_PATTERNS)]
SPEC_STAGE = {"QUEUED", "SPEC_READY"}
BUILD_STAGE = {"BUILDING", "RETRY_REQUIRED"}
STOPPED = {"VERIFYING", "APPROVAL_REQUIRED", "BLOCKED"}
CLOSED = {"APPROVED", "DROPPED"}

FACTORY_CMD = re.compile(r"^\s*(uv run (--\S+ \S+ )*)?python3?\s+scripts/factory\.py\b")
HUMAN_ONLY = re.compile(r"scripts/factory\.py\s+(approve|resume|drop)\b")
GIT_PUSH = re.compile(r"\bgit\s+push\b")
GIT_STATE = re.compile(
    r"\bgit\s+(commit|checkout|switch|reset|rebase|merge|branch|stash|restore|clean|tag|"
    r"cherry-pick|am|apply|worktree)\b"
)
WRITE_HINT = re.compile(r">|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btee\b|\btruncate\b|\bdd\b")


def deny(reason: str) -> None:
    sys.stderr.write(f"[factory guard] {reason}\n")
    sys.exit(2)


def relative(raw: str, root: Path) -> str | None:
    path = Path(raw)
    if not path.is_absolute():
        normalized = PurePosixPath(raw.replace("\\", "/")).as_posix()
        return normalized.removeprefix("./")
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def load_active_job(root: Path) -> dict[str, Any] | None:
    path = root / STATE_PATH
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    active = state.get("active")
    job = state.get("jobs", {}).get(active) if active else None
    if not job or job.get("status") in CLOSED:
        return None
    result: dict[str, Any] = job
    return result


def over_time_cap(job: dict[str, Any]) -> bool:
    segments = job["time"]["segments"]
    running = bool(segments) and segments[-1][1] is None
    return running and active_seconds(job, now()) > job["time"]["cap_s"]


def is_acceptance_test(path: str, job_id: str) -> bool:
    return bool(
        re.match(rf"^backend/tests/acceptance/test_{job_id}\.py$", path)
        or re.match(rf"^frontend/src/(.+/)?\w+\.{job_id}\.accept\.test\.tsx$", path)
    )


def check_write(path: str, job: dict[str, Any] | None) -> None:
    if path in STATE_FILES:
        deny(f"{path}는 직접 고칠 수 없습니다. python scripts/factory.py 명령만 상태를 바꿉니다.")
    if job is None:
        return
    status = job["status"]
    if over_time_cap(job):
        deny("시간 상한을 넘었습니다. 수정을 멈추고 python scripts/factory.py verify로 기록하세요.")
    if status in STOPPED:
        deny(f"작업이 {status} 상태입니다. 수정하지 말고 상태를 보고하세요.")
    if _matches(path, POLICY_AND_CONFIG) and path not in job.get("allowed_config_changes", []):
        deny(f"{path}는 정책·검증 설정 파일이라 팩토리 작업 중에 고칠 수 없습니다.")
    if status in SPEC_STAGE:
        if path == job["spec_path"] or _matches(path, TEST_PATTERNS):
            return
        if is_acceptance_test(path, job["id"]):
            return
        deny(f"Spec 단계에서는 구현 코드를 쓰지 않습니다 ({path}). spec과 테스트만 작성하세요.")
    if status in BUILD_STAGE and path in job.get("locks", {}):
        deny(
            f"{path}는 잠긴 파일입니다 (수락 테스트·contract·기존 테스트). 틀렸다고 판단되면 "
            "멈추고 보고하세요."
        )


def check_bash(command: str, job: dict[str, Any] | None) -> None:
    if HUMAN_ONLY.search(command):
        deny("approve·resume·drop은 사람 전용 명령입니다.")
    if GIT_PUSH.search(command):
        deny("git push는 사람만 합니다.")
    is_factory = bool(FACTORY_CMD.match(command))
    if not is_factory and any(name in command for name in STATE_FILES):
        deny("상태 파일은 python scripts/factory.py 명령으로만 바꿉니다.")
    if job is None or is_factory:
        return
    if over_time_cap(job):
        deny("시간 상한을 넘었습니다. python scripts/factory.py 명령 외에는 실행할 수 없습니다.")
    if GIT_STATE.search(command):
        deny("팩토리 작업 중에는 브랜치·커밋을 팩토리가 관리합니다.")
    if WRITE_HINT.search(command):
        protected = [p for p in job.get("locks", {}) if p in command]
        if protected:
            deny(f"잠긴 파일을 셸 명령으로 바꿀 수 없습니다: {', '.join(protected)}")


def main() -> None:
    use_utf8_output()
    root = Path(os.environ.get("FACTORY_ROOT") or Path(__file__).resolve().parents[2])
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    job = load_active_job(root)
    if tool in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        path = relative(raw, root)
        if path is not None:
            check_write(path, job)
    elif tool == "Bash":
        check_bash(tool_input.get("command", ""), job)


if __name__ == "__main__":
    main()
