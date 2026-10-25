"""팩토리 상태 관리 CLI (factory/PROCESS.md).

상태는 factory/state.json에만 있고 이 명령만 바꾼다. 단계 전이에 필요한 판단
(Red/Green 확인, 전체 검증, 잠긴 파일 대조, 시간·재시도 상한)은 이 명령이 직접 실행한다.

사용법: python scripts/factory.py <명령> ...
명령: new, spec, red, verify, validate, merge, recover, block, status, approve, resume, drop
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_config import CLAUDE_MODELS, Config, ConfigError, load_config  # noqa: E402
from proc import resolve_command, use_utf8_output  # noqa: E402

Job = dict[str, Any]
State = dict[str, Any]

STATE_PATH = "factory/state.json"
SUMMARY_PATH = "factory-state.md"
SPECS_DIR = "factory/specs"
REVIEWS_DIR = "factory/reviews"
VALIDATOR_PROMPT = "factory/prompts/validator.md"  # 리뷰어 공통 지시
REVIEWER_PROMPT = "factory/prompts/review-{name}.md"  # 리뷰어별 관점
HEARTBEAT_PATH = "factory/heartbeat.json"
MERGE_LOCK = "factory-merge.lock"  # Git 공통 디렉터리에 둔다 (worktree끼리 공유)
# 상태 파일을 믿지 않는 실행 (dispatch의 호스트 머지·사람 승인). 컨테이너 세션은 작업 worktree의
# 상태·설정 파일을 쓸 수 있으므로, 설정은 이 스크립트가 있는 checkout에서 읽고 검증은 다시 한다
UNTRUSTED_ENV = "FACTORY_UNTRUSTED_STATE"
# 얇은 설치(기획안 12.1)면 엔진은 레포 밖에 있고, 대상 레포의 shim이 FACTORY_REPO로 레포를 알린다
ENGINE_ROOT = Path(__file__).resolve().parents[1]
TRUSTED_ROOT = Path(os.environ.get("FACTORY_REPO") or ENGINE_ROOT)

TYPES = {"feature", "bugfix", "copy", "refactor"}
SIZE_CAPS = {"small": 15 * 60, "medium": 30 * 60}
RISKS = ["LOW", "MEDIUM", "HIGH"]
SOFT_RATIO = 0.8
MAX_ATTEMPTS = 3
MAX_SPEC_REVISIONS = 1
EXTEND_RATIO = 0.5
LOCK_TTL_S = 300  # 마지막 활동 후 이 시간이 지나야 다른 세션이 작업을 이어받는다
MERGE_WAIT_S = 180  # 다른 작업이 머지 중이면 lock을 이만큼 기다린다 (병렬 실행)
AUTO_MERGE_RISKS = {"LOW"}

# 리뷰어 분리 (Phase 3c): 모두 PASS여야 통과. 승인 결정(Approver)은 모델이 아니라 규칙이 한다
REVIEWERS = ["code", "security", "test"]
# 에이전트 도구별 모델 (기획안 6.6). Builder는 경량 모델로 시작하고 실패 PROMOTE_AFTER회 뒤 승격
AGENTS = ["claude", "codex"]
MODELS = {
    "claude": {"builder": "sonnet", "builder_strong": "opus", "reviewer": "claude-sonnet-5-5"},
    "codex": {"builder": "gpt-6.1-sol", "builder_strong": "gpt-6-astra", "reviewer": "gpt-6.1-sol"},
}
PROMOTE_AFTER = 2
# 리뷰어 호출 하나당 비용 상한 (API 환산, 임시값). Claude만 적용 (Codex CLI에는 옵션이 없다)
REVIEW_BUDGET_USD = 0.5  # factory_config.BUDGET_DEFAULTS와 같다 (작업에 고정된 값이 우선)
# 에이전트 세션(FACTORY_AGENT_SESSION=1)에서는 factory.py가 직접 거부한다 (도구 hook과 별개)
HUMAN_ONLY = {"approve", "resume", "drop"}

ACTIVE_STATUSES = {"QUEUED", "SPEC_READY", "BUILDING", "RETRY_REQUIRED", "VERIFYING"}
CLOSED_STATUSES = {"MERGED", "DROPPED"}
RECOVERABLE_STATUSES = ACTIVE_STATUSES | {"APPROVED"}

# 위험 등급 계산에서 제외하는 팩토리 산출물
RISK_EXCLUDE = [STATE_PATH, SUMMARY_PATH, f"{SPECS_DIR}/*", f"{REVIEWS_DIR}/*", ".pytest_tmp*"]
# 검증한 변경과 머지하는 변경이 같은지 대조할 때 제외하는 파일
TREE_EXCLUDE = [STATE_PATH, SUMMARY_PATH, HEARTBEAT_PATH, f"{REVIEWS_DIR}/*", ".pytest_tmp*"]
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", ".git", ".mypy_cache", ".ruff_cache"}

HIGH_CONTENT = [(re.compile(r"DELETE\s+FROM", re.I), "데이터 삭제 SQL 추가")]
MEDIUM_CONTENT = [
    (re.compile(r"\b(CREATE|ALTER)\s+TABLE\b", re.I), "DB 스키마 변경"),
    (
        re.compile(r"@\w+\.(get|post|put|patch|delete)\("),
        "API 경로 추가·삭제",
    ),
    (
        re.compile(r"pytest\.mark\.(skip|xfail)|\b(it|test|describe)\.(skip|todo|only)\("),
        "테스트 skip·xfail·only 추가",
    ),
]
# 계획 단계(아직 코드가 없음)의 변경 표시 → 위 내용 규칙과 같은 등급·사유 (기획안 6.9 P1)
RISK_TOUCHES = {
    "data_delete": ("HIGH", HIGH_CONTENT[0][1]),
    "db_schema": ("MEDIUM", MEDIUM_CONTENT[0][1]),
    "api": ("MEDIUM", MEDIUM_CONTENT[1][1]),
    "test_skip": ("MEDIUM", MEDIUM_CONTENT[2][1]),
}
MAX_LINES = 400
MAX_FILES = 15


class FactoryError(Exception):
    """사용법·상태 오류. 상태를 바꾸지 않는다."""


def _untrusted() -> bool:
    return os.environ.get(UNTRUSTED_ENV) == "1"


def _config(root: Path) -> Config:
    """레포별 설정 (factory/config.json + 키트 공통 보호)."""
    try:
        return load_config(TRUSTED_ROOT if _untrusted() else root)
    except ConfigError as error:
        raise FactoryError(str(error)) from error


# ---------------------------------------------------------------- 시간


def now() -> float:
    fake = os.environ.get("FACTORY_FAKE_NOW")
    return float(fake) if fake else time.time()


def active_seconds(job: Job, now: float) -> float:
    total = 0.0
    for start, end in job["time"]["segments"]:
        total += (end if end is not None else now) - start
    return total


def _clock_running(job: Job) -> bool:
    segments = job["time"]["segments"]
    return bool(segments) and segments[-1][1] is None


def _stop_clock(job: Job, at: float) -> None:
    if _clock_running(job):
        job["time"]["segments"][-1][1] = at
    job["time"]["active_s"] = round(active_seconds(job, at), 1)


def _start_clock(job: Job, at: float) -> None:
    if not _clock_running(job):
        job["time"]["segments"].append([at, None])


# ---------------------------------------------------------------- 상태 파일


def load_state(root: Path) -> State:
    path = root / STATE_PATH
    if not path.exists():
        return {"version": 1, "active": None, "jobs": {}}
    loaded: State = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def save_state(root: Path, state: State) -> None:
    at = now()
    for job in state["jobs"].values():
        job["time"]["active_s"] = round(active_seconds(job, at), 1)
    path = root / STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    (root / SUMMARY_PATH).write_text(render_summary(state), encoding="utf-8")


def _fmt_minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f}분"


def _job_note(job: Job) -> str:
    if job["status"] == "BLOCKED" and job.get("blocked"):
        return f"{job['blocked']['reason']}: {job['blocked']['detail']}"[:80]
    if job["status"] in {"APPROVED", "MERGED"} and job.get("approval"):
        note = f"승인 {job['approval']['by']}"
        if job.get("merge_commit"):
            note += f", 머지 커밋 {job['merge_commit'][:7]}"
        return note
    if job["status"] in ACTIVE_STATUSES and job.get("builder_model"):
        return f"Builder 모델 {job['builder_model']}"
    return ""


def render_summary(state: State) -> str:
    lines = [
        "# 팩토리 상태",
        "",
        "> `python scripts/factory.py`가 자동으로 다시 쓰는 파일이다. 직접 고치지 않는다.",
        "",
        f"- 진행 중 작업: {state['active'] or '없음'}",
        "",
        "| ID | 상태 | 유형/규모 | 위험 | 시도 | 활성 시간 / 상한 | 요구사항 | 비고 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for job in state["jobs"].values():
        note = _job_note(job)
        cells = [
            job["id"],
            job["status"],
            f"{job.get('type') or '-'}/{job.get('size') or '-'}",
            job.get("risk") or job.get("risk_declared") or "-",
            str(job["attempts"]),
            f"{_fmt_minutes(job['time']['active_s'])} / {_fmt_minutes(job['time']['cap_s'])}",
            job["requirement"][:60],
            note,
        ]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    waiting = [j["id"] for j in state["jobs"].values() if j["status"] == "APPROVAL_REQUIRED"]
    to_merge = [j["id"] for j in state["jobs"].values() if j["status"] == "APPROVED"]
    lines += [
        "",
        f"- 승인 대기: {', '.join(waiting) if waiting else '없음'}",
        f"- 머지 대기: {', '.join(to_merge) if to_merge else '없음'}",
        "",
    ]
    return "\n".join(lines)


def _job(state: State, job_id: str) -> Job:
    if job_id not in state["jobs"]:
        raise FactoryError(f"작업 {job_id}가 없습니다")
    job: Job = state["jobs"][job_id]
    return job


def _require(job: Job, *statuses: str) -> None:
    if job["status"] not in statuses:
        raise FactoryError(
            f"{job['id']}는 {job['status']} 상태라 이 명령을 실행할 수 없습니다 "
            f"(허용: {', '.join(statuses)})"
        )


def _set_status(job: Job, status: str) -> None:
    job["status"] = status
    if status not in {"BLOCKED", "DROPPED"}:
        job["checkpoint"] = status
    job["history"].append({"status": status, "at": now()})


def _block(job: Job, reason: str, detail: str) -> None:
    _set_status(job, "BLOCKED")
    job["blocked"] = {"reason": reason, "detail": detail, "at": now()}
    # resume 뒤에도 사유를 남긴다 (운영 보고서의 BLOCKED 사유 분포, scripts/report.py)
    job.setdefault("blocks", []).append({"reason": reason, "at": now()})
    _stop_clock(job, now())


# ---------------------------------------------------------------- 외부 명령


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise FactoryError(f"git {' '.join(args)} 실패: {proc.stderr.strip()}")
    return proc.stdout


def _command(kind: str, default: list[str]) -> list[str]:
    override = os.environ.get(f"FACTORY_{kind.upper()}_CMD")
    return list(json.loads(override)) if override else default


def _run(
    cmd: list[str],
    root: Path,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    full = None
    if env or unset:
        full = {**os.environ, **(env or {})}
        for name in unset:
            full.pop(name, None)
    return subprocess.run(
        resolve_command(cmd),
        cwd=root,
        input=stdin,
        env=full,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def run_check_red(root: Path, expect: str, tests: list[str]) -> dict[str, Any]:
    cmd = _command("check_red", [sys.executable, "scripts/check_red.py"])
    proc = _run([*cmd, "--expect", expect, "--json", *tests], root)
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            parsed: dict[str, Any] = json.loads(line)
            return parsed
        except json.JSONDecodeError:
            continue
    return {
        "verdict": "NO_OUTPUT",
        "exit_code": proc.returncode,
        "tests": [],
        "detail": (proc.stdout + proc.stderr)[-1500:],
    }


def run_verify(root: Path, extra: list[str] | None = None) -> tuple[int, str]:
    cmd = [*_command("verify", [sys.executable, "scripts/verify.py"]), *(extra or [])]
    proc = _run(cmd, root)
    return proc.returncode, proc.stdout + proc.stderr


def run_style(root: Path, files: list[str]) -> tuple[bool, list[str]]:
    cmd = _command("style", [sys.executable, "scripts/check_style.py"])
    proc = _run([*cmd, *files], root)
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        return bool(parsed.get("ok")), list(parsed.get("problems", []))
    return False, [f"check_style 출력 없음 (exit {proc.returncode}): {proc.stderr[-500:]}"]


# ---------------------------------------------------------------- 파일·diff


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def _repo_files(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".pytest_tmp")
        ]
        for name in filenames:
            rel = Path(dirpath, name).relative_to(root).as_posix()
            found.append(rel)
    return found


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock_targets(root: Path, job: Job) -> list[str]:
    allowed = set(job["allowed_config_changes"])
    targets = {
        rel
        for rel in _repo_files(root)
        if _matches(rel, _config(root).lock_patterns) and rel not in allowed
    }
    targets.update(t.split("::")[0] for t in job["acceptance_tests"])
    targets.update(job["changed_tests"])
    targets.add(job["spec_path"])
    return sorted(targets)


def _changed_locks(root: Path, job: Job) -> list[str]:
    changed = []
    for rel, digest in job["locks"].items():
        path = root / rel
        if not path.exists():
            changed.append(f"{rel} (삭제됨)")
        elif _sha(path) != digest:
            changed.append(rel)
    for rel in _repo_files(root):
        if rel not in job["locks"] and _matches(rel, _config(root).no_new_file_patterns):
            changed.append(f"{rel} (새 파일)")
    return changed


def _diff(root: Path, base: str) -> tuple[str, dict[str, tuple[list[str], list[str]]]]:
    """기준 커밋 대비 작업 트리 변경. (원문 diff, 파일별 (추가 줄, 삭제 줄))"""
    raw = _git(root, "diff", "--no-color", "--unified=0", base)
    files: dict[str, tuple[list[str], list[str]]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                files.setdefault(current, ([], []))
        elif line.startswith("--- "):
            source = line[4:]
            if source.startswith("a/"):
                files.setdefault(source[2:], ([], []))
                current = source[2:]
        elif current is not None and line.startswith("+"):
            files[current][0].append(line[1:])
        elif current is not None and line.startswith("-"):
            files[current][1].append(line[1:])
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    extra = []
    for rel in untracked:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files[rel] = (text.splitlines(), [])
        extra.append(
            f"--- /dev/null\n+++ b/{rel} (새 파일)\n"
            + "".join(f"+{ln}\n" for ln in text.splitlines())
        )
    files = {p: v for p, v in files.items() if not _matches(p, RISK_EXCLUDE)}
    return raw + "\n".join(extra), files


def path_risk(cfg: Config, path: str) -> tuple[str, str] | None:
    """경로 규칙 하나. verify(실제 diff)와 계획 검사(예상 경로)가 같이 쓴다."""
    if _matches(path, cfg.high_paths):
        return "HIGH", f"{path} 변경"
    if _matches(path, cfg.medium_paths):
        return "MEDIUM", f"{path} 변경"
    return None


def type_risk(job_type: str) -> tuple[str, str] | None:
    return ("MEDIUM", "refactor 유형") if job_type == "refactor" else None


def compute_risk(root: Path, job: Job) -> tuple[str, list[str]]:
    cfg = _config(root)
    _, files = _diff(root, job["base_commit"])
    level = RISKS.index(job["risk_declared"])
    reasons = [f"contract 선언: {job['risk_declared']}"]

    def raise_to(target: str, reason: str) -> None:
        nonlocal level
        reasons.append(f"{target}: {reason}")
        level = max(level, RISKS.index(target))

    if rule := type_risk(job["type"]):
        raise_to(*rule)
    if job["allowed_config_changes"]:
        raise_to("MEDIUM", "설정 파일 변경 허용")
    total_lines = 0
    for path, (added, removed) in files.items():
        total_lines += len(added) + len(removed)
        if rule := path_risk(cfg, path):
            raise_to(*rule)
        for pattern, reason in HIGH_CONTENT:
            if any(pattern.search(ln) for ln in added):
                raise_to("HIGH", f"{reason} ({path})")
        for pattern, reason in MEDIUM_CONTENT:
            if any(pattern.search(ln) for ln in added + removed):
                raise_to("MEDIUM", f"{reason} ({path})")
    if total_lines > MAX_LINES or len(files) > MAX_FILES:
        raise_to("MEDIUM", f"변경 규모 {len(files)}파일 {total_lines}줄")
    return RISKS[level], reasons


def _fork_point(root: Path, job: Job) -> str:
    """작업 변경의 기준점. rebase 전에는 base_commit, rebase 후에는 새 base 끝."""
    return _git(root, "merge-base", "HEAD", job["base_branch"]).strip()


def _changed_paths(root: Path, base: str) -> set[str]:
    """기준 커밋 대비 바뀐 파일 이름 (삭제, 추적 안 되는 새 파일, 바이너리 포함)."""
    names = set(_git(root, "diff", "--name-only", base).splitlines())
    names.update(_git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return {n for n in names if n}


def job_verify_args(root: Path, job: Job) -> list[str]:
    """작업 검증에 덧붙일 verify 인자 (factory/config.json의 job_verify).

    작업 diff에 전체 검증 조건 경로(키트 파일, 의존성 설정 등)가 있으면 빈 목록 = 전체 검증.
    """
    cfg = _config(root)
    if not cfg.job_verify_args:
        return []
    changed = _changed_paths(root, _fork_point(root, job))
    if any(_matches(p, cfg.job_verify_full_if_changed) for p in changed):
        return []
    return list(cfg.job_verify_args)


def tree_fingerprint(root: Path, job: Job) -> str:
    """기준점 대비 바뀐 파일과 그 내용의 해시. 커밋 여부와 관계없이 같은 변경이면 같은 값."""
    names = _changed_paths(root, _fork_point(root, job))
    digest = hashlib.sha256()
    for rel in sorted(n for n in names if n and not _matches(n, TREE_EXCLUDE)):
        path = root / rel
        digest.update(f"{rel}\0{_sha(path) if path.is_file() else 'deleted'}\n".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------- Git·lock 보조


def _git_ok(root: Path, *args: str) -> bool:
    return (
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=False).returncode == 0
    )


def _identity(root: Path) -> list[str]:
    if _git_ok(root, "config", "user.email"):
        return []
    return ["-c", "user.name=factory", "-c", "user.email=factory@localhost"]


def _git_path(root: Path, name: str, common: bool = False) -> Path:
    if common:
        base = Path(_git(root, "rev-parse", "--git-common-dir").strip())
        path = base / name
    else:
        path = Path(_git(root, "rev-parse", "--git-path", name).strip())
    return path if path.is_absolute() else root / path


def _abort_git_operations(root: Path) -> list[str]:
    """세션이 죽으면서 남긴 rebase·merge를 되돌린다."""
    aborted = []
    if _git_path(root, "rebase-merge").exists() or _git_path(root, "rebase-apply").exists():
        _git(root, "rebase", "--abort")
        aborted.append("rebase")
    if _git_path(root, "MERGE_HEAD").exists():
        _git(root, "merge", "--abort")
        aborted.append("merge")
    return aborted


def read_heartbeat(root: Path) -> dict[str, Any] | None:
    try:
        loaded: dict[str, Any] = json.loads((root / HEARTBEAT_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded


def _clear_heartbeat(root: Path) -> None:
    (root / HEARTBEAT_PATH).unlink(missing_ok=True)


def _read_merge_lock(path: Path) -> dict[str, Any]:
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"job": "?", "at": 0}
    return loaded


def _acquire_merge_lock(root: Path, job_id: str, wait_s: float = 0.0) -> str | None:
    """lock을 잡으면 None. 다른 작업이 wait_s 동안 놓지 않으면 그 작업 ID."""
    path = _git_path(root, MERGE_LOCK, common=True)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = str(_read_merge_lock(path).get("job"))
            if holder == job_id:
                return None
            if time.monotonic() >= deadline:
                return holder
            time.sleep(1.0)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"job": job_id, "at": now()}, handle)
        return None


def _release_merge_lock(root: Path, job_id: str) -> bool:
    path = _git_path(root, MERGE_LOCK, common=True)
    if path.exists() and _read_merge_lock(path).get("job") == job_id:
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------- 실패 처리


def _record_failure(job: Job, signature: str, detail: str) -> str:
    previous = job.get("last_failure")
    job["attempts"] += 1
    job["last_failure"] = signature
    if previous == signature:
        _block(job, "REPEATED_FAILURE", f"같은 실패 2회 연속: {detail}")
    elif job["attempts"] >= MAX_ATTEMPTS:
        _block(job, "RETRY_LIMIT", f"검증 실패 {job['attempts']}회: {detail}")
    elif active_seconds(job, now()) > job["time"]["cap_s"] * SOFT_RATIO:
        _block(job, "TIMEOUT", f"소프트 상한 이후 재시도 필요: {detail}")
    else:
        _set_status(job, "RETRY_REQUIRED")
        _maybe_promote(job)
    return str(job["status"])


def _agent(name: str = "FACTORY_AGENT", default: str = "claude") -> str:
    agent = os.environ.get(name) or default
    if agent not in AGENTS:
        raise FactoryError(f"{name}={agent}: 지원하는 에이전트 도구는 {', '.join(AGENTS)}")
    return agent


API_KEY = "ANTHROPIC_API_KEY"
# 오케스트레이터가 구독으로 돌 때 dispatch가 키를 이 이름으로 넘긴다 (claude가 키를 쓰지 않게).
# API 키를 쓰는 리뷰어 프로세스에만 ANTHROPIC_API_KEY로 되돌려 준다. 값은 출력·기록하지 않는다
API_KEY_STASH = "FACTORY_ANTHROPIC_API_KEY_STASH"


def stage_model(root: Path, stage: str, job: Job) -> dict[str, str]:
    """단계 모델: 환경변수(FACTORY_MODEL_<단계>) > 설정(stages) > 자동 규칙.

    반환 {"alias", "model", "source": env|config|auto, "reason"}. Claude만 단계 설정을 쓴다.
    """
    agent = job.get("agent", "claude")
    if agent != "claude":
        name = MODELS[agent]["builder" if stage == "builder" else "reviewer"]
        return {"alias": name, "model": name, "source": "auto", "reason": f"{agent} 기본"}
    override = os.environ.get(f"FACTORY_MODEL_{stage.upper()}")
    if override:
        if override not in CLAUDE_MODELS:
            raise FactoryError(
                f"FACTORY_MODEL_{stage.upper()}={override} (가능: {', '.join(CLAUDE_MODELS)})"
            )
        alias, source, reason = override, "env", "환경변수로 지정"
    elif _config(root).stages[stage]["model"] != "auto":
        alias, source, reason = _config(root).stages[stage]["model"], "config", "설정으로 지정"
    else:
        source = "auto"
        if stage == "builder" and job.get("attempts", 0) >= PROMOTE_AFTER:
            alias, reason = "opus", f"실패 {job.get('attempts', 0)}회 → 상위 모델로 승격"
        elif stage == "builder":
            alias, reason = "sonnet", "Builder 기본 (경량 시작)"
        elif stage == "reviewer_security" and job.get("risk") == "HIGH":
            alias, reason = "opus", "HIGH 위험의 보안 리뷰"
        elif stage == "orchestrator":
            alias, reason = "opus", "오케스트레이터 기본"
        elif stage == "planner":
            alias, reason = "opus", "계획자 기본 (기획안 6.9, 사람 결정)"
        else:
            alias, reason = "sonnet", "리뷰어 기본"
    return {"alias": alias, "model": CLAUDE_MODELS[alias], "source": source, "reason": reason}


def stage_auth(root: Path, stage: str) -> tuple[str, dict[str, str], tuple[str, ...]]:
    """리뷰어 프로세스의 인증: (방식, 넣을 환경변수, 지울 환경변수).

    auto는 지금 환경을 따른다 (ANTHROPIC_API_KEY가 있으면 API, 없으면 구독).
    """
    setting = _config(root).stages[stage]["auth"]
    key = os.environ.get(API_KEY) or os.environ.get(API_KEY_STASH)
    if setting == "api" and not key:
        raise FactoryError(f"{stage}: 인증이 api인데 API 키가 없습니다 ({API_KEY})")
    if setting == "api" or (setting == "auto" and os.environ.get(API_KEY)):
        add = {} if os.environ.get(API_KEY) else {API_KEY: str(key)}
        return "api", add, (API_KEY_STASH,)
    return "subscription", {}, (API_KEY, API_KEY_STASH)


def _maybe_promote(job: Job) -> None:
    """실패가 PROMOTE_AFTER회 쌓이면 다음 Builder를 상위 모델로 올린다 (기획안 6.6).

    사람이 모델을 정했으면(설정·환경변수) 바꾸지 않는다.
    """
    current = (job.get("models") or {}).get("builder") or {"source": "auto"}
    if current.get("source") != "auto" or job["attempts"] < PROMOTE_AFTER:
        return
    if job.get("agent", "claude") == "claude":
        strong = "opus"
        info = {"alias": strong, "model": CLAUDE_MODELS[strong], "source": "auto",
                "reason": f"실패 {job['attempts']}회 → 상위 모델로 승격"}  # fmt: skip
    else:
        strong = MODELS[job["agent"]]["builder_strong"]
        info = {"alias": strong, "model": strong, "source": "auto", "reason": "실패 누적 승격"}
    if job.get("builder_model") != strong:
        job["builder_model"] = strong
        job.setdefault("models", {})["builder"] = info
        job.setdefault("model_changes", []).append(
            {"attempt": job["attempts"], "model": strong, "reason": "실패 누적", "at": now()}
        )


def _builder_note(job: Job) -> str:
    if job["status"] != "RETRY_REQUIRED" or not job.get("builder_model"):
        return ""
    promoted = " (승격)" if job.get("model_changes") else ""
    return f" 다음 Builder 모델: {job['builder_model']}{promoted}."


def _check_time(job: Job) -> bool:
    """하드 상한을 넘었으면 BLOCKED(TIMEOUT)으로 바꾸고 False."""
    elapsed = active_seconds(job, now())
    if elapsed > job["time"]["cap_s"]:
        _block(
            job,
            "TIMEOUT",
            f"활성 시간 {_fmt_minutes(elapsed)}가 상한 {_fmt_minutes(job['time']['cap_s'])} 초과",
        )
        return False
    return True


# ---------------------------------------------------------------- spec


def parse_contract(text: str, job_id: str, cfg: Config) -> dict[str, Any]:
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
    if len(blocks) != 1:
        raise FactoryError("spec에 ```json contract 블록이 정확히 하나 있어야 합니다")
    try:
        contract: dict[str, Any] = json.loads(blocks[0])
    except json.JSONDecodeError as error:
        raise FactoryError(f"contract JSON 오류: {error}") from error
    errors = []
    if contract.get("type") not in TYPES:
        errors.append(f"type은 {sorted(TYPES)} 중 하나")
    if contract.get("size") not in SIZE_CAPS:
        errors.append(f"size는 {sorted(SIZE_CAPS)} 중 하나 (더 크면 분할)")
    if contract.get("risk") not in RISKS:
        errors.append(f"risk는 {RISKS} 중 하나")
    if not contract.get("acceptance_criteria"):
        errors.append("acceptance_criteria가 비어 있음")
    tests = contract.get("acceptance_tests") or []
    if not tests:
        errors.append("acceptance_tests가 비어 있음")
    if not cfg.acceptance_tests:
        errors.append("factory/config.json에 수락 테스트 경로 규칙(acceptance_tests)이 없음")
    for test in tests:
        if cfg.acceptance_tests and not cfg.is_acceptance_test(str(test), job_id):
            errors.append(f"수락 테스트 경로 규칙 위반: {test} ({cfg.acceptance_examples(job_id)})")
    for key in ("changed_tests", "allowed_config_changes"):
        if not isinstance(contract.get(key, []), list):
            errors.append(f"{key}는 목록")
    # 가정: 요구사항 해석이 갈린 지점과 고른 쪽. 없으면 []로 명시한다 (승인 요약 "판단할 것", H2)
    assumptions = contract.get("assumptions")
    if not isinstance(assumptions, list) or not all(
        isinstance(item, str) and item.strip() for item in assumptions
    ):
        errors.append("assumptions는 비어 있지 않은 문자열의 목록 (가정이 없으면 [])")
    # changed_tests는 명령 인자(형식·린트 확인)로 넘어가므로 기존 테스트 파일 경로만 받는다
    for test in contract.get("changed_tests") or []:
        if not cfg.changed_test.match(str(test)):
            errors.append(
                f"changed_tests 경로 규칙 위반: {test} (기존 테스트 파일만, config의 changed_test)"
            )
    if errors:
        raise FactoryError("contract 형식 오류:\n- " + "\n- ".join(errors))
    contract.setdefault("changed_tests", [])
    contract.setdefault("allowed_config_changes", [])
    return contract


# ---------------------------------------------------------------- 명령


def _exclude_factory_files(root: Path) -> None:
    """상태·리뷰 파일과 실행 캐시를 레포 설정과 관계없이 Git 추적에서 뺀다 (.git/info/exclude).

    merge가 작업 브랜치를 `git add -A`로 커밋하므로, .gitignore가 빠진 레포에서도 캐시가
    커밋되어 병렬 작업끼리 충돌하지 않게 한다.
    """
    exclude = root / _git(root, "rev-parse", "--git-path", "info/exclude").strip()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    wanted = [
        STATE_PATH,
        f"{STATE_PATH[:-5]}.tmp",
        SUMMARY_PATH,
        HEARTBEAT_PATH,
        f"{REVIEWS_DIR}/",
        ".pytest_tmp*/",
        "__pycache__/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".pytest_cache/",
    ]
    missing = [w for w in wanted if w not in current.splitlines()]
    if missing:
        prefix = "" if current.endswith("\n") or not current else "\n"
        block = "# AI SW Factory\n" + "\n".join(missing) + "\n"
        exclude.write_text(current + prefix + block, encoding="utf-8")


def _create_job_branch(root: Path, state: State) -> tuple[str, str]:
    """다음 job ID로 브랜치를 만든다.

    브랜치는 worktree끼리 공유되므로, 상태 파일의 작업 수가 아니라 이미 있는 factory/J*
    브랜치까지 보고 번호를 정한다. 다른 worktree가 동시에 같은 번호를 잡으면 다음 번호로 재시도한다.
    """
    used = {int(j[1:]) for j in state["jobs"] if re.fullmatch(r"J\d+", j)}
    for ref in _git(root, "branch", "--list", "factory/J*", "--format=%(refname:short)").split():
        if re.fullmatch(r"factory/J\d+", ref):
            used.add(int(ref.split("/J")[1]))
    number = max(used, default=0) + 1
    for _ in range(20):
        job_id = f"J{number:03d}"
        branch = f"factory/{job_id}"
        created = subprocess.run(
            ["git", "switch", "-q", "-c", branch], cwd=root, capture_output=True, check=False
        )
        if created.returncode == 0:
            return job_id, branch
        number += 1
    raise FactoryError("새 작업 브랜치를 만들지 못했습니다 (factory/J* 번호 충돌)")


def cmd_new(
    root: Path,
    state: State,
    requirement: str,
    base_branch: str | None = None,
    plan_ref: str | None = None,
    expected_risk: str | None = None,
) -> str:
    plan_id = plan_job = None
    if plan_ref is not None:
        # 계획 실행(dispatch.py --run-plan)이 넘긴다: "P001:A" (기획안 6.5, 6.9)
        found = re.fullmatch(r"(P\d{3,}):(\S+)", plan_ref)
        if not found or expected_risk not in RISKS:
            raise FactoryError(f"--plan은 P001:A 형식, --expected-risk는 {RISKS} 중 하나입니다")
        plan_id, plan_job = found.groups()
    active = state["active"]
    if active and state["jobs"][active]["status"] not in CLOSED_STATUSES:
        raise FactoryError(f"진행 중인 작업 {active}가 있습니다 (checkout 하나에 한 작업)")
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise FactoryError("작업 트리에 커밋되지 않은 변경이 있습니다")
    base = _git(root, "rev-parse", "HEAD").strip()
    if base_branch is None:
        base_branch = _git(root, "branch", "--show-current").strip()
        if not base_branch:
            raise FactoryError(
                "브랜치가 없는 checkout(detached HEAD)에서는 --base <브랜치>가 필요합니다"
            )
    elif _git(root, "rev-parse", base_branch).strip() != base:
        raise FactoryError(
            f"HEAD가 {base_branch}의 끝과 다릅니다. 최신 {base_branch}에서 시작하세요"
        )
    agent = _agent()
    job_id, branch = _create_job_branch(root, state)
    _exclude_factory_files(root)
    _clear_heartbeat(root)
    at = now()
    state["jobs"][job_id] = {
        "id": job_id,
        "requirement": requirement,
        "status": "QUEUED",
        "checkpoint": "QUEUED",
        "branch": branch,
        "base_commit": base,
        "base_branch": base_branch,
        "created_at": at,
        "spec_path": f"{SPECS_DIR}/{job_id}.md",
        "type": None,
        "size": None,
        "risk_declared": None,
        "risk": None,
        "risk_reasons": [],
        "acceptance_tests": [],
        "changed_tests": [],
        "allowed_config_changes": [],
        "assumptions": [],
        "contract_version": 0,
        "locks": {},
        "red": [],
        "validation": [],
        "reviews": [],
        "attempts": 0,
        "spec_revisions": 0,
        "last_failure": None,
        "approval": None,
        "approval_brief": None,
        "plan_id": plan_id,
        "plan_job": plan_job,
        "expected_risk": expected_risk if plan_id else None,
        "agent": agent,
        "builder_model": None,
        "models": {},
        "model_changes": [],
        "blocked": None,
        "time": {
            "segments": [[at, None]],
            "cap_s": SIZE_CAPS["small"],
            "extended": False,
            "active_s": 0.0,
        },
        "history": [{"status": "QUEUED", "at": at}],
    }
    job = state["jobs"][job_id]
    builder = stage_model(root, "builder", job)
    job["builder_model"], job["models"] = builder["alias"], {"builder": builder}
    job["budgets_applied"] = dict(_config(root).budgets)
    state["active"] = job_id
    return f"{job_id} 생성 (브랜치 {branch}). 다음: {SPECS_DIR}/{job_id}.md 작성 후 spec {job_id}"


def cmd_spec(root: Path, state: State, job_id: str) -> str:
    job = _job(state, job_id)
    _require(job, "QUEUED", "SPEC_READY")
    spec = root / job["spec_path"]
    if not spec.exists():
        raise FactoryError(f"{job['spec_path']}가 없습니다")
    contract = parse_contract(spec.read_text(encoding="utf-8"), job_id, _config(root))
    missing = [t for t in contract["acceptance_tests"] if not (root / t.split("::")[0]).exists()]
    if missing:
        raise FactoryError(f"수락 테스트 파일이 없습니다: {', '.join(missing)}")
    missing = [t for t in contract["changed_tests"] if not (root / t).is_file()]
    if missing:
        raise FactoryError(f"changed_tests 파일이 없습니다: {', '.join(missing)}")
    job.update(
        type=contract["type"],
        size=contract["size"],
        risk_declared=contract["risk"],
        acceptance_tests=contract["acceptance_tests"],
        changed_tests=contract["changed_tests"],
        allowed_config_changes=contract["allowed_config_changes"],
        assumptions=contract["assumptions"],
        contract=contract,
    )
    job["contract_version"] += 1
    if not job["time"]["extended"]:
        job["time"]["cap_s"] = SIZE_CAPS[contract["size"]]
    _set_status(job, "SPEC_READY")
    return f"{job_id}: SPEC_READY (contract v{job['contract_version']}). 다음: red {job_id}"


def cmd_red(root: Path, state: State, job_id: str) -> tuple[int, str]:
    job = _job(state, job_id)
    _require(job, "SPEC_READY")
    if not _check_time(job):
        return 1, f"{job_id}: BLOCKED (TIMEOUT)"
    # 잠근 뒤에는 고칠 수 없으므로 verify의 형식·린트에 걸릴 테스트는 지금 돌려보낸다
    style_ok, problems = run_style(root, [*job["acceptance_tests"], *job["changed_tests"]])
    if not style_ok:
        return 1, (
            f"{job_id}: 테스트 파일 형식·린트 문제 → SPEC_READY 유지 "
            "(스펙 재작성 횟수는 쓰지 않음). 고친 뒤 다시 red\n" + "\n".join(problems)
        )
    expect = "green" if job["type"] == "refactor" else "red"
    result = run_check_red(root, expect, job["acceptance_tests"])
    job["red"].append(
        {
            "expect": expect,
            "verdict": result["verdict"],
            "tests": result.get("tests", []),
            "at": now(),
            "contract_version": job["contract_version"],
        }
    )
    wanted = "GREEN_OK" if expect == "green" else "RED_OK"
    if result["verdict"] == wanted:
        job["locks"] = {rel: _sha(root / rel) for rel in _lock_targets(root, job)}
        _set_status(job, "BUILDING")
        return 0, (
            f"{job_id}: {wanted} → BUILDING. 파일 {len(job['locks'])}개 잠금. "
            f"다음: Builder 호출 (Builder 모델 {job.get('builder_model')}) 후 verify {job_id}"
        )
    job["spec_revisions"] += 1
    detail = f"{result['verdict']}: " + ", ".join(
        f"{t.get('id')}={t.get('outcome')}" for t in result.get("tests", [])
    )
    if job["spec_revisions"] > MAX_SPEC_REVISIONS:
        _block(job, "SPEC_INVALID", f"Red 확인 {job['spec_revisions']}회 실패. {detail}")
        return 1, f"{job_id}: BLOCKED (SPEC_INVALID). {detail}"
    _set_status(job, "SPEC_READY")
    return 1, (
        f"{job_id}: {result['verdict']} → SPEC_READY (스펙 재작성 "
        f"{job['spec_revisions']}/{MAX_SPEC_REVISIONS}회 사용). {detail}"
    )


def _failed_lines(output: str) -> list[str]:
    return sorted(
        {
            ln.strip()
            for ln in output.splitlines()
            if re.search(r"\bFAIL\b", ln) and not ln.strip().startswith("verify:")
        }
    )


CONFLICT_MARKER = re.compile(rb"^(<{7}|>{7})( |\r?$)", re.MULTILINE)


def _conflict_markers(root: Path, job: Job) -> list[str]:
    """작업 변경 중 충돌 표시(<<<<<<< / >>>>>>>)가 남은 파일."""
    found = []
    for rel in sorted(_changed_paths(root, _fork_point(root, job))):
        path = root / rel
        skip = not path.is_file() or _matches(rel, TREE_EXCLUDE)
        if not skip and CONFLICT_MARKER.search(path.read_bytes()):
            found.append(rel)
    return found


def cmd_verify(root: Path, state: State, job_id: str) -> tuple[int, str]:
    job = _job(state, job_id)
    _require(job, "BUILDING", "RETRY_REQUIRED")
    if not _check_time(job):
        return 1, f"{job_id}: BLOCKED (TIMEOUT)"
    changed = _changed_locks(root, job)
    if changed:
        _block(job, "PROTECTED_FILE_CHANGED", "잠긴 파일 변경: " + ", ".join(changed))
        return 1, f"{job_id}: BLOCKED (PROTECTED_FILE_CHANGED): {', '.join(changed)}"
    markers = _conflict_markers(root, job)
    if markers:
        signature = f"충돌 표시 남음: {', '.join(markers)}"
        status = _record_failure(job, signature, signature)
        return 1, f"{job_id}: {status}.{_builder_note(job)} {signature} (<<<<<<< ~ >>>>>>> 정리)"

    green = run_check_red(root, "green", job["acceptance_tests"])
    verify_args = job_verify_args(root, job)
    verify_exit, verify_output = run_verify(root, verify_args)
    risk, reasons = compute_risk(root, job)
    job["risk"], job["risk_reasons"] = risk, reasons
    record = {
        "kind": "verify",
        "attempt": job["attempts"] + 1,
        "green_verdict": green["verdict"],
        "green_tests": green.get("tests", []),
        "verify_exit": verify_exit,
        "verify_args": verify_args,
        "verify_tail": verify_output.strip()[-2500:],
        "risk": risk,
        "at": now(),
    }
    job["validation"].append(record)
    if green["verdict"] == "GREEN_OK" and verify_exit == 0:
        job["verified_tree"] = tree_fingerprint(root, job)
        _set_status(job, "VERIFYING")
        return 0, f"{job_id}: 검증 통과 → VERIFYING (위험 {risk}). 다음: validate {job_id}"

    problems = []
    if green["verdict"] != "GREEN_OK":
        failing = [t.get("id") for t in green.get("tests", []) if t.get("outcome") != "passed"]
        problems.append(f"수락 테스트 {green['verdict']}: {', '.join(map(str, failing))}")
    if verify_exit != 0:
        problems.append("verify 실패: " + "; ".join(_failed_lines(verify_output)))
    signature = " | ".join(problems)
    status = _record_failure(job, signature, signature)
    note = _builder_note(job)
    return 1, f"{job_id}: {status}.{note} {signature}\n{verify_output.strip()[-2500:]}"


DEFAULT_VALIDATOR_INSTRUCTIONS = """너는 AI SW Factory의 리뷰어다.
구현자의 대화는 보지 못하고 아래 자료만 본다.
필요하면 Read/Glob/Grep으로 레포 파일을 확인한다. 파일을 고치지 않는다.
아래 '리뷰어' 절의 관점에서만 판정한다. 그 관점의 문제가 아니면 FAIL로 하지 않는다.
마지막 줄에 JSON 한 줄로만 판정한다: {"verdict": "PASS" 또는 "FAIL", "issues": ["..."],
"notes_for_human": ["..."], "risk_up": null 또는 {"to": "MEDIUM"|"HIGH", "reason": "..."}}
"""

DEFAULT_REVIEWER_FOCUS = {
    "code": "diff가 contract의 acceptance_criteria를 실제로 충족하는가, 범위 밖 변경이 없는가, "
    "명백한 버그가 없는가.",
    "security": "인증·인가 우회, 입력 검증 누락, 비밀정보 노출, 위험한 명령·쿼리가 없는가. "
    "선언된 위험 등급이 실제 변경보다 낮지 않은가.",
    "test": "수락 테스트가 기준을 제대로 확인하는가, skip·xfail·assertion 약화·하드코딩 같은 "
    "우회가 없는가.",
}


def _instructions(root: Path, rel: str, default: str) -> str:
    # 얇은 설치 레포에는 리뷰어 지시문이 없고 엔진에만 있다
    for path in (root / rel, ENGINE_ROOT / rel):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return default


def build_validator_prompt(root: Path, job: Job, reviewer: str = "code") -> str:
    instructions = _instructions(root, VALIDATOR_PROMPT, DEFAULT_VALIDATOR_INSTRUCTIONS)
    focus = _instructions(
        root, REVIEWER_PROMPT.format(name=reviewer), DEFAULT_REVIEWER_FOCUS[reviewer]
    )
    diff_text, _ = _diff(root, job["base_commit"])
    last = job["validation"][-1] if job["validation"] else {}
    red = job["red"][-1] if job["red"] else {}
    return "\n\n".join(
        [
            instructions,
            f"## 리뷰어: {reviewer}\n{focus}",
            f"## 요구사항\n{job['requirement']}",
            "## contract\n```json\n"
            + json.dumps(job.get("contract", {}), ensure_ascii=False, indent=2)
            + "\n```",
            "## Red 확인 기록\n" + json.dumps(red, ensure_ascii=False),
            "## 최근 검증 기록\n"
            + json.dumps(
                {k: last.get(k) for k in ("green_verdict", "verify_exit", "risk")},
                ensure_ascii=False,
            )
            + f"\n위험 판단 근거: {job['risk_reasons']}",
            "## diff (기준 커밋 대비)\n```diff\n" + diff_text[:60000] + "\n```",
        ]
    )


# 리뷰어 판정 형식. Claude는 --json-schema로 CLI가 강제하고, Codex는 지시문으로 같은 형식을 낸다
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        # PASS여도 사람이 알아야 할 점 (승인 요약 "판단할 것", H3)
        "notes_for_human": {"type": "array", "items": {"type": "string"}},
        # 위험 등급 상향 제안. 올리는 방향만 반영한다 (H4)
        "risk_up": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "enum": ["MEDIUM", "HIGH"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["to", "reason"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": ["verdict", "issues", "notes_for_human", "risk_up"],
    "additionalProperties": False,
}


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """출력에서 마지막 판정 JSON을 찾는다. 중첩 객체(risk_up)가 있어도 읽는다."""
    decoder = json.JSONDecoder()
    for start in reversed([i for i, ch in enumerate(text) if ch == "{"]):
        try:
            parsed, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("verdict") in {"PASS", "FAIL"}:
            notes = parsed.get("notes_for_human")
            parsed["notes_for_human"] = (
                [n for n in notes if isinstance(n, str) and n.strip()]
                if isinstance(notes, list)
                else []
            )
            up = parsed.get("risk_up")
            valid = isinstance(up, dict) and up.get("to") in ("MEDIUM", "HIGH")
            parsed["risk_up"] = (
                {"to": up["to"], "reason": str(up.get("reason", ""))} if valid else None
            )
            return parsed
    return None


def reviewer_budget(job: Job) -> str:
    """리뷰어 호출 하나의 비용 상한: 환경변수 > 작업에 고정된 상한(new 때) > 기본."""
    env = os.environ.get("FACTORY_REVIEW_BUDGET_USD")
    if env:
        return env
    applied = (job.get("budgets_applied") or {}).get("reviewer_call")
    return str(applied if applied is not None else REVIEW_BUDGET_USD)


def reviewer_command(
    agent: str, out_file: Path, model: str | None = None, budget: str | None = None
) -> list[str]:
    """읽기 전용 리뷰어 프로세스. 프롬프트는 stdin으로 넘긴다."""
    model = model or MODELS[agent]["reviewer"]
    if agent == "codex":
        return [
            "codex",
            "exec",
            "--model",
            model,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--output-last-message",
            str(out_file),
            # dispatch.py와 같은 추가 인자 (예: 모델 제공자 지정)
            *[str(a) for a in json.loads(os.environ.get("FACTORY_CODEX_ARGS", "[]"))],
            "-",
        ]
    return [
        "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--max-budget-usd",
        budget or reviewer_budget({}),
        "--json-schema",
        json.dumps(REVIEW_SCHEMA),
        "--allowedTools",
        "Read",
        "Glob",
        "Grep",
    ]


def _usage(envelope: dict[str, Any]) -> dict[str, Any]:
    """claude -p 결과의 비용(API 환산)과 토큰 수."""
    usage = envelope.get("usage") or {}
    tokens = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                  "output_tokens")
    )  # fmt: skip
    detail = {
        "in": int(usage.get("input_tokens") or 0),
        "out": int(usage.get("output_tokens") or 0),
        "cr": int(usage.get("cache_read_input_tokens") or 0),
        "cw": int(usage.get("cache_creation_input_tokens") or 0),
    }
    return {"cost_usd": envelope.get("total_cost_usd"), "tokens": tokens, "usage": detail}


def _reviewer_plan(root: Path, job: Job, reviewer: str) -> dict[str, Any]:
    """리뷰어 하나의 모델·인증. 리뷰어를 띄우기 전에 전부 정한다 (키가 없으면 아무것도 안 띄움)."""
    agent = _agent("FACTORY_REVIEW_AGENT", job.get("agent", "claude"))
    stage = f"reviewer_{reviewer}"
    info = stage_model(root, stage, {**job, "agent": agent})
    if agent == "claude":
        auth, add, unset = stage_auth(root, stage)
    else:
        auth, add, unset = "codex", {}, (API_KEY_STASH,)
    return {"agent": agent, "info": {**info, "auth": auth}, "add": add, "unset": unset}


def _run_reviewer(
    root: Path, job: Job, reviewer: str, path: Path, plan: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    """리뷰어 하나를 실행하고 (최종 응답, stderr, 비용·토큰)을 돌려준다."""
    agent = plan["agent"]
    last_message = path.with_suffix(".last")
    cmd = _command(
        "validator",
        reviewer_command(agent, last_message, plan["info"]["model"], reviewer_budget(job)),
    )
    proc = _run(
        cmd,
        root,
        stdin=build_validator_prompt(root, job, reviewer),
        env={"FACTORY_REVIEWER": reviewer, **plan["add"]},
        unset=plan["unset"],
    )
    text = proc.stdout
    usage: dict[str, Any] = {"cost_usd": None, "tokens": None, "usage": None}
    if last_message.exists():  # codex exec --output-last-message (토큰은 기록하지 않음)
        text = last_message.read_text(encoding="utf-8")
        last_message.unlink()
    else:
        try:
            envelope = json.loads(proc.stdout)
            if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
                text = envelope["result"]  # claude -p --output-format json
                usage = _usage(envelope)
                structured = envelope.get("structured_output")
                if isinstance(structured, dict):  # --json-schema: CLI가 형식을 확인한 판정
                    text += "\n" + json.dumps(structured, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return text, proc.stderr, usage


def judgement_lines(job: Job) -> list[str]:
    """사람이 코드를 읽지 않고 판단할 논점 (H3): spec의 가정, 리뷰어 의견, 등급 상향 제안."""
    assumptions = job.get("assumptions") or []
    lines = [f"- 가정: {item}" for item in assumptions] or [
        "- 가정 없음 (spec에 AI가 혼자 정한 해석이 없다고 적음)"
    ]
    last_round = max((int(r.get("round", 0)) for r in job["reviews"]), default=0)
    for review in job["reviews"]:
        if int(review.get("round", 0)) != last_round:
            continue
        lines += [
            f"- [{review['reviewer']}] {note}" for note in review.get("notes_for_human") or []
        ]
        if review.get("risk_up"):
            up = review["risk_up"]
            lines.append(f"- [{review['reviewer']}] 위험 등급 {up['to']}로 상향: {up['reason']}")
    return lines


def screenshot_paths(root: Path, patterns: list[str], limit: int = 10) -> list[str]:
    """사용자 흐름 테스트가 남긴 스크린샷 (레포 기준 경로, 최대 limit개)."""
    found = {
        path.relative_to(root).as_posix()
        for pattern in patterns
        for path in root.glob(pattern)
        if path.is_file()
    }
    return sorted(found)[:limit]


def _screenshot_lines(root: Path, cfg: Config) -> list[str]:
    if not cfg.screenshots:
        return []
    shots = screenshot_paths(root, cfg.screenshots)
    body = [f"- {s}" for s in shots] or ["- 스크린샷 없음 (사용자 흐름 테스트가 남긴 파일이 없음)"]
    return ["## 화면 스크린샷", *body, ""]


def write_approval_brief(root: Path, job: Job) -> str:
    """사람 승인용 요약 (Approver). 승인 여부는 사람이 정한다."""
    _, files = _diff(root, job["base_commit"])
    reviews = [r for r in job["reviews"] if r.get("round") == job["reviews"][-1].get("round")]
    lines = [
        f"# {job['id']} 승인 요청",
        "",
        f"- 요구사항: {job['requirement']}",
        f"- 위험 등급: **{job['risk']}** (선언 {job.get('risk_declared')})",
        *[f"  - {reason}" for reason in job["risk_reasons"]],
        f"- 활성 시간: {_fmt_minutes(active_seconds(job, now()))}, 재시도 {job['attempts']}회",
        "",
        "## 판단할 것",
        *judgement_lines(job),
        "",
        "## 리뷰 결과",
        *[f"- {r['reviewer']}: {r['verdict']} ({r['path']})" for r in reviews],
        "",
        "## 바뀐 파일",
        *[f"- {path} (+{len(a)} -{len(d)})" for path, (a, d) in sorted(files.items())],
        "",
        *_screenshot_lines(root, _config(root)),
        "## 사람의 다음 명령",
        "dispatch로 만든 작업은 기본 checkout에서 실행한다 (작업 worktree의 scripts/와 상태 "
        "파일은 세션이 바꿀 수 있다):",
        f"- 승인하고 머지: `python scripts/dispatch.py --approve {job['id']}`",
        f'- 반려: `python scripts/dispatch.py --reject {job["id"]} --reason "<이유>"`',
        "대화형 세션(기본 checkout)의 작업:",
        f"- 승인하고 머지: `python scripts/factory.py approve {job['id']}`",
        f'- 반려: `python scripts/factory.py approve {job["id"]} --reject "<이유>"`',
        f"- 중단: `python scripts/factory.py drop {job['id']}`",
    ]
    rel = f"{REVIEWS_DIR}/{job['id']}-approval.md"
    (root / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rel


def cmd_validate(root: Path, state: State, job_id: str) -> tuple[int, str]:
    job = _job(state, job_id)
    _require(job, "VERIFYING")
    if not _check_time(job):
        return 1, f"{job_id}: BLOCKED (TIMEOUT)"
    if tree_fingerprint(root, job) != job.get("verified_tree"):
        _set_status(job, "BUILDING")
        return (
            1,
            f"{job_id}: verify 이후 작업 트리가 바뀌었습니다 → BUILDING. 다음: verify {job_id}",
        )
    (root / REVIEWS_DIR).mkdir(parents=True, exist_ok=True)
    round_no = 1 + max((int(r.get("round", 0)) for r in job["reviews"]), default=0)
    paths = {name: root / REVIEWS_DIR / f"{job_id}-{round_no}-{name}.txt" for name in REVIEWERS}
    spent = sum(float(r.get("cost_usd") or 0) for r in job["reviews"])
    limit = float(
        (job.get("budgets_applied") or {}).get("reviewer_total")
        or _config(root).budgets["reviewer_total"]
    )
    if spent >= limit:
        # 실제로 쓴 합계로만 막는다 (다음 회차 최대치로 미리 막으면 재시도 경로가 닫힌다)
        _block(job, "BUDGET", f"리뷰어 합계 ${spent:.2f}가 상한 ${limit:.2f}에 닿음")
        return 1, f"{job_id}: BLOCKED (BUDGET). 리뷰어 합계 ${spent:.2f} / ${limit:.2f}"
    plans = {name: _reviewer_plan(root, job, name) for name in REVIEWERS}
    models = job.setdefault("models", {})
    for name in REVIEWERS:
        models[f"reviewer_{name}"] = plans[name]["info"]
    with ThreadPoolExecutor(max_workers=len(REVIEWERS)) as pool:
        outputs = dict(
            zip(
                REVIEWERS,
                pool.map(
                    lambda name: _run_reviewer(root, job, name, paths[name], plans[name]),
                    REVIEWERS,
                ),
                strict=True,
            )
        )
    verdicts: dict[str, dict[str, Any] | None] = {}
    for name in REVIEWERS:
        text, stderr, usage = outputs[name]
        paths[name].write_text(text + "\n\n[stderr]\n" + stderr, encoding="utf-8")
        verdict = _parse_verdict(text)
        verdicts[name] = verdict
        job["reviews"].append(
            {
                "round": round_no,
                "reviewer": name,
                "path": paths[name].relative_to(root).as_posix(),
                "verdict": verdict["verdict"] if verdict else None,
                "issues": verdict.get("issues", []) if verdict else [],
                "notes_for_human": verdict["notes_for_human"] if verdict else [],
                "risk_up": verdict["risk_up"] if verdict else None,
                **usage,
                "at": now(),
            }
        )
    unreadable = [name for name, v in verdicts.items() if v is None]
    if unreadable:
        raise FactoryError(
            f"리뷰어 {', '.join(unreadable)}의 출력에서 판정 JSON을 찾지 못했습니다 (상태 유지). "
            f"원문: {REVIEWS_DIR}/{job_id}-{round_no}-*.txt"
        )
    issues = [
        f"[{name}] {issue}"
        for name, verdict in verdicts.items()
        if verdict and verdict["verdict"] == "FAIL"
        for issue in (verdict.get("issues") or ["사유 없음"])
    ]
    passed = not issues
    job["validation"].append(
        {
            "kind": "validator",
            "attempt": job["attempts"] + 1,
            "verdict": "PASS" if passed else "FAIL",
            "reviewers": {name: v["verdict"] for name, v in verdicts.items() if v},
            "issues": issues,
            "at": now(),
        }
    )
    if passed:
        # 리뷰어의 등급 상향 제안은 받아들인다. 낮추는 제안은 형식상 받지 않는다 (H4, 기획안 8.2)
        for name, v in verdicts.items():
            up = v["risk_up"] if v else None
            if up and RISKS.index(up["to"]) > RISKS.index(job["risk"]):
                job["risk"] = up["to"]
                job["risk_reasons"].append(f"{up['to']}: 리뷰어 {name} 상향 제안 ({up['reason']})")
        # Approver: 승인 결정은 규칙으로 한다. 모델은 안전 판정을 뒤집을 수 없다 (기획안 8.2)
        _stop_clock(job, now())
        if job["risk"] in AUTO_MERGE_RISKS:
            job["approval"] = {
                "by": "auto",
                "decision": "approved",
                "reason": f"위험 {job['risk']} 자동 승인, 리뷰어 전원 PASS (기획안 8.1)",
                "at": now(),
            }
            _set_status(job, "APPROVED")
            return 0, (
                f"{job_id}: 리뷰어 {len(REVIEWERS)}명 PASS → APPROVED (위험 {job['risk']}, "
                f"자동 승인). 다음: merge {job_id}"
            )
        job["approval_brief"] = write_approval_brief(root, job)
        _set_status(job, "APPROVAL_REQUIRED")
        return 0, (
            f"{job_id}: 리뷰어 {len(REVIEWERS)}명 PASS → APPROVAL_REQUIRED (위험 {job['risk']}). "
            f"사람의 승인을 기다립니다. 승인 요약: {job['approval_brief']}"
        )
    summary = "; ".join(issues)
    status = _record_failure(job, f"validator: {summary}", f"리뷰 FAIL: {summary}")
    return 1, f"{job_id}: 리뷰 FAIL → {status}.{_builder_note(job)} {summary}"


def cmd_block(state: State, job_id: str, reason: str, detail: str) -> str:
    job = _job(state, job_id)
    _require(job, *sorted(ACTIVE_STATUSES))
    _block(job, reason, detail)
    return f"{job_id}: BLOCKED ({reason})"


def _commit_job(root: Path, job: Job, message: str) -> str | None:
    """작업 브랜치에 남은 변경을 커밋한다. 커밋할 것이 없으면 None."""
    _git(root, "add", "-A")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root, check=False)
    if staged.returncode != 1:
        return None
    _git(root, *_identity(root), "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _worktrees(root: Path) -> list[tuple[Path, str | None]]:
    """(경로, checkout된 브랜치) 목록. 브랜치가 없으면(detached) None."""
    entries: list[tuple[Path, str | None]] = []
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            entries.append((Path(line[len("worktree ") :]), None))
        elif line.startswith("branch ") and entries:
            entries[-1] = (entries[-1][0], line[len("branch ") :].removeprefix("refs/heads/"))
    return entries


def _base_checkout(root: Path, job: Job) -> Path | None:
    """base 브랜치가 다른 worktree에 checkout되어 있으면 그 경로 (병렬 실행, Phase 3)."""
    here = root.resolve()
    for path, branch in _worktrees(root):
        if branch == job["base_branch"] and path.resolve() != here:
            return path
    return None


def _return_to_base(root: Path, job: Job) -> None:
    """base가 다른 worktree에 있으면 작업 브랜치에 그대로 남는다."""
    if _base_checkout(root, job) is not None:
        return
    if _git(root, "branch", "--show-current").strip() != job["base_branch"]:
        _git(root, "switch", "-q", job["base_branch"])


def _commit_and_return(root: Path, job: Job, message: str) -> str | None:
    """작업 브랜치에 결과를 커밋하고 원래 브랜치로 돌아간다. 사람이 실행한 명령에서만 호출된다."""
    commit = _commit_job(root, job, message)
    _return_to_base(root, job)
    return commit


def _close_job(root: Path, state: State, job: Job, status: str) -> None:
    _set_status(job, status)
    _stop_clock(job, now())
    _clear_heartbeat(root)
    if state["active"] == job["id"]:
        state["active"] = None


def _is_merged(root: Path, job: Job) -> bool:
    """작업 브랜치에 커밋이 있고, 그 끝이 base 브랜치에 들어가 있는가 (Git으로 확인)."""
    tip = _git(root, "rev-parse", job["branch"]).strip()
    return tip != job["base_commit"] and _git_ok(
        root, "merge-base", "--is-ancestor", tip, job["base_branch"]
    )


def _mark_merged(root: Path, state: State, job: Job) -> None:
    job["merge_commit"] = _git(root, "rev-parse", job["base_branch"]).strip()
    _close_job(root, state, job, "MERGED")


def _replay_from_spec(root: Path, job: Job, head: str, files: list[str]) -> tuple[int, str]:
    """작업이 바꾼 기존 테스트(changed_tests)가 충돌: 새 base 위에서 Spec 단계부터 다시 한다.

    잠긴 테스트는 Builder가 고칠 수 없다. 구현과 기존 테스트 변경을 빼고 spec·수락 테스트만
    새 base 위에 남긴다. 오케스트레이터가 기존 테스트를 새 base 기준으로 다시 바꾸고 spec → red
    (수락 테스트 Red를 새 base에서 다시 확인) → Builder 순서로 간다. 한 작업에 한 번만.
    """
    job_id, base = job["id"], job["base_branch"]
    _git(root, "reset", "-q", "--hard", base)
    own = [job["spec_path"], *(t.split("::")[0] for t in job["acceptance_tests"])]
    _git(root, "checkout", head, "--", *own)
    _git(root, "reset", "-q")  # 작업 파일은 커밋하지 않은 새 파일로 둔다
    onto = _git(root, "rev-parse", base).strip()
    job.setdefault("replays", []).append(
        {"from": head, "onto": onto, "files": files, "mode": "spec", "at": now()}
    )
    job.update(approval=None, verified_tree=None, locks={})
    job["last_failure"] = f"CONFLICT (spec): {', '.join(files)}"
    _set_status(job, "SPEC_READY")
    _start_clock(job, now())
    _clear_heartbeat(root)
    listed = ", ".join(files)
    diffs = " / ".join(f"git diff {onto[:7]} {head[:7]} -- {f}" for f in files)
    return 1, (
        f"{job_id}: SPEC_READY (CONFLICT). 이 작업이 바꾼 기존 테스트({listed})가 {base}에 먼저 "
        f"머지된 변경과 충돌했습니다. {base} 끝({onto[:7]}) 위에서 Spec 단계부터 다시 합니다: "
        f"spec·수락 테스트는 남기고 구현과 기존 테스트 변경은 뺐습니다. 이전에 바꾼 내용: {diffs}. "
        f"기존 테스트를 새 base 기준으로 다시 바꾼 뒤 spec {job_id} → red {job_id} → Builder."
    )


def _replay_on_base(root: Path, job: Job, rebase_output: str) -> tuple[int, str]:
    """rebase 충돌: 작업 변경을 base 끝 위에 다시 펼치고 Builder가 충돌을 풀게 한다 (기획안 8.3).

    충돌 파일에는 충돌 표시가 남고, 변경은 커밋하지 않은 채 작업 트리에 둔다. 승인·검증 기록은
    무효가 되어 verify부터 다시 한다. 잠긴 파일이 충돌하면 Builder가 고칠 수 없으므로 작업
    브랜치를 되돌리고 BLOCKED(CONFLICT)로 사람에게 넘긴다.
    """
    job_id, base = job["id"], job["base_branch"]
    head = _git(root, "rev-parse", "HEAD").strip()
    fork = _git(root, "merge-base", head, base).strip()
    _git(root, "reset", "-q", "--hard", base)
    subprocess.run(
        ["git", "merge", "-q", "--squash", head], cwd=root, capture_output=True, check=False
    )
    files = sorted(
        n for n in _git(root, "diff", "--name-only", "--diff-filter=U").splitlines() if n
    )
    locked = [f for f in files if f in job["locks"]]
    own_tests = bool(locked) and set(locked) <= set(job["changed_tests"])
    spec_replays = [r for r in job.get("replays", []) if r.get("mode") == "spec"]
    if own_tests and not spec_replays:
        return _replay_from_spec(root, job, head, locked)
    if locked or not files:
        _git(root, "reset", "-q", "--hard", head)
        detail = f"잠긴 파일 충돌: {', '.join(locked)}" if locked else "rebase 충돌"
        _block(job, "CONFLICT", f"{base} 기준 {detail}")
        return 1, f"{job_id}: BLOCKED (CONFLICT). {detail}\n{rebase_output}"
    _git(root, "reset", "-q")  # 충돌 색인을 비운다. 변경은 작업 트리에만 남는다
    # base가 깔끔하게 바꾼 잠긴 파일은 작업의 변경이 아니다. 지금 내용으로 다시 잠근다
    cfg = _config(root)
    landed = _git(root, "diff", "--name-only", "--diff-filter=A", fork, base).splitlines()
    relock = set(_lock_targets(root, job)) | {
        p for p in landed if _matches(p, cfg.no_new_file_patterns)
    }
    job["locks"] = {rel: _sha(root / rel) for rel in sorted(relock) if (root / rel).is_file()}
    onto = _git(root, "rev-parse", base).strip()
    job.setdefault("replays", []).append({"from": head, "onto": onto, "files": files, "at": now()})
    job["approval"] = None
    job["verified_tree"] = None
    _clear_heartbeat(root)
    listed = ", ".join(files)
    status = _record_failure(
        job, f"CONFLICT: {listed}", f"{base}에 먼저 머지된 변경과 충돌: {listed}"
    )
    if status == "RETRY_REQUIRED":
        _start_clock(job, now())
    return 1, (
        f"{job_id}: {status} (CONFLICT). {base}에 먼저 머지된 변경과 겹쳐, 작업 변경을 {base} 끝"
        f"({onto[:7]}) 위에 다시 펼쳤습니다. 충돌 표시(<<<<<<<)가 남은 파일: {listed}. "
        f"Builder에게 두 변경을 모두 살려 충돌을 풀게 한 뒤 verify {job_id}부터 다시."
        f"{_builder_note(job)}"
    )


def _merge_steps(root: Path, state: State, job: Job, approved_now: bool) -> tuple[int, str]:
    job_id = job["id"]
    if _git(root, "branch", "--show-current").strip() != job["branch"]:
        _git(root, "switch", "-q", job["branch"])
    target = _base_checkout(root, job)
    if target is not None and _git(target, "status", "--porcelain", "--untracked-files=no").strip():
        return 1, (
            f"{job_id}: {job['base_branch']} checkout({target})에 커밋되지 않은 변경이 있어 "
            "머지하지 않습니다 (상태 유지). 정리한 뒤 다시 merge"
        )
    if tree_fingerprint(root, job) != job.get("verified_tree"):
        _block(job, "TREE_CHANGED", "검증·승인 이후 작업 트리가 바뀌었습니다")
        return 1, f"{job_id}: BLOCKED (TREE_CHANGED). 검증한 변경과 머지할 변경이 다릅니다"
    approval = job["approval"]
    # 상태 파일을 믿지 않을 때는 지금 이 프로세스에서 실행한 approve만 사람 승인으로 본다
    # (세션이 상태에 "사람 승인"을 써 넣을 수 있다)
    auto = approval["by"] == "auto" or (_untrusted() and not approved_now)
    if auto:
        risk, reasons = compute_risk(root, job)
        if risk not in AUTO_MERGE_RISKS:
            job["risk"], job["risk_reasons"] = risk, reasons
            job["approval"] = None
            _set_status(job, "APPROVAL_REQUIRED")
            return (
                1,
                f"{job_id}: 머지 전 재계산한 위험이 {risk}라 자동 머지하지 않습니다 → 사람 승인",
            )
    untrusted = _untrusted()
    if untrusted:
        cfg = _config(root)
        changed = sorted(_changed_paths(root, _fork_point(root, job)))
        locked = [
            p
            for p in changed
            if _matches(p, cfg.lock_patterns) and not cfg.is_acceptance_test(p, job_id)
        ]
        if locked and auto:
            risk, reasons = compute_risk(root, job)
            job["risk"] = RISKS[max(RISKS.index(risk), RISKS.index("MEDIUM"))]
            job["risk_reasons"] = [*reasons, *[f"MEDIUM: 잠긴 파일 변경 ({p})" for p in locked]]
            job["approval"] = None
            _set_status(job, "APPROVAL_REQUIRED")
            return 1, f"{job_id}: 잠긴 파일이 바뀌어 자동 머지하지 않습니다 → 사람 승인 ({locked})"
        # 수락 테스트 목록은 상태가 아니라 diff에서도 찾는다 (상태 목록을 비우는 조작 대비)
        acceptance = sorted(
            {p for p in changed if cfg.is_acceptance_test(p, job_id) and (root / p).exists()}
            | set(job["acceptance_tests"])
        )
        if not acceptance:
            _block(job, "MERGE_CHECK_FAILED", "수락 테스트가 없습니다 (diff와 상태 모두)")
            return 1, f"{job_id}: BLOCKED (MERGE_CHECK_FAILED). 수락 테스트 없음"
        job["acceptance_tests"] = acceptance
    summary = f"위험 {job['risk']}, 승인 {'auto' if auto else approval['by']}"
    job["commit"] = (
        _commit_job(root, job, f"factory {job_id}: {job['requirement']}\n\n{summary}")
        or _git(root, "rev-parse", "HEAD").strip()
    )

    rebased = not _git_ok(root, "merge-base", "--is-ancestor", job["base_branch"], "HEAD")
    if rebased:
        rebase = subprocess.run(
            ["git", *_identity(root), "rebase", "-q", job["base_branch"]],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if rebase.returncode != 0:
            _abort_git_operations(root)
            return _replay_on_base(root, job, rebase.stdout.strip()[-800:])
    if rebased or untrusted:
        why = "rebase" if rebased else "untrusted"
        green = run_check_red(root, "green", job["acceptance_tests"])
        verify_exit, verify_output = run_verify(root, job_verify_args(root, job))
        job["merge_checks"] = job.get("merge_checks", []) + [
            {"green_verdict": green["verdict"], "verify_exit": verify_exit, "why": why, "at": now()}
        ]
        if green["verdict"] != "GREEN_OK" or verify_exit != 0:
            _block(
                job,
                "MERGE_CHECK_FAILED",
                f"머지 전 재검증({why}) 실패 (수락 테스트 {green['verdict']}, "
                f"verify exit {verify_exit})",
            )
            return 1, f"{job_id}: BLOCKED (MERGE_CHECK_FAILED)\n{verify_output.strip()[-1500:]}"
    if rebased:
        job["verified_tree"] = tree_fingerprint(root, job)
        job["commit"] = _git(root, "rev-parse", "HEAD").strip()

    # base가 다른 worktree(기본 checkout)에 있으면 그곳에서 머지한다. 작업 worktree는 그대로 둔다
    merge_root = target or root
    if target is None:
        _git(root, "switch", "-q", job["base_branch"])
    merged = subprocess.run(
        [
            "git",
            *_identity(root),
            "merge",
            "-q",
            "--no-ff",
            "-m",
            f"factory {job_id} 머지: {job['requirement']}\n\n{summary}",
            job["branch"],
        ],
        cwd=merge_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if merged.returncode != 0 or not _is_merged(root, job):
        _abort_git_operations(merge_root)
        if target is None:
            _git(root, "switch", "-q", job["branch"])
        _block(job, "CONFLICT", f"merge 실패: {merged.stderr.strip()[-300:]}")
        return 1, f"{job_id}: BLOCKED (CONFLICT). merge 실패"
    _mark_merged(root, state, job)
    return 0, (
        f"{job_id}: MERGED → {job['base_branch']} "
        f"(머지 커밋 {job['merge_commit'][:7]}, {summary}). "
        f"되돌리기: git revert -m 1 {job['merge_commit'][:7]}"
    )


def cmd_merge(root: Path, state: State, job_id: str, approved_now: bool = False) -> tuple[int, str]:
    """승인된 작업을 base 브랜치에 머지한다 (기획안 8.3). 한 번에 하나씩 (merge lock)."""
    job = _job(state, job_id)
    _require(job, "APPROVED")
    if not job.get("approval") or job["approval"].get("decision") != "approved":
        raise FactoryError(f"{job_id}에 승인 기록이 없습니다")
    if os.environ.get("FACTORY_MERGE_ON_HOST") == "1":
        # 컨테이너 세션(Phase 3b)은 base 브랜치를 건드리지 않는다. 세션이 끝나면 호스트가 머지한다
        return 0, f"{job_id}: APPROVED. 컨테이너 세션이라 머지는 호스트가 합니다. 여기서 멈추세요"
    wait_s = float(os.environ.get("FACTORY_MERGE_WAIT_S", MERGE_WAIT_S))
    holder = _acquire_merge_lock(root, job_id, wait_s)
    if holder is not None:
        return 1, f"{job_id}: 다른 작업 {holder}가 머지 중입니다 (상태 유지). 끝난 뒤 다시 merge"
    try:
        return _merge_steps(root, state, job, approved_now)
    finally:
        _release_merge_lock(root, job_id)


def cmd_approve(root: Path, state: State, job_id: str, reject: str | None) -> tuple[int, str]:
    job = _job(state, job_id)
    _require(job, "APPROVAL_REQUIRED")
    if reject:
        job["approval"] = {"by": "human", "decision": "rejected", "reason": reject, "at": now()}
        _set_status(job, "RETRY_REQUIRED")
        _start_clock(job, now())
        # 이어서 고치는 세션이 끝난 세션의 heartbeat에 막히지 않게 한다 (충돌 이어받기와 같음)
        _clear_heartbeat(root)
        return 0, f"{job_id}: 반려 → RETRY_REQUIRED ({reject})"
    job["approval"] = {"by": "human", "decision": "approved", "reason": "", "at": now()}
    _set_status(job, "APPROVED")
    return cmd_merge(root, state, job_id, approved_now=True)


def cmd_recover(root: Path, state: State, job_id: str, force: bool) -> str:
    """끊긴 세션의 작업을 이어받는다 (기획안 6.3). 진행 중이던 단계는 다시 실행한다."""
    job = _job(state, job_id)
    _require(job, *sorted(RECOVERABLE_STATUSES))
    at = now()
    heartbeat = read_heartbeat(root)
    if heartbeat and at - float(heartbeat.get("at", 0)) < LOCK_TTL_S and not force:
        idle = at - float(heartbeat.get("at", 0))
        raise FactoryError(
            f"이전 세션이 {idle:.0f}초 전까지 활동했습니다. {LOCK_TTL_S}초가 지난 뒤 다시 recover "
            "하세요 (그 세션이 끝난 것이 확실하면 사람이 recover --force)"
        )
    seen = [float(job["history"][-1]["at"])]
    if heartbeat:
        seen.append(float(heartbeat.get("at", 0)))
    last_seen = min(max(seen), at)
    if _clock_running(job):
        segment = job["time"]["segments"][-1]
        segment[1] = min(max(segment[0], last_seen), at)
    aborted = _abort_git_operations(root)
    released = _release_merge_lock(root, job_id)
    _clear_heartbeat(root)
    job.setdefault("recoveries", []).append(
        {
            "at": at,
            "from": job["status"],
            "gap_s": round(at - last_seen, 1),
            "aborted": aborted,
            "released_merge_lock": released,
            "forced": force,
        }
    )
    if job["status"] == "APPROVED" and _is_merged(root, job):
        _return_to_base(root, job)
        _mark_merged(root, state, job)
        return f"{job_id}: 이전 세션이 머지까지 끝냈습니다 → MERGED ({job['merge_commit'][:7]})"
    if _git(root, "branch", "--show-current").strip() != job["branch"]:
        _git(root, "switch", "-q", job["branch"])
    if job["status"] in ACTIVE_STATUSES:
        _start_clock(job, at)
    next_step = {
        "QUEUED": f"spec·수락 테스트 작성 후 spec {job_id}",
        "SPEC_READY": f"spec·수락 테스트 확인 후 red {job_id}",
        "BUILDING": f"Builder 호출 후 verify {job_id}",
        "RETRY_REQUIRED": f"Builder 호출 후 verify {job_id}",
        "VERIFYING": f"validate {job_id}",
        "APPROVED": f"merge {job_id}",
    }[job["status"]]
    done = f", 정리: {', '.join(aborted)} 중단" if aborted else ""
    gap = _fmt_minutes(at - last_seen)
    return f"{job_id}: 이어받음 ({job['status']}, 끊긴 시간 {gap}{done}). 다음: {next_step}"


def cmd_resume(root: Path, state: State, job_id: str, extend: bool) -> str:
    job = _job(state, job_id)
    _require(job, "BLOCKED")
    if extend:
        if job["time"]["extended"]:
            raise FactoryError(f"{job_id}는 이미 한 번 연장했습니다 (split 또는 drop)")
        base_cap = SIZE_CAPS.get(job.get("size") or "small", SIZE_CAPS["small"])
        job["time"]["cap_s"] = job["time"]["cap_s"] + int(base_cap * EXTEND_RATIO)
        job["time"]["extended"] = True
    target = job["checkpoint"]
    job["blocked"] = None
    job["last_failure"] = None
    _set_status(job, target)
    _clear_heartbeat(root)
    if target in ACTIVE_STATUSES:
        _start_clock(job, now())
    return f"{job_id}: 재개 → {target} (상한 {_fmt_minutes(job['time']['cap_s'])})"


def cmd_drop(root: Path, state: State, job_id: str) -> str:
    job = _job(state, job_id)
    if job["status"] in CLOSED_STATUSES:
        raise FactoryError(f"{job_id}는 이미 {job['status']}입니다")
    _abort_git_operations(root)
    if _git(root, "branch", "--show-current").strip() != job["branch"]:
        _git(root, "switch", "-q", job["branch"])
    job["commit"] = _commit_and_return(
        root, job, f"factory {job_id} (중단 보관): {job['requirement']}"
    )
    _release_merge_lock(root, job_id)
    _close_job(root, state, job, "DROPPED")
    return f"{job_id}: DROPPED (브랜치 {job['branch']}는 보관)"


def cmd_status(state: State, as_json: bool) -> str:
    if as_json:
        return json.dumps(state, ensure_ascii=False, indent=2)
    return render_summary(state)


def cmd_status_all(root: Path) -> str:
    """모든 worktree의 작업을 한 표로 보여준다 (작업별 상태는 각 worktree에 있다)."""
    lines = [
        "# 팩토리 상태 (모든 worktree)",
        "",
        "| worktree | ID | 상태 | 위험 | 활성 시간 / 상한 | 요구사항 | 비고 |",
        "|---|---|---|---|---|---|---|",
    ]
    for path, _ in _worktrees(root):
        for job in load_state(path)["jobs"].values():
            cells = [
                path.as_posix(),
                job["id"],
                job["status"],
                job.get("risk") or job.get("risk_declared") or "-",
                f"{_fmt_minutes(job['time']['active_s'])} / {_fmt_minutes(job['time']['cap_s'])}",
                job["requirement"][:50],
                _job_note(job),
            ]
            lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    # FACTORY_ROOT: 다른 checkout(작업 worktree)을 대상으로 실행할 때 (dispatch.py가 사용)
    root = root or Path(os.environ.get("FACTORY_ROOT") or TRUSTED_ROOT)
    parser = argparse.ArgumentParser(prog="factory", description="팩토리 상태 관리")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new")
    new.add_argument("requirement")
    new.add_argument("--base", help="머지 대상 브랜치 (detached worktree에서 필요)")
    new.add_argument("--plan", help="계획 작업 P001:A (dispatch.py --run-plan이 넘김)")
    new.add_argument("--expected-risk", help="계획 단계의 예상 등급")
    for name in ("spec", "red", "verify", "validate", "merge", "drop"):
        sub.add_parser(name).add_argument("job_id")
    block = sub.add_parser("block")
    block.add_argument("job_id")
    block.add_argument(
        "--reason",
        required=True,
        choices=[
            "TEST_INVALID",
            "SPEC_INVALID",
            "NEEDS_DECISION",
            "TIMEOUT",
            "BUDGET",
            "SESSION_ENDED",
            "MANUAL",
        ],
    )
    block.add_argument("--detail", required=True)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--all", action="store_true", help="모든 worktree의 작업")
    approve = sub.add_parser("approve")
    approve.add_argument("job_id")
    approve.add_argument("--reject")
    resume = sub.add_parser("resume")
    resume.add_argument("job_id")
    resume.add_argument("--extend", action="store_true")
    recover = sub.add_parser("recover")
    recover.add_argument("job_id")
    recover.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    human_only = args.command in HUMAN_ONLY or (args.command == "recover" and args.force)
    if human_only and os.environ.get("FACTORY_AGENT_SESSION") == "1":
        print(
            "오류: approve·resume·drop·recover --force는 사람 전용입니다. "
            "에이전트 세션에서는 실행할 수 없습니다 (사람에게 안내만 하세요)."
        )
        return 2

    state = load_state(root)
    code = 0
    try:
        if args.command == "new":
            message = cmd_new(root, state, args.requirement, args.base, args.plan,
                              args.expected_risk)  # fmt: skip
        elif args.command == "spec":
            message = cmd_spec(root, state, args.job_id)
        elif args.command == "red":
            code, message = cmd_red(root, state, args.job_id)
        elif args.command == "verify":
            code, message = cmd_verify(root, state, args.job_id)
        elif args.command == "validate":
            code, message = cmd_validate(root, state, args.job_id)
        elif args.command == "merge":
            code, message = cmd_merge(root, state, args.job_id)
        elif args.command == "recover":
            message = cmd_recover(root, state, args.job_id, args.force)
        elif args.command == "block":
            message = cmd_block(state, args.job_id, args.reason, args.detail)
        elif args.command == "approve":
            code, message = cmd_approve(root, state, args.job_id, args.reject)
        elif args.command == "resume":
            message = cmd_resume(root, state, args.job_id, args.extend)
        elif args.command == "drop":
            message = cmd_drop(root, state, args.job_id)
        elif args.all:
            print(cmd_status_all(root))
            return 0
        else:
            print(cmd_status(state, args.json))
            return 0
    except FactoryError as error:
        print(f"오류: {error}")
        # 외부 프로세스·Git 작업 중 남긴 기록(리뷰 원문, 승인 기록)은 보존한다
        if args.command in {"validate", "approve", "merge"}:
            save_state(root, state)
        return 2
    save_state(root, state)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
