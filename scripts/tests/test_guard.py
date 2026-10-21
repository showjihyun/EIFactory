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
        [sys.executable, str(GUARD)],
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
