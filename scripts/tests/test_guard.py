import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

GUARD = Path(__file__).resolve().parents[1] / "hooks" / "guard.py"
NOW = 1_000_000.0


def make_job(status: str, **overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "J001",
        "status": status,
        "size": "small",
        "acceptance_tests": ["backend/tests/acceptance/test_J001.py"],
        "changed_tests": [],
        "allowed_config_changes": [],
        "spec_path": "factory/specs/J001.md",
        "locks": {
            "backend/tests/acceptance/test_J001.py": "x",
            "backend/tests/test_existing.py": "x",
            "factory/specs/J001.md": "x",
            "Makefile": "x",
        },
        "time": {"segments": [[NOW - 60, None]], "cap_s": 900, "extended": False, "active_s": 60},
    }
    job.update(overrides)
    return job


def call(
    root: Path,
    tool: str,
    tool_input: dict[str, Any],
    job: dict[str, Any] | None = None,
    session: str = "s1",
    args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    state_dir = root / "factory"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "version": 1,
        "active": job["id"] if job else None,
        "jobs": {job["id"]: job} if job else {},
    }
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        input=json.dumps(
            {
                "tool_name": tool,
                "tool_input": tool_input,
                "cwd": str(root),
                "session_id": session,
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"FACTORY_ROOT": str(root), "FACTORY_FAKE_NOW": str(NOW), "PATH": ""},
        check=False,
    )


def allowed(proc: subprocess.CompletedProcess[str]) -> bool:
    assert proc.returncode in (0, 2), proc.stderr
    return proc.returncode == 0


def edit(path: str) -> dict[str, Any]:
    return {"file_path": path, "old_string": "a", "new_string": "b"}


def bash(command: str) -> dict[str, Any]:
    return {"command": command}


# ---------- 작업이 없을 때 ----------


def test_no_job_allows_normal_edits(tmp_path: Path) -> None:
    assert allowed(call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py")))
    assert allowed(call(tmp_path, "Edit", edit("AGENTS.md")))


@pytest.mark.parametrize("path", ["factory/state.json", "factory-state.md"])
def test_state_files_are_never_edited_directly(tmp_path: Path, path: str) -> None:
    proc = call(tmp_path, "Write", {"file_path": path, "content": "{}"})
    assert not allowed(proc)
    assert "factory.py" in proc.stderr


@pytest.mark.parametrize(
    "command",
    [
        "python scripts/factory.py approve J001",
        "python scripts/factory.py resume J001 --extend",
        "python3 scripts/factory.py drop J001",
    ],
)
def test_human_only_commands_are_denied(tmp_path: Path, command: str) -> None:
    proc = call(tmp_path, "Bash", bash(command), make_job("APPROVAL_REQUIRED"))
    assert not allowed(proc)
    assert "사람" in proc.stderr


def test_git_push_is_always_denied(tmp_path: Path) -> None:
    assert not allowed(call(tmp_path, "Bash", bash("git push origin main")))


def test_bash_writing_state_file_is_denied(tmp_path: Path) -> None:
    assert not allowed(call(tmp_path, "Bash", bash("echo {} > factory/state.json")))


# ---------- Spec 단계 ----------


@pytest.mark.parametrize("status", ["QUEUED", "SPEC_READY"])
def test_spec_stage_allows_spec_and_tests(tmp_path: Path, status: str) -> None:
    job = make_job(status, locks={})
    for path in (
        "factory/specs/J001.md",
        "backend/tests/acceptance/test_J001.py",
        "frontend/src/components/BookmarkList.J001.accept.test.tsx",
        "backend/tests/test_bookmarks.py",
    ):
        assert allowed(call(tmp_path, "Write", {"file_path": path, "content": ""}, job)), path


def test_spec_stage_denies_implementation_code(tmp_path: Path) -> None:
    proc = call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), make_job("SPEC_READY"))
    assert not allowed(proc)
    assert "구현" in proc.stderr
    assert not allowed(
        call(tmp_path, "Edit", edit("frontend/src/App.tsx"), make_job("QUEUED", locks={}))
    )


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "CLAUDE.md",
        "factory/PROCESS.md",
        ".claude/settings.json",
        "scripts/verify.py",
        "Makefile",
        "backend/pyproject.toml",
        "docker/proxy/filter",
        ".devcontainer/Dockerfile",
    ],
)
def test_policy_and_config_files_are_denied_during_a_job(tmp_path: Path, path: str) -> None:
    assert not allowed(call(tmp_path, "Edit", edit(path), make_job("BUILDING")))


# ---------- 구현 단계 ----------


@pytest.mark.parametrize("status", ["BUILDING", "RETRY_REQUIRED"])
def test_build_stage_allows_implementation(tmp_path: Path, status: str) -> None:
    assert allowed(call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), make_job(status)))
    assert allowed(
        call(
            tmp_path,
            "Write",
            {"file_path": "backend/tests/test_helper_unit.py", "content": ""},
            make_job(status),
        )
    )


@pytest.mark.parametrize(
    "path",
    [
        "backend/tests/acceptance/test_J001.py",
        "backend/tests/test_existing.py",
        "factory/specs/J001.md",
    ],
)
def test_build_stage_denies_locked_files(tmp_path: Path, path: str) -> None:
    proc = call(tmp_path, "Edit", edit(path), make_job("BUILDING"))
    assert not allowed(proc)
    assert "잠긴" in proc.stderr


def test_allowed_config_change_is_permitted_in_build(tmp_path: Path) -> None:
    job = make_job("BUILDING", allowed_config_changes=["backend/pyproject.toml"])
    assert allowed(call(tmp_path, "Edit", edit("backend/pyproject.toml"), job))


def test_multiedit_and_absolute_paths_are_checked(tmp_path: Path) -> None:
    absolute = str(tmp_path / "backend" / "tests" / "test_existing.py")
    assert not allowed(
        call(tmp_path, "MultiEdit", {"file_path": absolute, "edits": []}, make_job("BUILDING"))
    )


def test_bash_touching_locked_file_is_denied(tmp_path: Path) -> None:
    job = make_job("BUILDING")
    assert not allowed(
        call(tmp_path, "Bash", bash("sed -i s/a/b/ backend/tests/test_existing.py"), job)
    )
    assert allowed(
        call(tmp_path, "Bash", bash("uv run pytest backend/tests/test_existing.py"), job)
    )


@pytest.mark.parametrize(
    "command",
    ["git commit -am x", "git checkout main", "git switch main", "git reset --hard", "git stash"],
)
def test_git_state_changes_are_denied_during_a_job(tmp_path: Path, command: str) -> None:
    assert not allowed(call(tmp_path, "Bash", bash(command), make_job("BUILDING")))


def test_factory_stage_commands_are_allowed(tmp_path: Path) -> None:
    job = make_job("BUILDING")
    assert allowed(call(tmp_path, "Bash", bash("python scripts/factory.py verify J001"), job))
    assert allowed(call(tmp_path, "Bash", bash("git diff --stat"), job))


# ---------- 검증·승인 대기·중단 ----------


@pytest.mark.parametrize("status", ["VERIFYING", "APPROVAL_REQUIRED", "APPROVED", "BLOCKED"])
def test_edits_stop_after_build(tmp_path: Path, status: str) -> None:
    job = make_job(status)
    job["time"]["segments"] = [[NOW - 60, NOW - 10]]
    assert not allowed(call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), job))


# ---------- 시간 상한 ----------


def test_hard_time_cap_stops_everything_but_factory(tmp_path: Path) -> None:
    job = make_job("BUILDING")
    job["time"]["segments"] = [[NOW - 1300, None]]
    proc = call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), job)
    assert not allowed(proc)
    assert "상한" in proc.stderr
    assert not allowed(call(tmp_path, "Bash", bash("uv run pytest"), job))
    assert allowed(call(tmp_path, "Bash", bash("python scripts/factory.py verify J001"), job))


def test_read_tools_are_never_blocked(tmp_path: Path) -> None:
    job = make_job("BUILDING")
    job["time"]["segments"] = [[NOW - 5000, None]]
    assert allowed(call(tmp_path, "Read", {"file_path": "backend/tests/test_existing.py"}, job))


# ---------- Phase 2: 머지·세션 lock ----------


def heartbeat(root: Path) -> dict[str, Any] | None:
    path = root / "factory" / "heartbeat.json"
    if not path.exists():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def set_heartbeat(root: Path, session: str, at: float) -> None:
    (root / "factory").mkdir(parents=True, exist_ok=True)
    (root / "factory" / "heartbeat.json").write_text(
        json.dumps({"session": session, "at": at}), encoding="utf-8"
    )


def test_merged_job_is_closed(tmp_path: Path) -> None:
    job = make_job("MERGED")
    job["time"]["segments"] = [[NOW - 60, NOW - 10]]
    assert allowed(call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), job))


def test_recover_is_allowed_but_force_is_human_only(tmp_path: Path) -> None:
    job = make_job("BUILDING")
    assert allowed(call(tmp_path, "Bash", bash("python scripts/factory.py recover J001"), job))
    proc = call(tmp_path, "Bash", bash("python scripts/factory.py recover J001 --force"), job)
    assert not allowed(proc)
    assert "사람" in proc.stderr


def test_merge_command_is_allowed_for_agents(tmp_path: Path) -> None:
    job = make_job("APPROVED")
    job["time"]["segments"] = [[NOW - 60, NOW - 10]]
    assert allowed(call(tmp_path, "Bash", bash("python scripts/factory.py merge J001"), job))


def test_heartbeat_file_is_never_edited_directly(tmp_path: Path) -> None:
    proc = call(tmp_path, "Write", {"file_path": "factory/heartbeat.json", "content": "{}"})
    assert not allowed(proc)


def test_allowed_call_records_heartbeat(tmp_path: Path) -> None:
    assert allowed(
        call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), make_job("BUILDING"))
    )
    assert heartbeat(tmp_path) == {"session": "s1", "at": NOW}


def test_no_heartbeat_without_active_job(tmp_path: Path) -> None:
    assert allowed(call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py")))
    assert heartbeat(tmp_path) is None


def test_other_live_session_is_locked_out(tmp_path: Path) -> None:
    set_heartbeat(tmp_path, "s1", NOW - 30)
    job = make_job("BUILDING")
    proc = call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), job, session="s2")
    assert not allowed(proc)
    assert "다른 세션" in proc.stderr
    assert not allowed(call(tmp_path, "Bash", bash("uv run pytest"), job, session="s2"))
    assert not allowed(
        call(tmp_path, "Bash", bash("python scripts/factory.py verify J001"), job, session="s2")
    )
    assert allowed(
        call(tmp_path, "Bash", bash("python scripts/factory.py status"), job, session="s2")
    )
    assert heartbeat(tmp_path) == {"session": "s1", "at": NOW - 30}


def test_stale_session_must_be_recovered_first(tmp_path: Path) -> None:
    set_heartbeat(tmp_path, "s1", NOW - 3600)
    job = make_job("BUILDING")
    proc = call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), job, session="s2")
    assert not allowed(proc)
    assert "recover" in proc.stderr
    # recover 자체는 통과시키고, heartbeat는 건드리지 않는다 (recover가 지운다)
    assert allowed(
        call(tmp_path, "Bash", bash("python scripts/factory.py recover J001"), job, session="s2")
    )
    assert heartbeat(tmp_path) == {"session": "s1", "at": NOW - 3600}


def test_same_session_keeps_working(tmp_path: Path) -> None:
    set_heartbeat(tmp_path, "s1", NOW - 3600)
    job = make_job("BUILDING")
    assert allowed(call(tmp_path, "Edit", edit("backend/src/bookmarks/main.py"), job))
    assert heartbeat(tmp_path) == {"session": "s1", "at": NOW}


def test_dispatch_is_human_only(tmp_path: Path) -> None:
    for job in (None, make_job("BUILDING")):
        proc = call(tmp_path, "Bash", bash("python scripts/dispatch.py 요구사항"), job)
        assert not allowed(proc)
        assert "사람" in proc.stderr


# ---------- Phase 3c: Codex 도구 호출, Builder 모델 ----------
# Codex(0.163)의 hook 입력은 가짜 모델 서버로 실제 실행해 확인한 형태다:
# 셸은 tool_name "Bash" + {"command": "<문자열>"}, 파일 수정은 "apply_patch" + {"command": 패치},
# 하위 에이전트는 "collaborationspawn_agent". Codex는 종료 코드 2를 hook 실패로 보고 호출을
# 그대로 실행하므로, `--json-deny`일 때는 종료 코드 0 + 판정 JSON으로 막는다.


def patch(*paths: str) -> dict[str, Any]:
    body = "".join(f"*** Update File: {p}\n@@\n-a\n+b\n" for p in paths)
    return {"command": f"*** Begin Patch\n{body}*** End Patch\n"}


def codex(root: Path, tool: str, tool_input: dict[str, Any], job: dict[str, Any]) -> bool:
    """Codex 방식으로 hook을 불러 허용 여부를 돌려준다."""
    proc = call(root, tool, tool_input, job, args=("--json-deny",))
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return True
    decision = json.loads(proc.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    return False


def test_codex_denial_exits_zero_with_decision_json(tmp_path: Path) -> None:
    proc = call(tmp_path, "Bash", bash("git push"), make_job("BUILDING"), args=("--json-deny",))
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "push" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "python scripts/factory.py approve J001",
        "python scripts/dispatch.py x",
        "git commit -am x",
    ],
)
def test_codex_shell_calls_are_checked(tmp_path: Path, command: str) -> None:
    assert not codex(tmp_path, "Bash", bash(command), make_job("BUILDING"))


def test_codex_factory_commands_are_allowed(tmp_path: Path) -> None:
    assert codex(
        tmp_path, "Bash", bash("python scripts/factory.py verify J001"), make_job("BUILDING")
    )


def test_codex_apply_patch_paths_are_checked(tmp_path: Path) -> None:
    job = make_job("BUILDING")
    assert not codex(tmp_path, "apply_patch", patch("Makefile"), job)
    assert not codex(tmp_path, "apply_patch", patch("backend/src/bookmarks/x.py", "Makefile"), job)
    assert codex(tmp_path, "apply_patch", patch("backend/src/bookmarks/x.py"), job)
    assert not codex(
        tmp_path, "apply_patch", patch("backend/src/bookmarks/x.py"), make_job("SPEC_READY")
    )


def test_patch_text_is_not_treated_as_a_shell_command(tmp_path: Path) -> None:
    # 패치 본문에 "git push" 같은 글자가 있어도 셸 명령으로 보지 않는다
    text = patch("backend/src/bookmarks/x.py")["command"].replace("+b", "+# git push 금지")
    assert codex(tmp_path, "apply_patch", {"command": text}, make_job("BUILDING"))


@pytest.mark.parametrize(
    ("builder_model", "requested", "ok"),
    [
        ("sonnet", None, True),
        ("sonnet", "sonnet", True),
        ("opus", None, False),
        ("opus", "sonnet", False),
        ("opus", "opus", True),
    ],
)
def test_claude_builder_must_run_on_the_model_factory_chose(
    tmp_path: Path, builder_model: str, requested: str | None, ok: bool
) -> None:
    job = make_job("RETRY_REQUIRED", builder_model=builder_model, agent="claude")
    tool_input: dict[str, Any] = {"subagent_type": "builder", "prompt": "job ID: J001"}
    if requested:
        tool_input["model"] = requested
    for tool in ("Agent", "Task"):
        assert allowed(call(tmp_path, tool, tool_input, job)) is ok


@pytest.mark.parametrize(
    ("tool_input", "ok"),
    [
        ({"task_name": "builder", "model": "gpt-6-astra", "fork_turns": "none"}, True),
        ({"task_name": "builder", "model": "gpt-6.1-sol", "fork_turns": "none"}, False),
        ({"task_name": "builder", "fork_turns": "none"}, False),  # 모델 생략 = 부모 모델 상속
        ({"task_name": "builder", "model": "gpt-6-astra"}, False),  # 대화 상속 (기본 all)
        ({"task_name": "builder_retry", "model": "gpt-6-astra", "fork_turns": "all"}, False),
        ({"task_name": "explorer", "fork_turns": "all"}, True),  # Builder가 아닌 하위 에이전트
    ],
)
def test_codex_builder_spawn_is_checked(
    tmp_path: Path, tool_input: dict[str, Any], ok: bool
) -> None:
    job = make_job("RETRY_REQUIRED", builder_model="gpt-6-astra", agent="codex")
    tool_input = {"message": "job ID: J001", **tool_input}
    assert codex(tmp_path, "collaborationspawn_agent", tool_input, job) is ok
