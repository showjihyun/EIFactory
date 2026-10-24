"""여러 요구사항을 worktree별 headless 세션으로 동시에 처리한다 (기획안 Phase 3).

사용법:
    python scripts/dispatch.py [--max-parallel 3] [--wall-timeout 40] [--model M]
                               [--agent claude|codex] [--review-agent claude|codex]
                               [--isolation host|docker] [--require-api-key] [--keep-merged]
                               "<요구사항>" ...
    python scripts/dispatch.py --approve <작업 ID>
    python scripts/dispatch.py --reject <작업 ID> --reason "<이유>"
    python scripts/dispatch.py --plan "<목표>"          # 계획 초안 + 검사 (기획안 6.9 P1)
    python scripts/dispatch.py --run-plan <계획 ID>     # 사람 전용, 실행 = 계획 승인

요구사항마다:
1. 기본 checkout의 base 브랜치 끝에서 worktree를 만들고 `factory.py new --base <base>`로
   작업을 만든다 (상태는 worktree마다 따로). worktree를 작업 ID 폴더로 옮긴 뒤 의존성을 설치한다.
2. headless 세션(`claude -p` 또는 `codex exec`)으로 factory 스킬을 실행한다. 실행 래퍼가 벽시계
   시간 상한을 건다 (7.6). 세션에는 FACTORY_AGENT_SESSION=1을 넣어 사람 전용 명령을 막는다.
3. 끝나면 상태를 읽는다. rebase 충돌로 RETRY_REQUIRED면 같은 worktree에서 세션을 이어서 띄워
   Builder가 새 base 위에서 충돌을 풀게 한다 (최대 MAX_CONTINUE회, `--approve` 뒤에도 같다).
   MERGED면 worktree를 지우고, 그 밖의 상태는 사람이 보도록 남긴다.

사람 승인 (`--approve`, `--reject`): 승인 대기 작업을 기본 checkout의 factory.py로 처리한다.
작업 worktree의 scripts/와 상태 파일은 세션이 바꿀 수 있으므로 그 worktree의 factory.py를 쓰지
않는다. 승인하면 상태의 검증 기록을 믿지 않고(FACTORY_UNTRUSTED_STATE) 다시 검증한 뒤 머지하며,
컨테이너 작업이면 재검증도 오프라인 컨테이너에서 돌린다.

계획 실행 (`--run-plan`): `plan.py check`를 통과한 계획을 depends_on 순서로 돌린다. 경로가 겹치지
않는 독립 작업은 동시에, 승인 대기에 기대는 작업만 기다린다. BLOCKED에 기대는 작업이 있으면 계획은
STOPPED. 다시 실행하면 남은 작업만 한다. 검사 뒤 계획 문서가 바뀌었으면 거부한다.

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
import contextlib
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
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plan  # noqa: E402
from factory_config import CLAUDE_MODELS, DEFAULT_WORKDIR, ConfigError, load_config  # noqa: E402
from proc import resolve_command, use_utf8_output  # noqa: E402
from report import combine_usage, job_cost, live_usage, session_usage, unit_prices  # noqa: E402

BASE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Skill", "Agent", "Task"]
# 오케스트레이터 모델 (기획안 6.6). Builder·리뷰어 모델은 factory.py의 MODELS가 정한다
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6-astra"}
API_KEYS = {"claude": ["ANTHROPIC_API_KEY"], "codex": ["CODEX_API_KEY", "OPENAI_API_KEY"]}
DEFAULT_WALL_TIMEOUT_MIN = 40.0  # 중형 상한 30분 + 정리 여유
# 세션 하나의 비용 상한 (API 환산, 임시값). 규모는 spec에서 정해지므로 중형 기준 하나로 건다.
# Phase 1~3 실제 세션 31개의 최대 $1.21 (리뷰어 제외)의 약 4배. Claude만 적용 (`--max-budget-usd`)
DEFAULT_BUDGET_USD = 5.0
ACTIVE = {"QUEUED", "SPEC_READY", "BUILDING", "RETRY_REQUIRED", "VERIFYING", "APPROVED"}

# 컨테이너 격리 (Phase 3b)
INTERNAL_NETWORK = "factory-internal"
PROXY_NAME = "factory-proxy"
PROXY_URL = f"http://{PROXY_NAME}:3128"
# 컨테이너 안 레포 경로·설치 명령·캐시 볼륨은 factory/config.json의 docker (아래는 설정이 없을 때)
CONTAINER_WORKDIR = DEFAULT_WORKDIR
DEFAULT_SETUP_SCRIPT = (
    "cd backend && uv sync -q && cd ../frontend && npm ci --no-audit --no-fund --loglevel=error"
)
DEFAULT_CACHE_VOLUMES = {"frontend/node_modules": "node", "/home/node/.venvs": "venv"}
CONTAINER_GIT = "/repo-git"
# 얇은 설치(기획안 12.1): 레포 밖 엔진을 컨테이너에 읽기 전용으로 끼운다
ENGINE = Path(__file__).resolve().parents[1]
CONTAINER_ENGINE_HOME = "/opt/factory/engines"
# 컨테이너에 이름으로 넘기지 않는 dispatcher 전용 설정
HOST_ONLY_SETTINGS = {
    "FACTORY_AGENT_CMD",
    "FACTORY_SETUP_CMD",
    "FACTORY_DOCKER_MOUNTS",
    "FACTORY_ROOT",
    "FACTORY_REPO",
    "FACTORY_ENGINE_HOME",
    "FACTORY_AGENT_SESSION",
    "FACTORY_DOCKER_NO_PROXY",
    "FACTORY_ANTHROPIC_API_KEY_STASH",
}
# 오케스트레이터가 구독으로 돌 때 API 키를 숨겨 두는 이름 (리뷰어 중 API를 쓰는 것만 되돌려 쓴다)
API_KEY_STASH = "FACTORY_ANTHROPIC_API_KEY_STASH"

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
# rebase 충돌로 RETRY_REQUIRED가 된 작업을 이어서 처리하는 세션 (기획안 8.3)
CONTINUE_TASK = (
    "작업 {job_id}는 base에 먼저 머지된 변경과 충돌해 RETRY_REQUIRED 상태다. factory.py가 "
    "작업 변경을 새 base 끝 위에 다시 펼쳐 두었다. 충돌 표시(<<<<<<<)가 남은 파일: {files}. "
    "recover 없이 factory/PROCESS.md 6번(Builder 호출)부터 진행해. "
    "Builder에게 이 파일들과 '두 변경을 모두 살려 충돌을 푼다'는 점을 전달해."
)
# 작업이 바꾼 기존 테스트가 충돌해 Spec 단계부터 다시 하는 경우 (잠긴 테스트는 Builder가 못 고침)
CONTINUE_SPEC_TASK = (
    "작업 {job_id}는 이 작업이 바꾼 기존 테스트({files})가 base에 먼저 머지된 변경과 충돌해 "
    "SPEC_READY로 돌아왔다. factory.py가 새 base 끝 위에 spec·수락 테스트만 남기고 구현과 기존 "
    "테스트 변경은 뺐다. recover 없이 factory/PROCESS.md 4번부터 진행해: 기존 테스트를 새 base "
    "기준으로 다시 바꾸고(이전에 바꾼 내용: {diffs}) spec → red → Builder 순서로."
)
CONTINUE_HEAD = {
    "claude": "factory 스킬로 작업 {job_id}를 이어서 처리해. 요구사항: {requirement}\n",
    "codex": "`.agents/skills/factory/SKILL.md`의 factory 스킬을 읽고, 그 절차로 작업 {job_id}를 "
    "이어서 처리해. 요구사항: {requirement}\n",
}
# 충돌을 풀러 세션을 다시 띄우는 최대 횟수. 같은 파일이 다시 충돌하면 factory.py가 BLOCKED로 막는다
MAX_CONTINUE = 2

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


def auth_label(plan: dict[str, str]) -> str:
    return {"api": "API 키 (환경변수)", "subscription": "로그인 세션 (구독)",
            "login": "로그인 세션 (codex login)"}.get(plan["auth"], plan["auth"])  # fmt: skip


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


def orchestrator_plan(
    root: Path, agent: str, model: str | None, auth: str | None, docker: bool
) -> dict[str, str]:
    """오케스트레이터 세션의 모델·인증 (기획안 6.6).

    모델: --model > FACTORY_MODEL_ORCHESTRATOR > 설정 stages.orchestrator > 자동(Opus).
    인증: --auth > 설정 > 자동(ANTHROPIC_API_KEY가 있으면 API, 없으면 구독).
    컨테이너는 API 키만 쓴다 (구독 자격 증명을 넣지 않는다, 기획안 9장).
    """
    if agent != "claude":
        has_key = any(os.environ.get(n) for n in API_KEYS[agent])
        return {"model": model or DEFAULT_MODELS[agent], "alias": model or DEFAULT_MODELS[agent],
                "source": "cli" if model else "auto", "reason": f"{agent} 기본",
                "auth": "api" if has_key else "login"}  # fmt: skip
    stage = load_config(root).stages["orchestrator"]
    env_model = os.environ.get("FACTORY_MODEL_ORCHESTRATOR")
    if model:
        alias, source, reason = model, "cli", "--model로 지정"
    elif env_model:
        alias, source, reason = env_model, "env", "환경변수로 지정"
    elif stage["model"] != "auto":
        alias, source, reason = stage["model"], "config", "설정으로 지정"
    else:
        alias, source, reason = "opus", "auto", "오케스트레이터 기본"
    if alias not in CLAUDE_MODELS:
        raise DispatchError(
            f"모델 {alias}: 가능한 모델은 {', '.join(CLAUDE_MODELS)} (최신 세 가지)"
        )
    setting = auth or stage["auth"]
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    mode = ("api" if has_key else "subscription") if setting == "auto" else setting
    if mode == "api" and not has_key:
        raise DispatchError("오케스트레이터 인증이 api인데 ANTHROPIC_API_KEY가 없습니다")
    if mode == "subscription" and docker:
        raise DispatchError("컨테이너 격리는 API 키만 씁니다 (오케스트레이터 인증을 api로)")
    return {"model": CLAUDE_MODELS[alias], "alias": alias, "source": source, "reason": reason,
            "auth": mode}  # fmt: skip


def check_container_stages(root: Path) -> None:
    """컨테이너에는 구독 로그인이 없으므로 리뷰어 인증을 subscription으로 둘 수 없다."""
    stages = load_config(root).stages
    bad = [name for name, item in stages.items() if item["auth"] == "subscription"]
    if bad:
        raise DispatchError(
            f"컨테이너 격리는 API 키만 씁니다: {', '.join(bad)}의 auth를 api나 auto로"
        )


def session_auth_env(plan: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """세션에 넣을 환경변수와 지울 환경변수.

    구독이면 claude가 키를 쓰지 않도록 ANTHROPIC_API_KEY를 지우고, 키가 있으면 다른 이름으로
    넘겨 리뷰어 중 api로 정한 것만 쓰게 한다 (factory.stage_auth). 키 값은 출력·기록하지 않는다.
    """
    if plan.get("auth") != "subscription":
        return {}, ()
    key = os.environ.get("ANTHROPIC_API_KEY")
    return ({API_KEY_STASH: key} if key else {}), ("ANTHROPIC_API_KEY",)


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
    root: Path,
    prompt: str,
    model: str,
    agent: str = "claude",
    workdir: str | None = None,
    budget_usd: float | None = None,
) -> list[str]:
    """오케스트레이터 세션 명령. workdir는 세션이 보는 레포 경로 (컨테이너면 컨테이너 경로).

    budget_usd는 Claude 세션의 비용 상한이다. Codex CLI에는 같은 옵션이 없어 벽시계 상한만 건다.
    """
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
    budget = ["--max-budget-usd", str(budget_usd)] if budget_usd is not None else []
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
        *budget,
        "--allowedTools",
        *allowed_tools(root),
    ]


def merges_on_host(agent: str, docker: bool) -> bool:
    """세션이 공유 .git에 쓸 수 없어 머지를 호스트가 하는가 (컨테이너 세션, Codex 샌드박스 세션)."""
    return docker or agent == "codex"


def setup_steps(root: Path) -> list[tuple[str, list[str]]]:
    """의존성 설치 단계 (영역 폴더, 명령). factory/config.json의 setup."""
    override = os.environ.get("FACTORY_SETUP_CMD")
    if override is not None:
        return [(str(area), list(cmd)) for area, cmd in json.loads(override)]
    return load_config(root).setup


# ---------------------------------------------------------------- 컨테이너 (Phase 3b)


@dataclass(frozen=True)
class DockerJob:
    root: Path  # 기본 checkout
    worktree: Path
    job_id: str
    stamp: str
    image: str
    workdir: str = CONTAINER_WORKDIR
    setup_script: str = DEFAULT_SETUP_SCRIPT
    cache_volumes: tuple[tuple[str, str], ...] = tuple(DEFAULT_CACHE_VOLUMES.items())

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


def _docker_job(root: Path, worktree: Path, job_id: str, stamp: str, image: str) -> DockerJob:
    cfg = load_config(root)
    return DockerJob(
        root,
        worktree,
        job_id,
        stamp,
        image,
        workdir=cfg.docker_workdir,
        setup_script=cfg.docker_setup_script or DEFAULT_SETUP_SCRIPT,
        cache_volumes=tuple((cfg.docker_cache_volumes or DEFAULT_CACHE_VOLUMES).items()),
    )


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


def _engine_mount(root: Path) -> list[str]:
    """얇은 설치 레포면 엔진 폴더를 읽기 전용으로 끼운다. 세션은 엔진을 바꿀 수 없다."""
    try:
        tag = json.loads((root / "factory/kit.lock").read_text(encoding="utf-8"))["engine"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return []
    target = f"{CONTAINER_ENGINE_HOME}/{tag}"
    return [
        "--mount",
        f"type=bind,source={ENGINE},target={target},readonly",
        "-e",
        f"FACTORY_ENGINE_HOME={CONTAINER_ENGINE_HOME}",
    ]


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
        f"type=bind,source={job.worktree},target={job.workdir}",
        # worktree의 .git 파일은 호스트 경로를 가리키므로 컨테이너 경로로 바꿔 끼운다
        "--mount",
        f"type=bind,source={job.dotgit},target={job.workdir}/.git,readonly",
        # 공유 .git(브랜치, 설정, hooks)은 읽기 전용. 커밋·머지는 호스트만 한다
        "--mount",
        f"type=bind,source={common_git},target={CONTAINER_GIT},readonly",
        # 리눅스용 의존성은 작업별 볼륨에 둔다 (호스트의 Windows용 설치와 섞이지 않게)
        *[
            arg
            for target, suffix in job.cache_volumes
            for arg in (
                "--mount",
                f"type=volume,source={job.name}-{suffix},"
                f"target={target if target.startswith('/') else f'{job.workdir}/{target}'}",
            )
        ],
        "-e",
        "GIT_CONFIG_COUNT=1",
        "-e",
        "GIT_CONFIG_KEY_0=safe.directory",
        "-e",
        "GIT_CONFIG_VALUE_0=*",
        *_engine_mount(job.root),
        *_extra_mounts(),
    ]


def _extra_no_proxy() -> list[str]:
    return [h for h in os.environ.get("FACTORY_DOCKER_NO_PROXY", "").split(",") if h]


def docker_setup_command(job: DockerJob) -> list[str]:
    """의존성 설치. 패키지 저장소가 필요하므로 일반 네트워크, 키는 넘기지 않는다."""
    return [*_docker_base(job, "bridge"), job.image, "bash", "-c", job.setup_script]


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
        # 프록시 허용 목록은 엔진 것을 쓴다 (얇은 설치 레포에는 없다)
        (_content_tag("bookmark-factory-proxy", ENGINE / "docker/proxy"), ENGINE / "docker/proxy"),
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


def _jobs_dir(root: Path) -> Path:
    return root.parent / f"{root.name}.jobs"


def session_info_path(root: Path, job_id: str) -> Path:
    """세션 정보 (격리 방식, 이미지). 작업 worktree 밖에 두어 세션이 바꾸지 못한다."""
    return _jobs_dir(root) / "meta" / job_id / "session.json"


def session_info(root: Path, job_id: str) -> dict[str, Any]:
    path = session_info_path(root, job_id)
    if not path.exists():
        raise DispatchError(f"{job_id}: dispatch 세션 정보가 없습니다 ({path})")
    info: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return info


def host_record_path(root: Path, job_id: str) -> Path:
    """호스트가 한 일(머지·승인·반려)의 기록. worktree 밖이라 세션이 쓸 수 없다."""
    return _jobs_dir(root) / "meta" / job_id / "host.json"


def host_record(root: Path, job_id: str) -> dict[str, Any]:
    path = host_record_path(root, job_id)
    if not path.exists():
        return {"events": []}
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return record


def _record_host(root: Path, job_id: str, action: str, job: dict[str, Any]) -> None:
    record = host_record(root, job_id)
    record["events"].append(
        {
            "action": action,
            "status": job.get("status"),
            "merge_commit": job.get("merge_commit"),
            # 호스트가 신뢰 코드로 다시 돌린 검증만 (세션이 쓴 검증 기록은 넣지 않는다)
            "merge_checks": [c for c in job.get("merge_checks", []) if c.get("why") == "untrusted"],
            "at": time.time(),
        }
    )
    path = host_record_path(root, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def host_merge_env(docker: DockerJob | None, wait_s: float) -> dict[str, str]:
    """호스트가 기본 checkout의 factory.py로 머지·승인할 때의 설정.

    세션이 쓴 상태 파일(검증 기록, 수락 테스트 목록)은 믿지 않고 다시 검증한다. 컨테이너 작업이면
    재검증도 컨테이너에서 돌려 호스트가 에이전트 코드를 실행하지 않는다.
    """
    env = {"FACTORY_UNTRUSTED_STATE": "1", "FACTORY_MERGE_WAIT_S": str(int(wait_s))}
    if docker:
        tools = {
            "FACTORY_VERIFY_CMD": docker_tool_command(docker, ["python", "scripts/verify.py"]),
            "FACTORY_CHECK_RED_CMD": docker_tool_command(
                docker, ["python", "scripts/check_red.py"]
            ),
        }
        env.update({k: json.dumps(v) for k, v in tools.items()})
    return env


def _save_record(root: Path, job_id: str, job: dict[str, Any]) -> Path:
    record = _jobs_dir(root) / "records" / f"{job_id}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def run_job(
    root: Path,
    base: str,
    requirement: str,
    index: int,
    args: argparse.Namespace,
    stamp: str,
    image: str | None = None,
    plan_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """plan_job: 계획 실행이면 검사된 계획 작업 ({"plan_id", 초안 필드, "expected_risk"})."""
    jobs_dir = root.parent / f"{root.name}.jobs"
    logs = jobs_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    slot = jobs_dir / f"slot-{stamp}-{index}"
    result: dict[str, Any] = {"requirement": requirement, "job_id": None, "status": "SETUP_FAILED"}
    started = time.time()
    with _git_lock:
        _git(root, "worktree", "add", "-q", "--detach", str(slot), base)
    plan_args = []
    if plan_job:
        plan_args = ["--plan", f"{plan_job['plan_id']}:{plan_job['id']}",
                     "--expected-risk", plan_job["expected_risk"]]  # fmt: skip
    created = _factory(root, slot, "new", requirement, "--base", base, *plan_args,
                       env=_agent_env(args))  # fmt: skip
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
    docker = _docker_job(root, worktree, job_id, stamp, image) if image else None
    info = session_info_path(root, job_id)
    info.parent.mkdir(parents=True, exist_ok=True)
    session = {"isolation": "docker" if docker else "host", "image": image, "stamp": stamp,
               "agent": args.agent, "review_agent": args.review_agent, "orchestrator": args.plan,
               "wall_timeout": args.wall_timeout}  # fmt: skip
    extra = ""
    if plan_job:
        extra = plan.plan_context(plan_job["plan_id"], plan_job)
        session["plan"] = {"id": plan_job["plan_id"], "job": plan_job["id"],
                           "expected_risk": plan_job["expected_risk"]}  # fmt: skip
        session["prompt_plan"] = extra
    info.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")

    # 가상환경은 절대 경로를 품으므로 worktree를 옮긴 뒤에 설치한다
    if docker:
        done = _run(docker_setup_command(docker), worktree)
        if done.returncode != 0:
            result["error"] = f"컨테이너 설치 실패: {(done.stdout + done.stderr)[-500:]}"
            return result
    else:
        for area, cmd in setup_steps(root):
            done = _run(cmd, worktree / area)
            if done.returncode != 0:
                result["error"] = f"{area} 설치 실패: {(done.stdout + done.stderr)[-500:]}"
                return result

    applied = _load_job(worktree, job_id).get("budgets_applied") or {}
    session_cap = float(
        args.budget_usd
        if args.budget_usd is not None
        else applied.get("session", DEFAULT_BUDGET_USD)
    )
    result["session_budget_usd"] = session_cap
    template = CODEX_PROMPT if args.agent == "codex" else PROMPT
    prompt = template.format(job_id=job_id, requirement=requirement)
    if extra:
        prompt = f"{prompt}\n\n{extra}"
    log_path = logs / f"{job_id}.jsonl"
    result.update(_session(root, worktree, job_id, prompt, args, docker, log_path, session_cap))
    job = _load_job(worktree, job_id)
    session_logs = [log_path]
    for _ in range(MAX_CONTINUE):
        if not needs_continue(job):
            break
        more = continue_session(root, worktree, job_id, args, docker, session_cap)
        result.setdefault("continued", []).append(more)
        session_logs.append(Path(more["log"]))
        if more.get("session_cost_usd") is not None:
            result["session_cost_usd"] = (result.get("session_cost_usd") or 0) + more[
                "session_cost_usd"
            ]
        job = _load_job(worktree, job_id)
    builder_id = ((job.get("models") or {}).get("builder") or {}).get("model")
    prices = unit_prices(sorted(logs.glob("*.jsonl")))
    live = combine_usage(
        [live_usage(log, prices, builder_model=builder_id) for log in session_logs]
    )
    result["cost"] = job_cost(job, live)
    job_total = float(applied.get("job_total", 0) or 0)
    result["over_job_total"] = bool(job_total) and result["cost"]["total_usd"] > job_total
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
    result["record"] = str(_save_record(root, job_id, job))
    if docker:
        _docker("volume", "rm", f"{docker.name}-node", f"{docker.name}-venv")
    if job["status"] == "MERGED" and not args.keep_merged:
        result["worktree_removed"] = _remove_worktree(root, worktree, result)
    return result


def needs_continue(job: dict[str, Any]) -> bool:
    """rebase 충돌로 새 base 위에 다시 펼쳐져 Builder를 기다리는 작업인가."""
    failure = str(job.get("last_failure") or "")
    return job["status"] in {"RETRY_REQUIRED", "SPEC_READY"} and failure.startswith("CONFLICT")


def continue_prompt(job: dict[str, Any], job_id: str, agent: str) -> str:
    """이어서 처리하는 세션의 프롬프트. Builder부터(충돌 표시) 또는 Spec부터(기존 테스트 충돌)."""
    last = (job.get("replays") or [{}])[-1]
    files = ", ".join(last.get("files") or []) or "-"
    diffs = " / ".join(
        f"`git diff {str(last.get('onto'))[:7]} {str(last.get('from'))[:7]} -- {f}`"
        for f in last.get("files") or []
    )
    task = CONTINUE_SPEC_TASK if job["status"] == "SPEC_READY" else CONTINUE_TASK
    head = CONTINUE_HEAD["codex" if agent == "codex" else "claude"]
    return (head + task).format(
        job_id=job_id, requirement=job["requirement"], files=files, diffs=diffs or "-"
    )


def _session(
    root: Path,
    worktree: Path,
    job_id: str,
    prompt: str,
    args: argparse.Namespace,
    docker: DockerJob | None,
    log_path: Path,
    session_cap: float,
) -> dict[str, Any]:
    """오케스트레이터 세션 하나를 돌리고 끝난 뒤를 처리한다 (상한 기록, 호스트 머지)."""
    out: dict[str, Any] = {}
    workdir = docker.workdir if docker else None
    inner = agent_command(
        worktree, prompt, args.model, agent=args.agent, workdir=workdir, budget_usd=session_cap
    )
    command = docker_session_command(docker, inner, keys=args.keys) if docker else inner
    host_merge = merges_on_host(args.agent, docker is not None)
    session_env = {**os.environ, **_agent_env(args), "FACTORY_AGENT_SESSION": "1"}
    add, unset = session_auth_env(args.plan)
    session_env.update(add)
    for name in unset:
        session_env.pop(name, None)
    if host_merge:
        session_env["FACTORY_MERGE_ON_HOST"] = "1"
    err_path = log_path.with_suffix(".stderr")
    with open(log_path, "wb") as log_out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            resolve_command(command),
            cwd=worktree,
            stdout=log_out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            env=session_env,
            start_new_session=sys.platform != "win32",
        )
        try:
            out["exit_code"] = proc.wait(timeout=args.wall_timeout * 60)
            out["timed_out"] = False
        except subprocess.TimeoutExpired:
            if docker:
                _docker("kill", docker.name)
            _kill_tree(proc)
            out["exit_code"] = None
            out["timed_out"] = True

    usage = session_usage(log_path)
    out.update(session_cost_usd=usage["cost_usd"], session_tokens=usage["tokens"])
    cost = usage["cost_usd"]
    job = _load_job(worktree, job_id)
    if job["status"] in ACTIVE - {"APPROVED"}:
        reason = detail = None
        if out["timed_out"]:
            reason = "TIMEOUT"
            detail = f"실행 래퍼 벽시계 상한 {args.wall_timeout}분 초과로 세션을 종료함"
        elif cost is not None and cost >= session_cap:
            reason = "BUDGET"
            detail = f"세션 비용 ${cost:.2f}가 상한 ${session_cap:.2f}에 닿아 세션이 멈춤"
        elif not needs_continue(job):
            # 진행 중으로 남겨 두면 대시보드에서 계속 도는 것처럼 보인다 (Builder를 백그라운드로
            # 띄우고 기다리지 못한 세션, 2026-10-24 Sonnet 함정 과제 실험)
            reason = "SESSION_ENDED"
            detail = f"세션이 끝났는데 작업이 {job['status']}에 남음 (exit {out['exit_code']})"
        if reason and detail:
            _factory(root, worktree, "block", job_id, "--reason", reason, "--detail", detail)
            job = _load_job(worktree, job_id)
    if host_merge and job["status"] == "APPROVED":
        # 세션은 머지하지 않는다. 호스트가 기본 checkout의 factory.py로 머지한다.
        # 앞 작업의 컨테이너 재검증이 수 분 걸리므로 lock을 세션 상한만큼 기다린다
        env = host_merge_env(docker, wait_s=args.wall_timeout * 60)
        merged = _factory(root, worktree, "merge", job_id, env=env)
        out["host_merge"] = merged.stdout.strip()[-500:]
        job = _load_job(worktree, job_id)
        _record_host(root, job_id, "merge", job)
    return out


def continue_session(
    root: Path,
    worktree: Path,
    job_id: str,
    args: argparse.Namespace,
    docker: DockerJob | None,
    session_cap: float,
) -> dict[str, Any]:
    """충돌로 새 base 위에 다시 펼쳐진 작업을 세션 하나로 이어서 처리한다.

    로그는 `<ID>.r<n>.jsonl` (n = 2, 3, ...).
    """
    job = _load_job(worktree, job_id)
    logs = _jobs_dir(root) / "logs"
    n = 2 + len(list(logs.glob(f"{job_id}.r*.jsonl")))
    prompt = continue_prompt(job, job_id, args.agent)
    log_path = logs / f"{job_id}.r{n}.jsonl"
    out = _session(root, worktree, job_id, prompt, args, docker, log_path, session_cap)
    job = _load_job(worktree, job_id)
    _record_host(root, job_id, "continue", job)
    return {"round": n, "status": job["status"], "log": str(log_path), **out}


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


def decide(root: Path, job_id: str, reject: str | None, keep_merged: bool = False) -> int:
    """승인 대기 작업을 사람이 승인(머지) 또는 반려한다. 기본 checkout의 factory.py로 실행한다."""
    worktree = _jobs_dir(root) / job_id
    info = session_info(root, job_id)
    if not (worktree / "factory/state.json").exists():
        raise DispatchError(f"{job_id}: 작업 worktree가 없습니다 ({worktree})")
    status = _load_job(worktree, job_id)["status"]
    if status != "APPROVAL_REQUIRED":
        raise DispatchError(f"{job_id}: 승인 대기 상태가 아닙니다 ({status})")
    if reject:
        done = _factory(
            root, worktree, "approve", job_id, "--reject", reject,
            env={"FACTORY_UNTRUSTED_STATE": "1"},
        )  # fmt: skip
        print(done.stdout.strip())
        job = _load_job(worktree, job_id)
        _save_record(root, job_id, job)
        _record_host(root, job_id, "reject", job)
        return done.returncode
    docker = None
    if info["isolation"] == "docker":
        docker = _docker_job(root, worktree, job_id, info["stamp"], info["image"])
        # 세션이 끝날 때 의존성 볼륨을 지웠으므로 재검증 전에 다시 설치한다 (키 없음)
        setup = _run(docker_setup_command(docker), worktree)
        if setup.returncode != 0:
            raise DispatchError(f"컨테이너 설치 실패: {(setup.stdout + setup.stderr)[-500:]}")
    try:
        env = host_merge_env(docker, wait_s=DEFAULT_WALL_TIMEOUT_MIN * 60)
        done = _factory(root, worktree, "approve", job_id, env=env)
        print(done.stdout.strip()[-2000:])
        job = _load_job(worktree, job_id)
        _save_record(root, job_id, job)
        _record_host(root, job_id, "approve", job)
        if needs_continue(job):
            # 승인 사이 base에 같은 줄이 먼저 머지됐다. 새 base 위에서 세션이 충돌을 푼다
            args = _session_args(root, info, docker is not None)
            cap = float((job.get("budgets_applied") or {}).get("session", DEFAULT_BUDGET_USD))
            for _ in range(MAX_CONTINUE):
                more = continue_session(root, worktree, job_id, args, docker, cap)
                print(
                    f"{job_id}: 충돌을 풀러 이어서 처리 (세션 {more['round']}) → {more['status']}"
                )
                job = _load_job(worktree, job_id)
                if not needs_continue(job):
                    break
            _save_record(root, job_id, job)
            if job["status"] == "APPROVAL_REQUIRED":
                print(
                    f"고친 변경을 다시 승인해 주세요: python scripts/dispatch.py --approve {job_id}"
                )
    finally:
        if docker:
            _docker("volume", "rm", f"{docker.name}-node", f"{docker.name}-venv")
    if job["status"] == "MERGED" and not keep_merged:
        result: dict[str, Any] = {}
        if not _remove_worktree(root, worktree, result):
            print(f"worktree를 지우지 못했습니다: {result.get('cleanup_error')}")
    if job["status"] == "MERGED" and job.get("plan_id"):
        print(
            f"계획 {job['plan_id']}의 남은 작업 이어서 (사람 전용): "
            f"python scripts/dispatch.py --run-plan {job['plan_id']}"
        )
    return 0 if job["status"] == "MERGED" else 1


def plan_statuses(root: Path, state: dict[str, Any]) -> dict[str, str | None]:
    """계획 작업 → 연결된 팩토리 작업의 지금 상태.

    worktree가 있으면 그 상태(사람이 resume·drop했을 수 있음), 지워졌으면 호스트 작업 기록.
    """
    out: dict[str, str | None] = {}
    for name, run in (state.get("runs") or {}).items():
        status, job_id = run.get("status"), run.get("job_id")
        if job_id:
            worktree = _jobs_dir(root) / job_id
            record = _jobs_dir(root) / "records" / f"{job_id}.json"
            if (worktree / "factory/state.json").exists():
                status = _load_job(worktree, job_id)["status"]
            elif record.exists():
                status = json.loads(record.read_text(encoding="utf-8"))["status"]
        out[name] = status
    return out


def run_plan(
    root: Path, state: dict[str, Any], args: argparse.Namespace, base: str, stamp: str,
    image: str | None,
) -> list[dict[str, Any]]:  # fmt: skip
    """검사된 계획을 depends_on 순서로 실행한다 (기획안 6.9 P1).

    선행 작업이 모두 MERGED인 작업만 시작하고, 하나가 끝날 때마다 다시 고른다. 승인 대기·BLOCKED에
    기대는 작업은 기다리고 독립 작업은 계속 돈다. 사람이 승인·처리한 뒤 다시 실행하면 남은 작업만
    한다.
    """
    items = {j["id"]: j for j in state["jobs"]}
    runs = state.setdefault("runs", {})
    state["status"] = "RUNNING"
    plan.save_state(root, state)
    results: list[dict[str, Any]] = []
    attempted: set[str] = set()
    running: dict[Future[dict[str, Any]], str] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.max_parallel)) as pool:
        while True:
            statuses = plan_statuses(root, state)
            start, _ = plan.schedule(state, statuses, set(running.values()) | attempted)
            for name in start[: max(0, args.max_parallel - len(running))]:
                attempted.add(name)
                item = {**items[name], "plan_id": state["id"]}
                future = pool.submit(run_job, root, base, item["requirement"], len(attempted),
                                     args, stamp, image, item)  # fmt: skip
                running[future] = name
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                name = running.pop(future)
                try:
                    result = future.result()
                except DispatchError as error:
                    result = {"requirement": items[name]["requirement"], "job_id": None,
                              "status": "ERROR", "error": str(error)}  # fmt: skip
                result["plan_job"] = name
                results.append(result)
                runs[name] = {"job_id": result.get("job_id"), "status": result["status"],
                              "at": time.time()}  # fmt: skip
                plan.save_state(root, state)
    statuses = plan_statuses(root, state)
    _, waiting = plan.schedule(state, statuses, set())
    state["status"] = plan.plan_status(state, statuses)
    state["waiting"] = waiting
    plan.save_state(root, state)
    return results


def plan_summary(state: dict[str, Any], statuses: dict[str, str | None]) -> str:
    say = {"dispatch": "", "human": "사람 직접", "needs_decision": "결정 필요"}
    lines = [
        f"계획 {state['id']}: {state['status']}",
        "",
        "| 계획 작업 | 팩토리 작업 | 상태 | 예상 등급 | 대기 사유 |",
        "|---|---|---|---|---|",
    ]
    for item in state["jobs"]:
        name = item["id"]
        run = (state.get("runs") or {}).get(name) or {}
        status = say[item["route"]] or statuses.get(name) or "-"
        if name in state.get("waiting", {}):
            status = "PLAN_WAITING"
        lines.append(
            f"| {name} | {run.get('job_id') or '-'} | {status} | {item['expected_risk']} "
            f"| {state.get('waiting', {}).get(name, '')} |"
        )
    again = "python scripts/dispatch.py --run-plan {id}"
    hint = {
        "RUNNING": f"승인 대기를 처리한 뒤 다시: {again}",
        "STOPPED": f"BLOCKED·사람 직접 항목을 처리한 뒤 다시: {again}",
        "DONE": "계획의 팩토리 작업이 모두 머지됐습니다",
    }
    lines += ["", hint[state["status"]].format(id=state["id"])]
    return "\n".join(lines)


def _session_args(root: Path, info: dict[str, Any], docker: bool) -> argparse.Namespace:
    """승인 뒤 이어서 처리하는 세션의 설정. 처음 dispatch가 남긴 세션 정보를 그대로 쓴다."""
    agent = info.get("agent") or "claude"
    review_agent = info.get("review_agent") or agent
    plan = info.get("orchestrator") or orchestrator_plan(root, agent, None, None, docker)
    real_docker = docker and not os.environ.get("FACTORY_AGENT_CMD")
    return argparse.Namespace(
        agent=agent,
        review_agent=review_agent,
        plan=plan,
        model=plan["model"],
        wall_timeout=float(info.get("wall_timeout") or DEFAULT_WALL_TIMEOUT_MIN),
        keys=api_key_names([agent, review_agent]) if real_docker else None,
    )


def _agent_env(args: argparse.Namespace) -> dict[str, str]:
    """factory.py가 작업에 기록하고 리뷰어를 고를 때 쓰는 도구 설정."""
    return {"FACTORY_AGENT": args.agent, "FACTORY_REVIEW_AGENT": args.review_agent}


def _summary(report: dict[str, Any]) -> str:
    lines = [
        f"도구: {report['agent']} (리뷰어 {report['review_agent']}), "
        f"오케스트레이터 {report['orchestrator']['alias']} ({report['orchestrator']['source']}), "
        f"인증: {report['auth']}, 격리 {report['isolation']}, "
        f"동시 실행 {report['max_parallel']}개, base {report['base']}",
        "",
        "| 작업 | 상태 | 위험 | 활성/경과(분) | 작업 비용($) | 비고 | worktree |",
        "|---|---|---|---|---|---|---|",
    ]
    for job in report["jobs"]:
        note = job.get("blocked") or job.get("error") or ""
        if job.get("timed_out"):
            note = f"벽시계 상한 초과 {note}"
        if job.get("merge_commit"):
            note = f"머지 {job['merge_commit'][:7]}"
        where = "(삭제)" if job.get("worktree_removed") else job.get("worktree", "")
        cost = job.get("cost") or {}
        cost_text = "-"
        if cost:
            mark = "≈" if cost.get("estimated") else ""
            session = cost.get("session_usd")
            cost_text = (
                f"{mark}{cost['total_usd']:.2f} (세션 "
                f"{'-' if session is None else f'{session:.2f}'} + "
                f"리뷰 {cost['reviewers_usd']:.2f})"
            )
        lines.append(
            f"| {job.get('job_id') or '-'} | {job['status']} | {job.get('risk') or '-'} | "
            f"{job.get('active_min', '-')}/{job.get('wall_min', '-')} | "
            f"{cost_text} | {note} | {where} |"
        )
    lines += [
        "",
        "승인 대기: `python scripts/dispatch.py --approve <ID>` (반려는 `--reject <ID> --reason`). "
        "BLOCKED: 해당 worktree에서 `python scripts/factory.py resume|drop <ID>`.",
    ]
    return "\n".join(lines)


# Windows 알림 (WinRT, Windows PowerShell 5.1 내장). 문구는 환경변수로만 받는다 (명령에 넣지 않음)
TOAST_SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType = WindowsRuntime] > $null; "
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
    "$n = $t.GetElementsByTagName('text'); "
    "$n.Item(0).AppendChild($t.CreateTextNode($env:FACTORY_TOAST_TITLE)) > $null; "
    "$n.Item(1).AppendChild($t.CreateTextNode($env:FACTORY_TOAST_BODY)) > $null; "
    "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'; "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show("
    "[Windows.UI.Notifications.ToastNotification]::new($t))"
)
STATUS_SAY = {"MERGED": "MERGED", "APPROVAL_REQUIRED": "승인 대기", "BLOCKED": "BLOCKED"}


def toast_text(report: dict[str, Any]) -> tuple[str, str]:
    counts: dict[str, int] = {}
    for job in report["jobs"]:
        say = STATUS_SAY.get(str(job.get("status")), str(job.get("status")))
        counts[say] = counts.get(say, 0) + 1
    body = " · ".join(f"{say} {n}" for say, n in counts.items())
    return f"팩토리 dispatch 끝 ({len(report['jobs'])}건)", body


def notify_done(root: Path, report: dict[str, Any]) -> None:
    """dispatch 종료 알림 (기획안 6.9 P2). 알림이 실패해도 dispatch 결과는 바꾸지 않는다."""
    try:
        if not load_config(root).notify_toast:
            return
    except ConfigError:
        return
    raw = os.environ.get("FACTORY_NOTIFY_CMD")
    if raw:
        cmd = [str(a) for a in json.loads(raw)]
    elif sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST_SCRIPT]
    else:
        return
    title, body = toast_text(report)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            resolve_command(cmd),
            env={**os.environ, "FACTORY_TOAST_TITLE": title, "FACTORY_TOAST_BODY": body},
            capture_output=True,
            timeout=20,
            check=False,
        )


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    root = root or Path(os.environ.get("FACTORY_REPO") or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(prog="dispatch", description="팩토리 병렬 실행")
    parser.add_argument("requirements", nargs="*")
    parser.add_argument("--approve", metavar="ID", help="승인 대기 작업을 승인하고 머지")
    parser.add_argument(
        "--plan", metavar="목표", help="계획자로 목표를 작업 초안으로 나눔 (기획안 6.9 P1)"
    )
    parser.add_argument(
        "--run-plan", metavar="ID", help="검사된 계획을 실행 (사람 전용, 실행 = 계획 승인)"
    )
    parser.add_argument("--reject", metavar="ID", help="승인 대기 작업을 반려")
    parser.add_argument("--reason", help="--reject 이유")
    parser.add_argument("--max-parallel", type=int, default=3)
    parser.add_argument("--wall-timeout", type=float, default=DEFAULT_WALL_TIMEOUT_MIN, help="분")
    parser.add_argument(
        "--budget-usd", type=float, help="세션 비용 상한 (기본: 작업에 고정된 budgets.session)"
    )
    parser.add_argument("--agent", choices=sorted(DEFAULT_MODELS), default="claude")
    parser.add_argument("--review-agent", choices=sorted(DEFAULT_MODELS), help="기본: --agent")
    parser.add_argument(
        "--model", help="오케스트레이터 모델 (claude: haiku|sonnet|opus, 기본: 설정·자동)"
    )
    parser.add_argument(
        "--auth", choices=["subscription", "api"], help="오케스트레이터 인증 (기본: 설정·자동)"
    )
    parser.add_argument("--isolation", choices=["host", "docker"], default="host")
    parser.add_argument("--require-api-key", action="store_true")
    parser.add_argument("--keep-merged", action="store_true", help="머지된 worktree도 남김")
    args = parser.parse_args(argv)
    if args.plan:
        try:
            plan_id = plan.draft_plan(root, args.plan)
        except plan.PlanError as error:
            print(f"오류: {error}")
            return 1
        print(f"계획 초안: {plan.PLANS_DIR}/{plan_id}.md (계획자, 사람이 확인)")
        return plan.main(["check", plan_id], root=root)
    if args.approve or args.reject:
        if args.reject and not args.reason:
            print("오류: --reject에는 --reason이 필요합니다")
            return 2
        try:
            return decide(root, args.approve or args.reject, args.reason if args.reject else None,
                          keep_merged=args.keep_merged)  # fmt: skip
        except DispatchError as error:
            print(f"오류: {error}")
            return 2
    plan_state = None
    if args.run_plan:
        if args.requirements:
            print("오류: --run-plan에는 요구사항을 함께 줄 수 없습니다 (계획에서 읽음)")
            return 2
        try:
            plan_state = plan.load_checked(root, args.run_plan)
        except plan.PlanError as error:
            print(f"오류: {error}")
            return 2
    elif not args.requirements:
        print("오류: 요구사항이 필요합니다 (또는 --approve / --reject / --run-plan)")
        return 2
    args.review_agent = args.review_agent or args.agent
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
            and not os.environ.get("FACTORY_AGENT_CMD")
        ):
            # 호스트 실행이면 머지 전 재검증이 에이전트 코드를 샌드박스 밖에서 돌린다. Windows에서는
            # Codex 샌드박스가 읽기 전용으로 떨어져 명령·패치도 못 한다 (codex 0.163 실측)
            raise DispatchError("Codex는 --isolation docker로만 실행합니다")
        try:
            load_config(root)
        except ConfigError as error:
            raise DispatchError(str(error)) from error
        # 가짜 오케스트레이터(FACTORY_AGENT_CMD)로 시험할 때는 컨테이너 인증 조건을 보지 않는다
        real_docker = args.isolation == "docker" and not os.environ.get("FACTORY_AGENT_CMD")
        args.plan = orchestrator_plan(root, args.agent, args.model, args.auth, real_docker)
        if real_docker:
            check_container_stages(root)
        args.model = args.plan["model"]
        if args.isolation == "docker":
            image = prepare_docker(root)
    except DispatchError as error:
        print(f"오류: {error}")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    count = f"계획 {args.run_plan}" if plan_state else f"작업 {len(args.requirements)}개"
    print(
        f"도구: {args.agent}, 오케스트레이터 {args.plan['alias']} ({args.plan['source']}), "
        f"인증: {auth_label(args.plan)}, 격리 {args.isolation}, "
        f"{count}, 동시 {args.max_parallel}개"
    )
    if plan_state:
        jobs = run_plan(root, plan_state, args, base, stamp, image)
    else:
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
        "plan_id": args.run_plan,
        "agent": args.agent,
        "review_agent": args.review_agent,
        "auth": auth_label(args.plan),
        "orchestrator": args.plan,
        "isolation": args.isolation,
        "image": image,
        "base": base,
        "max_parallel": args.max_parallel,
        "budget_usd": args.budget_usd if args.budget_usd is not None else DEFAULT_BUDGET_USD,
        "model": args.model,
        "jobs": jobs,
    }
    jobs_dir = root.parent / f"{root.name}.jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"dispatch-{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(_summary(report))
    if plan_state:
        print()
        print(plan_summary(plan_state, plan_statuses(root, plan_state)))
    if jobs:
        notify_done(root, report)
    if plan_state:
        return 1 if plan_state["status"] == "STOPPED" else 0
    return 0 if all(j["status"] not in {"ERROR", "SETUP_FAILED"} for j in jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
