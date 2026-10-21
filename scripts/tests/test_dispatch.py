import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import dispatch  # noqa: E402

# check_red·verify·validator·style를 흉내 낸다. 동시에 여러 프로세스가 부르므로 상태를 쓰지 않는다.
FAKE_TOOL = r"""
import json, sys
kind = sys.argv[1]
if kind == "check_red":
    expect = sys.argv[sys.argv.index("--expect") + 1]
    verdict = "GREEN_OK" if expect == "green" else "RED_OK"
    outcome = "passed" if expect == "green" else "assertion_failed"
    tests = [{"id": "t", "outcome": outcome}]
    print(json.dumps({"verdict": verdict, "exit_code": 0, "tests": tests}))
elif kind == "style":
    print(json.dumps({"ok": True, "problems": []}))
elif kind == "verify":
    print("verify: PASS (exit 0)")
elif kind == "validator":
    sys.stdin.read()
    print(json.dumps({"type": "result", "result": '{"verdict": "PASS", "issues": []}'}))
"""

# 오케스트레이터 세션을 흉내 낸다: 배정된 작업을 spec부터 merge까지 진행한다.
FAKE_AGENT = r"""
import json, os, subprocess, sys, time
from pathlib import Path
if os.environ.get("FAKE_AGENT_MODE") == "sleep":
    time.sleep(120)
root = Path.cwd()
state = json.loads((root / "factory/state.json").read_text(encoding="utf-8"))
job_id = state["active"]
contract = {
    "type": "feature", "size": "small", "risk": "LOW", "risk_reason": "새 모듈",
    "acceptance_criteria": ["모듈이 있다"],
    "acceptance_tests": [f"backend/tests/acceptance/test_{job_id}.py"],
    "changed_tests": [], "allowed_config_changes": [],
}
spec = root / f"factory/specs/{job_id}.md"
spec.parent.mkdir(parents=True, exist_ok=True)
spec.write_text("```json\n" + json.dumps(contract) + "\n```\n", encoding="utf-8")
test = root / contract["acceptance_tests"][0]
test.write_text("def test_x():\n    assert False\n", encoding="utf-8")
def factory(*args):
    cmd = [sys.executable, "scripts/factory.py", *args]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    print(args[0], out.returncode, out.stdout.strip()[-200:], flush=True)
    return out.returncode
factory("spec", job_id)
factory("red", job_id)
(root / f"backend/src/bookmarks/mod_{job_id}.py").write_text("VALUE = 1\n", encoding="utf-8")
factory("verify", job_id)
factory("validate", job_id)
factory("merge", job_id)
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    for rel, text in {
        "AGENTS.md": "# rules\n",
        "backend/src/bookmarks/main.py": "def list_items():\n    return []\n",
        "backend/tests/acceptance/__init__.py": "",
        ".claude/settings.json": json.dumps(
            {"permissions": {"allow": ["Bash(python scripts/factory.py spec:*)"]}}
        ),
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "scripts").mkdir()
    for name in ("factory.py", "proc.py"):
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    git(root, "init", "-q", "-b", "main")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")

    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    agent = tmp_path / "fake_agent.py"
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    for kind in ("check_red", "style", "verify", "validator"):
        monkeypatch.setenv(
            f"FACTORY_{kind.upper()}_CMD", json.dumps([sys.executable, str(tool), kind])
        )
    monkeypatch.setenv("FACTORY_AGENT_CMD", json.dumps([sys.executable, str(agent)]))
    monkeypatch.setenv("FACTORY_SETUP_CMD", "[]")
    monkeypatch.setenv("FACTORY_MERGE_WAIT_S", "60")
    monkeypatch.delenv("FAKE_AGENT_MODE", raising=False)
    return root


def run(repo: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    code = dispatch.main(list(argv), root=repo)
    reports = sorted((repo.parent / f"{repo.name}.jobs").glob("dispatch-*.json"))
    report: dict[str, Any] = json.loads(reports[-1].read_text(encoding="utf-8")) if reports else {}
    return code, report


def test_three_parallel_jobs_all_merge_without_conflict(repo: Path) -> None:
    code, report = run(repo, "--max-parallel", "3", "첫째", "둘째", "셋째")
    assert code == 0
    jobs = report["jobs"]
    assert [j["status"] for j in jobs] == ["MERGED", "MERGED", "MERGED"]
    ids = {j["job_id"] for j in jobs}
    assert len(ids) == 3
    for job_id in ids:
        assert (repo / f"backend/src/bookmarks/mod_{job_id}.py").exists()
    assert len(git(repo, "rev-list", "--merges", "main").splitlines()) == 3
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert git(repo, "branch", "--show-current") == "main"
    # 머지된 작업의 worktree는 지운다
    assert git(repo, "worktree", "list").count("\n") == 0
    assert report["max_parallel"] == 3


def test_wall_clock_timeout_kills_session_and_blocks_job(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODE", "sleep")
    code, report = run(repo, "--wall-timeout", "0.05", "오래 걸리는 작업")
    job = report["jobs"][0]
    assert job["status"] == "BLOCKED"
    assert job["timed_out"] is True
    state = json.loads((Path(job["worktree"]) / "factory/state.json").read_text(encoding="utf-8"))
    assert state["jobs"][job["job_id"]]["blocked"]["reason"] == "TIMEOUT"
    # 사람이 볼 수 있도록 worktree는 남긴다
    assert Path(job["worktree"]).exists()


def test_refuses_dirty_base_checkout(repo: Path) -> None:
    (repo / "AGENTS.md").write_text("고치는 중\n", encoding="utf-8")
    assert dispatch.main(["요구사항"], root=repo) == 2


def test_require_api_key(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert dispatch.main(["--require-api-key", "요구사항"], root=repo) == 2
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    assert dispatch.auth_mode() == "API 키 (환경변수)"


def test_agent_command_uses_project_allow_list(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    cmd = dispatch.agent_command(repo, "프롬프트", "claude-opus-5-5")
    assert cmd[:3] == ["claude", "-p", "프롬프트"]
    tools = cmd[cmd.index("--allowedTools") + 1 :]
    assert "Bash(python scripts/factory.py spec:*)" in tools
    assert "Read" in tools and "Agent" in tools
    assert not any("approve" in t for t in tools)


def test_api_key_never_appears_in_command_or_report(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy-should-not-leak")
    _, report = run(repo, "요구사항")
    assert "sk-dummy-should-not-leak" not in json.dumps(report)
    assert report["auth"] == "API 키 (환경변수)"
    assert os.environ["ANTHROPIC_API_KEY"] not in " ".join(
        dispatch.agent_command(repo, "p", "claude-opus-5-5")
    )


def test_setup_runs_in_final_worktree_path(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 의존성 설치(가상환경)는 절대 경로를 품으므로, worktree를 옮긴 뒤에 해야 한다
    recorder = tmp_path / "record_cwd.py"
    log = tmp_path / "setup_cwd.txt"
    recorder.write_text(
        "import os, sys\nopen(sys.argv[1], 'a', encoding='utf-8').write(os.getcwd())\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "FACTORY_SETUP_CMD", json.dumps([["backend", [sys.executable, str(recorder), str(log)]]])
    )
    _, report = run(repo, "--keep-merged", "요구사항")
    job = report["jobs"][0]
    recorded = Path(log.read_text(encoding="utf-8").strip())
    assert recorded == Path(job["worktree"]) / "backend"
