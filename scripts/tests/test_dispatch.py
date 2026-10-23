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
    print(json.dumps({"type": "result", "result": '{"verdict": "PASS", "issues": []}',
                      "total_cost_usd": 0.01}))
"""

# 오케스트레이터 세션을 흉내 낸다: 배정된 작업을 spec부터 merge까지 진행한다.
FAKE_AGENT = r"""
import json, os, subprocess, sys, time
from pathlib import Path
if os.environ.get("FAKE_AGENT_MODE") == "sleep":
    time.sleep(120)
if os.environ.get("FAKE_AGENT_MODE") == "budget":
    # claude -p --max-budget-usd: 상한에 닿으면 작업 도중 결과 이벤트를 내고 끝난다
    print(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                      "total_cost_usd": 5.2, "usage": {"input_tokens": 10, "output_tokens": 5}}))
    sys.exit(1)
root = Path.cwd()
print("session-env", os.environ.get("FACTORY_AGENT_SESSION"), os.environ.get("FACTORY_AGENT"))
state = json.loads((root / "factory/state.json").read_text(encoding="utf-8"))
job_id = state["active"]
contract = {
    "type": "feature", "size": "small", "risk": os.environ.get("FAKE_AGENT_RISK", "LOW"),
    "risk_reason": "새 모듈",
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
print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.5,
                  "usage": {"input_tokens": 100, "output_tokens": 20}}))
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
    for name in ("factory.py", "factory_config.py", "proc.py"):
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    (root / "factory").mkdir()
    shutil.copy(SCRIPTS.parent / "factory" / "config.json", root / "factory" / "config.json")
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
    monkeypatch.delenv("FAKE_AGENT_RISK", raising=False)
    monkeypatch.delenv("FACTORY_UNTRUSTED_STATE", raising=False)
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
    # worktree를 지워도 작업 기록(상태)은 남긴다
    for job in jobs:
        record = json.loads(Path(job["record"]).read_text(encoding="utf-8"))
        assert record["status"] == "MERGED"
        assert record["merge_commit"] == job["merge_commit"]


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


# ---------- Phase 3b: 컨테이너 격리 ----------


def docker_job(tmp_path: Path) -> "dispatch.DockerJob":
    worktree = tmp_path / "repo.jobs" / "J001"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {(tmp_path / 'repo/.git/worktrees/slot-1').as_posix()}\n", encoding="utf-8"
    )
    return dispatch.DockerJob(
        root=tmp_path / "repo", worktree=worktree, job_id="J001", stamp="s1", image="agent:t"
    )


def mounts(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]


def test_gitdir_name_comes_from_worktree_git_file(tmp_path: Path) -> None:
    assert docker_job(tmp_path).gitdir_name == "slot-1"


def test_session_container_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy-should-not-leak")
    job = docker_job(tmp_path)
    cmd = dispatch.docker_session_command(job, ["claude", "-p", "x"])
    assert cmd[cmd.index("--network") + 1] == dispatch.INTERNAL_NETWORK
    assert "HTTPS_PROXY=http://factory-proxy:3128" in cmd
    assert "FACTORY_MERGE_ON_HOST=1" in cmd
    # 키는 이름만 넘겨 값이 명령줄에 나타나지 않는다
    assert cmd[cmd.index("ANTHROPIC_API_KEY") - 1] == "-e"
    assert "sk-dummy-should-not-leak" not in " ".join(cmd)
    # 공유 .git은 읽기 전용, worktree의 .git 파일은 컨테이너 경로로 바꿔 끼운다
    m = mounts(cmd)
    assert any("target=/repo-git" in x and x.endswith(",readonly") for x in m)
    assert any("target=/workspaces/bookmark-factory/.git," in x and "readonly" in x for x in m)
    assert job.dotgit.read_text(encoding="utf-8") == "gitdir: /repo-git/worktrees/slot-1\n"
    assert cmd[-3:] == ["claude", "-p", "x"]


def test_setup_container_has_network_but_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    cmd = dispatch.docker_setup_command(docker_job(tmp_path))
    assert cmd[cmd.index("--network") + 1] == "bridge"
    assert "ANTHROPIC_API_KEY" not in cmd


def test_tool_container_for_host_merge_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    cmd = dispatch.docker_tool_command(docker_job(tmp_path), ["python", "scripts/verify.py"])
    assert cmd[cmd.index("--network") + 1] == dispatch.INTERNAL_NETWORK
    assert "ANTHROPIC_API_KEY" not in cmd
    assert not any(a.startswith("HTTPS_PROXY") for a in cmd)
    assert cmd[-2:] == ["python", "scripts/verify.py"]


def test_factory_settings_are_passed_into_container_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_VALIDATOR_CMD", '["python", "fake.py"]')
    cmd = dispatch.docker_session_command(docker_job(tmp_path), ["x"])
    assert cmd[cmd.index("FACTORY_VALIDATOR_CMD") - 1] == "-e"
    assert "FACTORY_AGENT_CMD" not in cmd


def test_docker_mode_requires_api_key(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    assert dispatch.main(["--isolation", "docker", "요구사항"], root=repo) == 2


# ---------- Phase 3c: Codex ----------

REPO_ROOT = SCRIPTS.parent


def test_sessions_run_as_agent_sessions_with_chosen_tool(repo: Path) -> None:
    code, report = run(repo, "--agent", "codex", "요구사항")
    job = report["jobs"][0]
    assert job["status"] == "MERGED"
    assert report["agent"] == "codex"
    record = json.loads(Path(job["record"]).read_text(encoding="utf-8"))
    assert record["agent"] == "codex"
    assert record["builder_model"] == "gpt-6.1-sol"
    # 사람 전용 명령은 factory.py가 거부하도록 세션에 표시를 남긴다
    log = Path(job["log"]).read_bytes().decode("utf-8", errors="replace")
    assert "session-env 1 codex" in log


def test_codex_host_command(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    cmd = dispatch.agent_command(repo, "프롬프트", "gpt-6-astra", agent="codex")
    assert cmd[:4] == ["codex", "exec", "--model", "gpt-6-astra"]
    assert cmd[-1] == "프롬프트"
    assert "--json" in cmd
    # 프로젝트 hook(.codex/hooks.json)은 신뢰된 프로젝트에서만 읽힌다. 없으면 hook 없이 돈다
    assert "--dangerously-bypass-hook-trust" in cmd
    assert f"projects.'{repo}'.trust_level=\"trusted\"" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    # 공유 .git(hooks, config 포함)에 쓰기 권한을 주지 않는다. 머지는 세션이 끝난 뒤 호스트가 한다
    assert "--add-dir" not in cmd


def test_host_merges_for_sessions_without_git_write(repo: Path) -> None:
    assert dispatch.merges_on_host("codex", docker=False)
    assert dispatch.merges_on_host("claude", docker=True)
    assert not dispatch.merges_on_host("claude", docker=False)


def test_codex_container_command_uses_container_as_sandbox(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    cmd = dispatch.agent_command(
        repo, "p", "gpt-6-astra", agent="codex", workdir=dispatch.CONTAINER_WORKDIR
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--sandbox" not in cmd
    assert f"projects.'{dispatch.CONTAINER_WORKDIR}'.trust_level=\"trusted\"" in cmd


def test_codex_extra_args_are_appended(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    monkeypatch.setenv("FACTORY_CODEX_ARGS", json.dumps(["-c", "model_provider=x"]))
    cmd = dispatch.agent_command(repo, "p", "m", agent="codex")
    assert cmd[-3:] == ["-c", "model_provider=x", "p"]


def test_codex_container_gets_codex_key_by_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-dummy")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    cmd = dispatch.docker_session_command(docker_job(tmp_path), ["x"], keys=["CODEX_API_KEY"])
    assert cmd[cmd.index("CODEX_API_KEY") - 1] == "-e"
    assert "ANTHROPIC_API_KEY" not in cmd
    assert "sk-codex-dummy" not in " ".join(cmd)
    assert "FACTORY_AGENT_SESSION=1" in cmd


@pytest.mark.parametrize(
    ("env", "agents", "expected"),
    [
        ({"ANTHROPIC_API_KEY": "a"}, ["claude"], ["ANTHROPIC_API_KEY"]),
        ({"CODEX_API_KEY": "c", "OPENAI_API_KEY": "o"}, ["codex"], ["CODEX_API_KEY"]),
        ({"OPENAI_API_KEY": "o"}, ["codex"], ["OPENAI_API_KEY"]),
        ({"ANTHROPIC_API_KEY": "a"}, ["codex"], None),
        (
            {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"},
            ["codex", "claude"],
            ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
        ),
    ],
)
def test_api_key_names_per_agent_tool(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    agents: list[str],
    expected: list[str] | None,
) -> None:
    for name in ("ANTHROPIC_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if expected is None:
        with pytest.raises(dispatch.DispatchError):
            dispatch.api_key_names(agents)
    else:
        assert dispatch.api_key_names(agents) == expected


def test_codex_docker_mode_requires_codex_key(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    assert dispatch.main(["--agent", "codex", "--isolation", "docker", "x"], root=repo) == 2


def test_proxy_allows_codex_api_and_image_has_codex() -> None:
    allowed = (REPO_ROOT / "docker/proxy/filter").read_text(encoding="utf-8").split()
    assert allowed == [r"^api\.anthropic\.com$", r"^api\.openai\.com$"]
    dockerfile = (REPO_ROOT / ".devcontainer/Dockerfile").read_text(encoding="utf-8")
    assert "@openai/codex@" in dockerfile


def test_codex_requires_docker_isolation(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 호스트 실행이면 rebase 후 재검증이 에이전트가 쓴 코드를 샌드박스 밖(호스트)에서 돌린다
    # (보안 리뷰 지적). Windows에서는 Codex 샌드박스가 읽기 전용으로 떨어지기도 한다 (실측)
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    monkeypatch.setenv("CODEX_API_KEY", "dummy")
    assert dispatch.main(["--agent", "codex", "x"], root=repo) == 2
    assert dispatch.main(["--agent", "codex", "--isolation", "host", "x"], root=repo) == 2


def test_extra_no_proxy_hosts_for_internal_test_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_DOCKER_NO_PROXY", "fake-openai")
    cmd = dispatch.docker_session_command(docker_job(tmp_path), ["x"], keys=[])
    assert "NO_PROXY=localhost,127.0.0.1,fake-openai" in cmd
    assert "FACTORY_DOCKER_NO_PROXY" not in cmd


def test_image_playwright_matches_frontend_version() -> None:
    # 검증 컨테이너는 오프라인이므로 브라우저를 이미지에 넣는다. 버전이 다르면 브라우저를 못 찾는다
    package = json.loads((REPO_ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    version = package["devDependencies"]["@playwright/test"]
    dockerfile = (REPO_ROOT / ".devcontainer/Dockerfile").read_text(encoding="utf-8")
    assert f"ARG PLAYWRIGHT_VERSION={version}\n" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" in dockerfile


def test_failed_worktree_removal_keeps_the_job_result(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 경로 길이 제한 등으로 worktree를 못 지워도 머지 결과를 잃지 않는다 (Codex e2e에서 발견)
    real_git = dispatch._git

    def failing_git(root: Path, *args: str) -> str:
        if "remove" in args:
            raise dispatch.DispatchError("git worktree remove 실패: Filename too long")
        return real_git(root, *args)

    monkeypatch.setattr(dispatch, "_git", failing_git)
    monkeypatch.setattr(dispatch, "_force_remove", lambda path: False)
    code, report = run(repo, "요구사항")
    job = report["jobs"][0]
    assert job["status"] == "MERGED"
    assert job["worktree_removed"] is False
    assert "Filename too long" in job["cleanup_error"]
    assert code == 0


def test_force_remove_deletes_read_only_files(tmp_path: Path) -> None:
    # 테스트가 만든 Git 저장소의 objects는 읽기 전용이라 Windows에서 그냥은 지워지지 않는다
    target = tmp_path / "wt"
    obj = target / ".pytest_tmp" / "repo" / ".git" / "objects" / "ab" / "cdef"
    obj.parent.mkdir(parents=True)
    obj.write_text("x", encoding="utf-8")
    obj.chmod(0o444)
    assert dispatch._force_remove(target) is True
    assert not target.exists()


def test_agent_command_passes_budget_cap_to_claude(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACTORY_AGENT_CMD")
    cmd = dispatch.agent_command(repo, "p", "claude-opus-5-5", budget_usd=3.0)
    # --allowedTools는 가변 인자라 그 앞에 둔다
    assert cmd[cmd.index("--max-budget-usd") + 1] == "3.0"
    assert cmd.index("--max-budget-usd") < cmd.index("--allowedTools")
    assert "--max-budget-usd" not in dispatch.agent_command(repo, "p", "claude-opus-5-5")
    # Codex CLI에는 비용 상한 옵션이 없다 (벽시계 상한만 적용)
    codex = dispatch.agent_command(repo, "p", "m", agent="codex", budget_usd=3.0)
    assert "--max-budget-usd" not in codex


def test_session_cost_is_recorded(repo: Path) -> None:
    code, report = run(repo, "요구사항")
    job = report["jobs"][0]
    assert job["status"] == "MERGED"
    assert job["session_cost_usd"] == 0.5
    assert job["session_tokens"] == 120
    assert report["budget_usd"] == dispatch.DEFAULT_BUDGET_USD


def test_budget_exhaustion_blocks_job(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODE", "budget")
    code, report = run(repo, "--budget-usd", "5", "비싼 작업")
    job = report["jobs"][0]
    assert job["status"] == "BLOCKED"
    assert job["blocked"] == "BUDGET"
    assert job["session_cost_usd"] == 5.2
    state = json.loads((Path(job["worktree"]) / "factory/state.json").read_text(encoding="utf-8"))
    assert "$5.20" in state["jobs"][job["job_id"]]["blocked"]["detail"]
    assert Path(job["worktree"]).exists()


# ---------- 사람 승인 (--approve / --reject) ----------


def waiting_job(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("FAKE_AGENT_RISK", "MEDIUM")
    _, report = run(repo, "중간 위험 작업")
    job: dict[str, Any] = report["jobs"][0]
    assert job["status"] == "APPROVAL_REQUIRED"
    return job


def test_host_merge_does_not_trust_session_state() -> None:
    env = dispatch.host_merge_env(None, wait_s=60)
    assert env["FACTORY_UNTRUSTED_STATE"] == "1"
    assert "FACTORY_VERIFY_CMD" not in env


def test_session_info_is_kept_outside_the_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = waiting_job(repo, monkeypatch)
    info = dispatch.session_info(repo, job["job_id"])
    assert info["isolation"] == "host"
    assert not Path(dispatch.session_info_path(repo, job["job_id"])).is_relative_to(
        Path(job["worktree"])
    )


def test_approve_merges_with_trusted_code_and_reverification(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = waiting_job(repo, monkeypatch)
    job_id = job["job_id"]
    assert dispatch.main(["--approve", job_id], root=repo) == 0
    record = json.loads(Path(job["record"]).read_text(encoding="utf-8"))
    assert record["status"] == "MERGED"
    assert record["approval"]["by"] == "human"
    assert record["merge_checks"][-1]["why"] == "untrusted"
    assert git(repo, "rev-parse", "main") == record["merge_commit"]
    assert not Path(job["worktree"]).exists()


def test_reject_sends_job_back(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = waiting_job(repo, monkeypatch)
    job_id = job["job_id"]
    assert dispatch.main(["--reject", job_id, "--reason", "이름이 이상함"], root=repo) == 0
    state = json.loads((Path(job["worktree"]) / "factory/state.json").read_text(encoding="utf-8"))
    assert state["jobs"][job_id]["status"] == "RETRY_REQUIRED"
    assert state["jobs"][job_id]["approval"]["reason"] == "이름이 이상함"


def test_approve_unknown_job_fails(repo: Path) -> None:
    assert dispatch.main(["--approve", "J999"], root=repo) == 2


def test_requirements_are_required_without_approve(repo: Path) -> None:
    assert dispatch.main([], root=repo) == 2


# ---------- 오케스트레이터 모델·인증 (단계 설정) ----------


def set_stages(repo: Path, stages: dict[str, Any]) -> None:
    config = repo / "factory" / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["stages"] = stages
    config.write_text(json.dumps(data), encoding="utf-8")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "stages")


def test_orchestrator_model_auto_config_and_cli(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACTORY_MODEL_ORCHESTRATOR", raising=False)
    plan = dispatch.orchestrator_plan(repo, "claude", model=None, auth=None, docker=False)
    assert (plan["model"], plan["source"]) == ("claude-opus-5-5", "auto")
    set_stages(repo, {"orchestrator": {"model": "sonnet"}})
    plan = dispatch.orchestrator_plan(repo, "claude", model=None, auth=None, docker=False)
    assert (plan["model"], plan["source"]) == ("claude-sonnet-5-5", "config")
    plan = dispatch.orchestrator_plan(repo, "claude", model="haiku", auth=None, docker=False)
    assert (plan["model"], plan["source"]) == ("claude-haiku-5-5", "cli")
    with pytest.raises(dispatch.DispatchError):
        dispatch.orchestrator_plan(repo, "claude", model="claude-3-opus", auth=None, docker=False)


def test_orchestrator_auth_choices(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auto = dispatch.orchestrator_plan(repo, "claude", model=None, auth=None, docker=False)
    assert auto["auth"] == "subscription"
    with pytest.raises(dispatch.DispatchError):  # api인데 키 없음
        dispatch.orchestrator_plan(repo, "claude", model=None, auth="api", docker=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    assert dispatch.orchestrator_plan(repo, "claude", None, None, docker=False)["auth"] == "api"
    set_stages(repo, {"orchestrator": {"auth": "subscription"}})
    sub = dispatch.orchestrator_plan(repo, "claude", model=None, auth=None, docker=False)
    assert sub["auth"] == "subscription"
    # 컨테이너에는 구독 자격 증명을 넣지 않는다 (API 키 전용)
    with pytest.raises(dispatch.DispatchError):
        dispatch.orchestrator_plan(repo, "claude", model=None, auth=None, docker=True)


def test_subscription_session_hides_key_but_stashes_it_for_reviewers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    add, unset = dispatch.session_auth_env({"auth": "subscription"})
    assert "ANTHROPIC_API_KEY" in unset
    assert add == {"FACTORY_ANTHROPIC_API_KEY_STASH": "dummy-not-a-real-key"}
    assert dispatch.session_auth_env({"auth": "api"}) == ({}, ())
    assert "FACTORY_ANTHROPIC_API_KEY_STASH" in dispatch.HOST_ONLY_SETTINGS


def test_report_and_session_info_record_orchestrator(repo: Path) -> None:
    _, report = run(repo, "요구사항")
    assert report["orchestrator"]["source"] == "auto"
    assert report["orchestrator"]["auth"] in {"subscription", "api"}
    info = dispatch.session_info(repo, report["jobs"][0]["job_id"])
    assert info["orchestrator"]["model"] == report["orchestrator"]["model"]


def test_docker_refuses_subscription_reviewers(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    set_stages(repo, {"reviewer_security": {"auth": "subscription"}})
    with pytest.raises(dispatch.DispatchError):
        dispatch.check_container_stages(repo)
    set_stages(repo, {"reviewer_security": {"auth": "api"}})
    dispatch.check_container_stages(repo)


def test_job_cost_includes_reviewers(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, report = run(repo, "요구사항")
    cost = report["jobs"][0]["cost"]
    assert cost["session_usd"] == 0.5
    assert cost["reviewers_usd"] == 0.03
    assert cost["total_usd"] == 0.53
    out = capsys.readouterr().out
    assert "작업 비용($)" in out and "0.53" in out


def test_session_cap_comes_from_job_budgets(repo: Path) -> None:
    config = repo / "factory" / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["budgets"] = {"session": 3.0, "job_total": 4.5}
    config.write_text(json.dumps(data), encoding="utf-8")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "budgets")
    _, report = run(repo, "요구사항")
    job = report["jobs"][0]
    assert job["session_budget_usd"] == 3.0
    assert job["over_job_total"] is False
    _, report = run(repo, "--budget-usd", "2", "요구사항 2")
    assert report["jobs"][0]["session_budget_usd"] == 2.0
