"""PreToolUse 보호 hook (기획안 6.3, 6.8, 7.3, 7.6).

도구 호출 직전에 실행되어, 현재 팩토리 작업 단계에 맞지 않는 수정을 막는다.
stdin으로 hook 입력 JSON을 받고, 막을 때는 이유를 stderr에 쓰고 종료 코드 2를 낸다.

이 hook은 마크다운 규칙을 대신하는 강제 장치다. 같은 판단을 factory.py verify가
파일 해시로 한 번 더 확인한다 (이중 방어).

진행 중 작업이 있으면 허용한 호출마다 heartbeat(세션 ID, 시각)를 남긴다. 다른 세션의
heartbeat가 있으면 그 세션이 살아 있는 동안은 막고, 끊긴 뒤에는 recover로 이어받게 한다.

Claude Code와 Codex가 같은 hook을 쓴다 (기획안 6.7). Codex의 `apply_patch`(본문의 파일 경로)와
하위 에이전트 생성(`collaborationspawn_agent`)도 같은 규칙으로 검사한다.
막는 방법은 도구마다 다르다. Claude Code는 종료 코드 2 + stderr. Codex는 종료 코드 2를 hook 실패로
보고 호출을 그대로 실행하므로, `--json-deny`(Codex용)일 때는 종료 코드 0 + 판정 JSON으로 막는다.
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
    HEARTBEAT_PATH,
    LOCK_PATTERNS,
    LOCK_TTL_S,
    STATE_PATH,
    SUMMARY_PATH,
    _matches,
    active_seconds,
    now,
    read_heartbeat,
)
from proc import use_utf8_output  # noqa: E402

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SUBAGENT_TOOLS = {"Agent", "Task", "collaborationspawn_agent", "spawn_agent"}
JSON_DENY = "--json-deny" in sys.argv[1:]
BUILDER_AGENT = ".claude/agents/builder.md"
PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)
STATE_FILES = {STATE_PATH, SUMMARY_PATH, HEARTBEAT_PATH}
TEST_PATTERNS = ["backend/tests/*", "frontend/src/*.test.ts", "frontend/src/*.test.tsx"]
POLICY_AND_CONFIG = [p for p in LOCK_PATTERNS if not _matches(p, TEST_PATTERNS)]
SPEC_STAGE = {"QUEUED", "SPEC_READY"}
BUILD_STAGE = {"BUILDING", "RETRY_REQUIRED"}
STOPPED = {"VERIFYING", "APPROVAL_REQUIRED", "APPROVED", "BLOCKED"}
CLOSED = {"MERGED", "DROPPED"}

FACTORY_CMD = re.compile(r"^\s*(uv run (--\S+ \S+ )*)?python3?\s+scripts/factory\.py\b")
HUMAN_ONLY = re.compile(r"scripts/factory\.py\s+(approve\b|resume\b|drop\b|recover\b.*--force)")
LOCK_FREE = re.compile(r"scripts/factory\.py\s+(status|recover)\b")
DISPATCH = re.compile(r"scripts/dispatch\.py\b")
GIT_PUSH = re.compile(r"\bgit\s+push\b")
GIT_STATE = re.compile(
    r"\bgit\s+(commit|checkout|switch|reset|rebase|merge|branch|stash|restore|clean|tag|"
    r"cherry-pick|am|apply|worktree)\b"
)
WRITE_HINT = re.compile(r">|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btee\b|\btruncate\b|\bdd\b")


def deny(reason: str) -> None:
    message = f"[factory guard] {reason}"
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": message,
    }
    sys.stdout.write(json.dumps({"hookSpecificOutput": decision}, ensure_ascii=False) + "\n")
    sys.stderr.write(message + "\n")
    sys.exit(0 if JSON_DENY else 2)


def is_patch(text: str) -> bool:
    return "*** Begin Patch" in text


def shell_command(tool_input: dict[str, Any]) -> str | None:
    """셸 명령 문자열. 목록 형태(["bash", "-lc", "<명령>"])도 받는다. 패치 본문은 명령이 아니다."""
    raw = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
        raw = parts[2] if len(parts) >= 3 and parts[1] in {"-c", "-lc"} else " ".join(parts)
    if not isinstance(raw, str) or is_patch(raw):
        return None
    return raw


def patch_paths(tool_input: dict[str, Any]) -> list[str]:
    """apply_patch 본문에서 바뀌는 파일 경로를 뽑는다."""
    paths: list[str] = []
    for value in tool_input.values():
        if isinstance(value, str) and is_patch(value):
            paths += [m.strip() for m in PATCH_PATH.findall(value)]
    return paths


def _frontmatter_model(root: Path) -> str | None:
    try:
        text = (root / BUILDER_AGENT).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^model:\s*(\S+)", text, re.M)
    return match.group(1) if match else None


def check_subagent(
    root: Path, tool: str, tool_input: dict[str, Any], job: dict[str, Any] | None
) -> None:
    """Builder는 factory.py가 정한 모델로만 띄운다 (실패 누적 시 승격, 기획안 6.6)."""
    required = job.get("builder_model") if job else None
    if not required:
        return
    if tool in {"Agent", "Task"}:  # Claude Code: builder 서브에이전트 (모델 생략 시 frontmatter)
        if tool_input.get("subagent_type") != "builder":
            return
        requested = tool_input.get("model") or _frontmatter_model(root) or "sonnet"
        how = f"builder를 model: {required}로 호출하세요."
    else:  # Codex: spawn_agent(task_name="builder...", model=..., fork_turns="none")
        if not str(tool_input.get("task_name", "")).startswith("builder"):
            return
        requested = tool_input.get("model")
        how = f'spawn_agent에 model: "{required}", fork_turns: "none"을 넣으세요.'
        if tool_input.get("fork_turns") != "none":
            deny("Builder는 오케스트레이터의 대화를 넘겨받지 않습니다 (fork_turns: none). " + how)
    if requested != required:
        deny(f"이 작업의 Builder 모델은 {required}입니다 (factory.py가 정함). {how}")


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


def check_session(root: Path, session: str, tool: str, command: str) -> bool:
    """다른 세션이 작업 중이면 막는다. heartbeat를 갱신해도 되면 True."""
    lock_free = tool == "Bash" and bool(FACTORY_CMD.match(command) and LOCK_FREE.search(command))
    beat = read_heartbeat(root)
    if beat is None or beat.get("session") == session:
        return not lock_free
    if lock_free:
        return False
    idle = now() - float(beat.get("at", 0))
    if idle < LOCK_TTL_S:
        deny(
            f"다른 세션이 {idle:.0f}초 전까지 이 작업을 진행했습니다. 같은 작업을 두 세션이 "
            "동시에 진행하지 않습니다. 상태만 확인하고 멈추세요."
        )
    deny(
        "이전 세션이 끊긴 작업입니다. 먼저 python scripts/factory.py recover <작업ID>로 "
        "이어받으세요."
    )
    return False


def write_heartbeat(root: Path, session: str) -> None:
    path = root / HEARTBEAT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session": session, "at": now()}), encoding="utf-8")


def check_bash(command: str, job: dict[str, Any] | None) -> None:
    if HUMAN_ONLY.search(command):
        deny("approve·resume·drop·recover --force는 사람 전용 명령입니다.")
    if DISPATCH.search(command):
        deny("dispatch.py(병렬 실행)는 사람 전용입니다. 세션 안에서 다른 세션을 띄우지 않습니다.")
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
    if not isinstance(tool_input, dict):
        tool_input = {"command": tool_input}
    job = load_active_job(root)
    session = str(payload.get("session_id") or "unknown")
    command = shell_command(tool_input) if tool not in WRITE_TOOLS | SUBAGENT_TOOLS else None
    record = job is not None and check_session(
        root, session, "Bash" if command is not None else tool, command or ""
    )
    if tool in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        path = relative(raw, root)
        if path is not None:
            check_write(path, job)
    elif tool in SUBAGENT_TOOLS:
        check_subagent(root, tool, tool_input, job)
    elif command is not None:
        check_bash(command, job)
    for raw in patch_paths(tool_input):
        path = relative(raw, root)
        if path is not None:
            check_write(path, job)
    if record:
        write_heartbeat(root, session)


if __name__ == "__main__":
    main()
