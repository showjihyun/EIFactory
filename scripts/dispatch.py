"""여러 요구사항을 worktree별 headless 세션으로 동시에 처리한다 (기획안 Phase 3).

사용법:
    python scripts/dispatch.py [--max-parallel 3] [--wall-timeout 40] [--model M]
                               [--agent claude|codex] [--review-agent claude|codex]
                               [--isolation host|docker] [--require-api-key] [--keep-merged]
                               "<요구사항>" ...

요구사항마다:
1. 기본 checkout의 base 브랜치 끝에서 worktree를 만들고 `factory.py new --base <base>`로
   작업을 만든다 (상태는 worktree마다 따로). worktree를 작업 ID 폴더로 옮긴 뒤 의존성을 설치한다.
2. headless 세션(`claude -p` 또는 `codex exec`)으로 factory 스킬을 실행한다. 실행 래퍼가 벽시계
   시간 상한을 건다 (7.6). 세션에는 FACTORY_AGENT_SESSION=1을 넣어 사람 전용 명령을 막는다.
3. 끝나면 상태를 읽는다. MERGED면 worktree를 지우고, 그 밖의 상태는 사람이 보도록 남긴다.

격리 (`--isolation`):
- host: 세션이 호스트에서 돈다. 머지는 각 세션의 `factory.py merge`가 한다.
- docker (Phase 3b): 세션이 컨테이너 안에서 돈다. 컨테이너는 내부 전용 네트워크에 있고 프록시를
  거쳐 api.anthropic.com에만 나갈 수 있다. 공유 `.git`은 읽기 전용으로만 보인다. 컨테이너는
  base 브랜치를 건드리지 않고, 세션이 끝나면 호스트가 머지한다. 머지 전 재검증도 컨테이너에서
  돌려, 호스트는 에이전트가 쓴 코드를 실행하지 않는다. 인증은 API 키만 쓴다.

에이전트 도구 (`--agent`, Phase 3c): claude는 Claude Code, codex는 Codex CLI. 정책·절차·상태·검증은
같고 래퍼만 다르다 (Claude Code `.claude/`, Codex `.codex/hooks.json`·`.agents/skills/`).
리뷰어 도구는 `--review-agent`로 따로 고를 수 있다 (기본: 세션과 같은 도구).

인증: claude는 ANTHROPIC_API_KEY가 있으면 API 키, 없으면 로그인 세션(구독). codex는 CODEX_API_KEY
또는 OPENAI_API_KEY, 없으면 `codex login` 상태. 컨테이너는 API 키만 쓴다. 키 값은
어디에도 출력하거나 기록하지 않는다 (컨테이너에는 변수 이름만 넘긴다).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proc import resolve_command, use_utf8_output  # noqa: E402

BASE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Skill", "Agent", "Task"]
# 오케스트레이터 모델 (기획안 6.6). Builder·리뷰어 모델은 factory.py의 MODELS가 정한다
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6-astra"}
API_KEYS = {"claude": ["ANTHROPIC_API_KEY"], "codex": ["CODEX_API_KEY", "OPENAI_API_KEY"]}
DEFAULT_WALL_TIMEOUT_MIN = 40.0  # 중형 상한 30분 + 정리 여유
SETUP_STEPS = [
    ("backend", ["uv", "sync", "-q"]),
    ("frontend", ["npm", "ci", "--no-audit", "--no-fund", "--loglevel=error"]),
]
ACTIVE = {"QUEUED", "SPEC_READY", "BUILDING", "RETRY_REQUIRED", "VERIFYING", "APPROVED"}

# 컨테이너 격리 (Phase 3b)
INTERNAL_NETWORK = "factory-internal"
PROXY_NAME = "factory-proxy"
PROXY_URL = f"http://{PROXY_NAME}:3128"
CONTAINER_WORKDIR = "/workspaces/bookmark-factory"  # 이미지가 node_modules 소유자를 맞춰 둔 경로
CONTAINER_GIT = "/repo-git"
# 컨테이너에 이름으로 넘기지 않는 dispatcher 전용 설정
HOST_ONLY_SETTINGS = {
    "FACTORY_AGENT_CMD",
    "FACTORY_SETUP_CMD",
    "FACTORY_DOCKER_MOUNTS",
    "FACTORY_ROOT",
    "FACTORY_AGENT_SESSION",
    "FACTORY_DOCKER_NO_PROXY",
}

PROMPT = (
    "factory 스킬로 배정된 작업 {job_id}를 처리해. 요구사항: {requirement}\n"
    "작업은 dispatcher가 이미 `new`로 만들어 QUEUED 상태다. recover 없이 "
    "factory/PROCESS.md 3번(spec·수락 테스트 작성)부터 진행해."
)
CODEX_PROMPT = (
    "`.agents/skills/factory/SKILL.md`의 factory 스킬을 읽고, 그 절차로 배정된 작업 {job_id}를 "
    "처리해. 요구사항: {requirement}\n"
    "작업은 dispatcher가 이미 `new`로 만들어 QUEUED 상태다. recover 없이 "
    "factory/PROCESS.md 3번(spec·수락 테스트 작성)부터 진행해."
)

_git_lock = threading.Lock()  # worktree add/move/remove는 Git 공통 디렉터리를 바꾸므로 한 번에 하나


class DispatchError(Exception):
    pass


def _run(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        resolve_command(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )


def _git(root: Path, *args: str) -> str:
    proc = _run(["git", *args], root)
    if proc.returncode != 0:
        raise DispatchError(f"git {' '.join(args)} 실패: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _factory(
    root: Path, worktree: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """기본 checkout의 factory.py를 작업 worktree에 겨눠 실행한다.

    작업 worktree의 scripts/는 에이전트가 바꿀 수 있으므로 호스트에서는 실행하지 않는다.
    """
    full_env = {**os.environ, **(env or {}), "FACTORY_ROOT": str(worktree)}
    return _run([sys.executable, str(root / "scripts/factory.py"), *args], worktree, full_env)


def auth_mode(agent: str = "claude") -> str:
    if any(os.environ.get(name) for name in API_KEYS[agent]):
        return "API 키 (환경변수)"
    return "로그인 세션 (구독)" if agent == "claude" else "로그인 세션 (codex login)"


def api_key_names(agents: list[str]) -> list[str]:
    """도구마다 설정된 API 키 변수 이름 하나씩. 없으면 DispatchError (값은 읽지 않는다)."""
    names: list[str] = []
    for agent in dict.fromkeys(agents):
        found = next((n for n in API_KEYS[agent] if os.environ.get(n)), None)
        if found is None:
            raise DispatchError(
                f"{agent}용 API 키 환경변수가 없습니다 ({' 또는 '.join(API_KEYS[agent])})"
            )
        names.append(found)
    return names


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


def codex_extra_args() -> list[str]:
    """codex exec에 덧붙일 인자 (예: 모델 제공자 지정). factory.py의 리뷰어도 같은 값을 쓴다."""
    return [str(a) for a in json.loads(os.environ.get("FACTORY_CODEX_ARGS", "[]"))]


def agent_command(
    root: Path, prompt: str, model: str, agent: str = "claude", workdir: str | None = None
) -> list[str]:
    """오케스트레이터 세션 명령. workdir는 세션이 보는 레포 경로 (컨테이너면 컨테이너 경로)."""
    override = os.environ.get("FACTORY_AGENT_CMD")
    if override:
        return [*json.loads(override), prompt]
    if agent == "codex":
        where = workdir or str(root)
        # 컨테이너는 그 자체가 격리 환경이다. 호스트에서는 Codex 샌드박스(작업 폴더 쓰기만)를 쓴다.
        # 공유 .git(hooks, config 포함)은 쓰기 대상에 넣지 않는다. 쓸 수 있으면 hook을 심어
        # 호스트에서 코드를 실행할 수 있다. 머지는 세션이 끝난 뒤 호스트가 한다 (merges_on_host)
        sandbox = (
            ["--dangerously-bypass-approvals-and-sandbox"]
            if workdir
            else ["--sandbox", "workspace-write"]
        )
        return [
            "codex",
            "exec",
            "--model",
            model,
            "--json",
            "--color",
            "never",
            "-C",
            where,
            *sandbox,
            # 프로젝트 hook(.codex/hooks.json)은 신뢰된 프로젝트에서만 읽힌다. hook은 잠긴 파일이라
            # 작업이 바꿀 수 없으므로 hook 신뢰 확인을 건너뛴다 (확인이 없으면 hook 없이 돈다)
            "-c",
            f"projects.'{where}'.trust_level=\"trusted\"",
            "--dangerously-bypass-hook-trust",
            "-c",
            "check_for_update_on_startup=false",
            *codex_extra_args(),
            prompt,
        ]
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


def merges_on_host(agent: str, docker: bool) -> bool:
    """세션이 공유 .git에 쓸 수 없어 머지를 호스트가 하는가 (컨테이너 세션, Codex 샌드박스 세션)."""
    return docker or agent == "codex"


def setup_steps() -> list[tuple[str, list[str]]]:
    override = os.environ.get("FACTORY_SETUP_CMD")
    if override is not None:
        return [(str(area), list(cmd)) for area, cmd in json.loads(override)]
    return SETUP_STEPS


# ---------------------------------------------------------------- 컨테이너 (Phase 3b)


@dataclass(frozen=True)
class DockerJob:
    root: Path  # 기본 checkout
    worktree: Path
    job_id: str
    stamp: str
    image: str

    @property
    def name(self) -> str:
        return f"fac-{self.stamp}-{self.job_id}".lower()

    @property
    def gitdir_name(self) -> str:
        """`.git/worktrees/<이름>`. worktree를 옮겨도 처음 이름이 그대로 남는다."""
        text = (self.worktree / ".git").read_text(encoding="utf-8").strip()
        return text.removeprefix("gitdir:").strip().replace("\\", "/").rstrip("/").split("/")[-1]

    @property
    def dotgit(self) -> Path:
        return self.root.parent / f"{self.root.name}.jobs" / "meta" / self.job_id / "dotgit"


def _write_dotgit(job: DockerJob) -> None:
    job.dotgit.parent.mkdir(parents=True, exist_ok=True)
    job.dotgit.write_text(
        f"gitdir: {CONTAINER_GIT}/worktrees/{job.gitdir_name}\n", encoding="utf-8"
    )


def _extra_mounts() -> list[str]:
    """시험용 읽기 전용 마운트 (FACTORY_DOCKER_MOUNTS = [[호스트 경로, 컨테이너 경로], ...])."""
    args: list[str] = []
    for source, target in json.loads(os.environ.get("FACTORY_DOCKER_MOUNTS", "[]")):
        args += ["--mount", f"type=bind,source={source},target={target},readonly"]
    return args


def _docker_base(job: DockerJob, network: str) -> list[str]:
    _write_dotgit(job)
    common_git = job.root / ".git"
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        "--mount",
        f"type=bind,source={job.worktree},target={CONTAINER_WORKDIR}",
        # worktree의 .git 파일은 호스트 경로를 가리키므로 컨테이너 경로로 바꿔 끼운다
        "--mount",
        f"type=bind,source={job.dotgit},target={CONTAINER_WORKDIR}/.git,readonly",
        # 공유 .git(브랜치, 설정, hooks)은 읽기 전용. 커밋·머지는 호스트만 한다
        "--mount",
        f"type=bind,source={common_git},target={CONTAINER_GIT},readonly",
        # 리눅스용 의존성은 작업별 볼륨에 둔다 (호스트의 Windows용 설치와 섞이지 않게)
        "--mount",
        f"type=volume,source={job.name}-node,target={CONTAINER_WORKDIR}/frontend/node_modules",
        "--mount",
        f"type=volume,source={job.name}-venv,target=/home/node/.venvs",
        "-e",
        "GIT_CONFIG_COUNT=1",
        "-e",
        "GIT_CONFIG_KEY_0=safe.directory",
        "-e",
        "GIT_CONFIG_VALUE_0=*",
        *_extra_mounts(),
    ]


def _extra_no_proxy() -> list[str]:
    return [h for h in os.environ.get("FACTORY_DOCKER_NO_PROXY", "").split(",") if h]


def docker_setup_command(job: DockerJob) -> list[str]:
    """의존성 설치. 패키지 저장소가 필요하므로 일반 네트워크, 키는 넘기지 않는다."""
    script = (
        "cd backend && uv sync -q && cd ../frontend && npm ci --no-audit --no-fund --loglevel=error"
    )
    return [*_docker_base(job, "bridge"), job.image, "bash", "-c", script]


def docker_session_command(
    job: DockerJob, inner: list[str], keys: list[str] | None = None
) -> list[str]:
    """에이전트 세션 컨테이너. keys는 넘길 API 키 변수 이름 (값은 넘기지 않는다)."""
    settings = sorted(
        name
        for name in os.environ
        if name.startswith("FACTORY_") and name not in HOST_ONLY_SETTINGS
    )
    env = [
        f"HTTPS_PROXY={PROXY_URL}",
        f"HTTP_PROXY={PROXY_URL}",
        # 시험용 내부 서버(가짜 모델 서버 등)는 FACTORY_DOCKER_NO_PROXY로 프록시를 거치지 않게 한다
        "NO_PROXY=" + ",".join(["localhost", "127.0.0.1", *_extra_no_proxy()]),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "FACTORY_MERGE_ON_HOST=1",
        "FACTORY_AGENT_SESSION=1",
    ]
    if keys is None:
        keys = ["ANTHROPIC_API_KEY"] if os.environ.get("ANTHROPIC_API_KEY") else []
    names = keys + settings
    return [
        *_docker_base(job, INTERNAL_NETWORK),
        "--name",
        job.name,
        *[a for e in env for a in ("-e", e)],
        *[a for n in names for a in ("-e", n)],
        job.image,
        *inner,
    ]


def docker_tool_command(job: DockerJob, inner: list[str]) -> list[str]:
    """호스트 머지 중의 재검증. 외부 네트워크도 키도 없는 컨테이너에서 돌린다."""
    return [*_docker_base(job, INTERNAL_NETWORK), job.image, *inner]


def _content_tag(name: str, directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return f"{name}:{digest.hexdigest()[:12]}"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["docker", *args], Path.cwd())


def prepare_docker(root: Path) -> str:
    """이미지·내부 네트워크·프록시를 준비하고 에이전트 이미지 태그를 돌려준다."""
    if _docker("info", "--format", "{{.OSType}}").returncode != 0:
        raise DispatchError("Docker를 쓸 수 없습니다 (docker info 실패)")
    images = [
        (_content_tag("bookmark-factory-agent", root / ".devcontainer"), root / ".devcontainer"),
        (_content_tag("bookmark-factory-proxy", root / "docker/proxy"), root / "docker/proxy"),
    ]
    for tag, context in images:
        if _docker("image", "inspect", tag).returncode != 0:
            built = _docker("build", "-q", "-t", tag, str(context))
            if built.returncode != 0:
                raise DispatchError(f"이미지 빌드 실패 {tag}: {built.stderr[-500:]}")
    if _docker("network", "inspect", INTERNAL_NETWORK).returncode != 0:
        _docker("network", "create", "--internal", INTERNAL_NETWORK)
    proxy_tag = images[1][0]
    running = _docker("inspect", "-f", "{{.State.Running}} {{.Config.Image}}", PROXY_NAME)
    if running.stdout.strip() != f"true {proxy_tag}":
        _docker("rm", "-f", PROXY_NAME)
        started = _docker("run", "-d", "--name", PROXY_NAME, "--network", "bridge", proxy_tag)
        if started.returncode != 0:
            raise DispatchError(f"프록시 시작 실패: {started.stderr[-500:]}")
        _docker("network", "connect", INTERNAL_NETWORK, PROXY_NAME)
    return images[0][0]


# ---------------------------------------------------------------- 작업 실행


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
    root: Path,
    base: str,
    requirement: str,
    index: int,
    args: argparse.Namespace,
    stamp: str,
    image: str | None = None,
) -> dict[str, Any]:
    jobs_dir = root.parent / f"{root.name}.jobs"
    logs = jobs_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    slot = jobs_dir / f"slot-{stamp}-{index}"
    result: dict[str, Any] = {"requirement": requirement, "job_id": None, "status": "SETUP_FAILED"}
    started = time.time()
    with _git_lock:
        _git(root, "worktree", "add", "-q", "--detach", str(slot), base)
    created = _factory(root, slot, "new", requirement, "--base", base, env=_agent_env(args))
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
    docker = DockerJob(root, worktree, job_id, stamp, image) if image else None

    # 가상환경은 절대 경로를 품으므로 worktree를 옮긴 뒤에 설치한다
    if docker:
        done = _run(docker_setup_command(docker), worktree)
        if done.returncode != 0:
            result["error"] = f"컨테이너 설치 실패: {(done.stdout + done.stderr)[-500:]}"
            return result
    else:
        for area, cmd in setup_steps():
            done = _run(cmd, worktree / area)
            if done.returncode != 0:
                result["error"] = f"{area} 설치 실패: {(done.stdout + done.stderr)[-500:]}"
                return result

    template = CODEX_PROMPT if args.agent == "codex" else PROMPT
    prompt = template.format(job_id=job_id, requirement=requirement)
    workdir = CONTAINER_WORKDIR if docker else None
    inner = agent_command(worktree, prompt, args.model, agent=args.agent, workdir=workdir)
    command = docker_session_command(docker, inner, keys=args.keys) if docker else inner
    host_merge = merges_on_host(args.agent, docker is not None)
    session_env = {**os.environ, **_agent_env(args), "FACTORY_AGENT_SESSION": "1"}
    if host_merge:
        session_env["FACTORY_MERGE_ON_HOST"] = "1"
    log_path = logs / f"{job_id}.jsonl"
    with open(log_path, "wb") as out, open(logs / f"{job_id}.stderr", "wb") as err:
        proc = subprocess.Popen(
            resolve_command(command),
            cwd=worktree,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            env=session_env,
            start_new_session=sys.platform != "win32",
        )
        try:
            result["exit_code"] = proc.wait(timeout=args.wall_timeout * 60)
            result["timed_out"] = False
        except subprocess.TimeoutExpired:
            if docker:
                _docker("kill", docker.name)
            _kill_tree(proc)
            result["exit_code"] = None
            result["timed_out"] = True

    job = _load_job(worktree, job_id)
    if result["timed_out"] and job["status"] in ACTIVE - {"APPROVED"}:
        detail = f"실행 래퍼 벽시계 상한 {args.wall_timeout}분 초과로 세션을 종료함"
        _factory(root, worktree, "block", job_id, "--reason", "TIMEOUT", "--detail", detail)
        job = _load_job(worktree, job_id)
    if host_merge and job["status"] == "APPROVED":
        # 세션은 머지하지 않는다. 호스트가 기본 checkout의 factory.py로 머지한다.
        # 컨테이너 세션이면 재검증도 컨테이너에서 돌려 호스트가 에이전트 코드를 실행하지 않는다
        env: dict[str, str] = {}
        if docker:
            tools = {
                "FACTORY_VERIFY_CMD": docker_tool_command(docker, ["python", "scripts/verify.py"]),
                "FACTORY_CHECK_RED_CMD": docker_tool_command(
                    docker, ["python", "scripts/check_red.py"]
                ),
            }
            env = {k: json.dumps(v) for k, v in tools.items()}
        # 앞 작업의 컨테이너 재검증이 수 분 걸리므로 lock을 세션 상한만큼 기다린다
        env["FACTORY_MERGE_WAIT_S"] = str(int(args.wall_timeout * 60))
        merged = _factory(root, worktree, "merge", job_id, env=env)
        result["host_merge"] = merged.stdout.strip()[-500:]
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
    if docker:
        _docker("volume", "rm", f"{docker.name}-node", f"{docker.name}-venv")
    if job["status"] == "MERGED" and not args.keep_merged:
        result["worktree_removed"] = _remove_worktree(root, worktree, result)
    return result


def _force_remove(path: Path) -> bool:
    """Windows 경로 길이 제한(MAX_PATH)을 넘는 파일, 읽기 전용 파일(Git objects)까지 지운다."""
    target = str(path.resolve())
    if sys.platform == "win32" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + target

    def retry_writable(func: Any, failed: str, _error: BaseException) -> None:
        try:
            os.chmod(failed, stat.S_IWRITE)
            func(failed)
        except OSError:
            pass

    shutil.rmtree(target, onexc=retry_writable)
    return not path.exists()


def _remove_worktree(root: Path, worktree: Path, result: dict[str, Any]) -> bool:
    """머지된 worktree를 지운다. 실패해도 작업 결과는 그대로 두고 사유만 남긴다.

    컨테이너 verify가 남긴 임시 폴더(테스트용 Git 저장소)는 경로가 깊어 Windows에서 지우지
    못할 수 있다 (Codex e2e에서 발견).
    """
    with _git_lock:
        try:
            _git(root, "-c", "core.longpaths=true", "worktree", "remove", "--force", str(worktree))
            return True
        except DispatchError as error:
            if _force_remove(worktree):
                _git(root, "worktree", "prune")
                return True
            result["cleanup_error"] = str(error)[-500:]
            return False


def _windows_host() -> bool:
    return sys.platform == "win32"


def _agent_env(args: argparse.Namespace) -> dict[str, str]:
    """factory.py가 작업에 기록하고 리뷰어를 고를 때 쓰는 도구 설정."""
    return {"FACTORY_AGENT": args.agent, "FACTORY_REVIEW_AGENT": args.review_agent}


def _summary(report: dict[str, Any]) -> str:
    lines = [
        f"도구: {report['agent']} (리뷰어 {report['review_agent']}), "
        f"인증: {report['auth']}, 격리 {report['isolation']}, "
        f"동시 실행 {report['max_parallel']}개, base {report['base']}",
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
    parser.add_argument("--agent", choices=sorted(DEFAULT_MODELS), default="claude")
    parser.add_argument("--review-agent", choices=sorted(DEFAULT_MODELS), help="기본: --agent")
    parser.add_argument("--model", help="오케스트레이터 모델 (기본: 도구별 DEFAULT_MODELS)")
    parser.add_argument("--isolation", choices=["host", "docker"], default="host")
    parser.add_argument("--require-api-key", action="store_true")
    parser.add_argument("--keep-merged", action="store_true", help="머지된 worktree도 남김")
    args = parser.parse_args(argv)
    args.review_agent = args.review_agent or args.agent
    args.model = args.model or DEFAULT_MODELS[args.agent]
    args.keys = None

    image: str | None = None
    try:
        needs_key = args.require_api_key or (
            args.isolation == "docker" and not os.environ.get("FACTORY_AGENT_CMD")
        )
        if needs_key:
            # 세션 도구와 리뷰어 도구의 키를 모두 확인한다 (리뷰어는 세션 안에서 돈다)
            args.keys = api_key_names([args.agent, args.review_agent])
        base = _git(root, "branch", "--show-current")
        if not base:
            raise DispatchError("기본 checkout이 브랜치 위에 있어야 합니다 (머지 대상)")
        if _git(root, "status", "--porcelain", "--untracked-files=no"):
            raise DispatchError(
                "기본 checkout에 커밋되지 않은 변경이 있습니다 (머지가 이곳에서 일어남)"
            )
        if (
            args.agent == "codex"
            and args.isolation == "host"
            and _windows_host()
            and not os.environ.get("FACTORY_AGENT_CMD")
        ):
            raise DispatchError(
                "Windows 호스트에서는 Codex 샌드박스가 읽기 전용으로 떨어져 명령·패치를 실행하지 "
                "못합니다 (codex 0.163 실측). --isolation docker를 쓰세요"
            )
        if args.isolation == "docker":
            image = prepare_docker(root)
    except DispatchError as error:
        print(f"오류: {error}")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    print(
        f"도구: {args.agent}, 인증: {auth_mode(args.agent)}, 격리 {args.isolation}, "
        f"작업 {len(args.requirements)}개, 동시 {args.max_parallel}개"
    )
    with ThreadPoolExecutor(max_workers=max(1, args.max_parallel)) as pool:
        futures = [
            pool.submit(run_job, root, base, req, i, args, stamp, image)
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
        "agent": args.agent,
        "review_agent": args.review_agent,
        "auth": auth_mode(args.agent),
        "isolation": args.isolation,
        "image": image,
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
