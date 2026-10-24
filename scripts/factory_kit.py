"""팩토리 키트 설치·점검·업그레이드 (얇은 설치, 기획안 12.1).

사용법:
    python scripts/factory_kit.py install <대상 레포> --engine factory-vN [--force]
    python scripts/factory_kit.py check <대상 레포>
    python scripts/factory_kit.py upgrade <대상 레포> --engine factory-vN [--yes]

엔진(상태 CLI, hook, dispatcher, 대시보드, 리뷰어 지시문, 프록시)은 이 레포의 버전 태그
(`factory-vN`)에서 꺼내 레포 밖 `FACTORY_ENGINE_HOME`(기본 `~/.factory/engines`)/<태그>에
읽기 전용으로 둔다. 작업 트리가 아니라 태그에서 꺼내므로 커밋 안 한 변경은 들어가지 않는다.

install: 대상 레포에는 그 레포에서 필요한 것만 둔다.
- shim: `scripts/factory.py` 등 같은 이름의 짧은 파일. 엔진을 찾아 실행한다 (`factory_engine.py`).
  명령·허용 목록·hook 설정이 지금과 같은 경로를 쓴다.
- 복사: 에이전트가 읽는 절차·지시문(`factory/PROCESS.md`, `.claude/`, `.agents/`, `.codex/`)과
  검증 계약 스크립트가 쓰는 `proc.py`. Claude Code 플러그인(12.1 ③) 전까지는 레포 안에 있어야 한다.
- 템플릿: `factory/config.json`, `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`은 없을 때만.
- 설치 기록 `factory/kit.lock`: 엔진 태그·커밋·엔진 파일 해시, 레포에 둔 키트 파일 해시.
이미 있는 키트 파일은 `--force` 없이는 덮어쓰지 않는다. 이 스크립트 자신은 엔진에도 대상에도
넣지 않는다 (설치는 원본 레포에서만, 복사본의 복사본이 생기지 않게).

check: 키트 파일과 엔진을 설치 기록과 대조하고, 검증 계약 스크립트와 레포별 설정을 점검한다.

upgrade: 새 태그와의 차이를 보여 준다. `--yes`를 붙이면 바꾼다. 레포의 키트 파일을 손으로
고쳤으면 바꾸지 않는다. 설치·업그레이드는 사람 전용이다 (팩토리 작업이 하지 않는다).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_config import Config, ConfigError, load_config  # noqa: E402
from factory_engine import (  # noqa: E402
    ENGINE_HOME_ENV,
    KIT_LOCK_PATH,
    engine_home,
    engine_problems,
    sha256,
)
from proc import use_utf8_output  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1]
TAG_PATTERN = re.compile(r"^factory-v\d+$")

# 엔진 파일 (레포 밖 엔진 폴더에만 둔다)
ENGINE_FILES = [
    "scripts/factory.py",
    "scripts/factory_config.py",
    "scripts/dispatch.py",
    "scripts/issues.py",
    "scripts/factory_settings.py",
    "scripts/monitor.py",
    "scripts/monitor_page.html",
    "scripts/monitor_assets/bolt_QUEUED.png",
    "scripts/monitor_assets/bolt_SPEC_READY.png",
    "scripts/monitor_assets/bolt_BUILDING.png",
    "scripts/monitor_assets/bolt_RETRY_REQUIRED.png",
    "scripts/monitor_assets/bolt_VERIFYING.png",
    "scripts/monitor_assets/bolt_APPROVAL_REQUIRED.png",
    "scripts/monitor_assets/bolt_APPROVED.png",
    "scripts/monitor_assets/bolt_MERGED.png",
    "scripts/monitor_assets/bolt_BLOCKED.png",
    "scripts/monitor_assets/bolt_ERROR.png",
    "scripts/monitor_assets/bolt_DROPPED.png",
    "scripts/monitor_assets/bolt_UNKNOWN.png",
    "scripts/report.py",
    "scripts/brief.py",
    "scripts/proc.py",
    "scripts/hooks/guard.py",
    "factory/prompts/validator.md",
    "factory/prompts/review-code.md",
    "factory/prompts/review-security.md",
    "factory/prompts/review-test.md",
    "factory/templates/upper-session.example.json",
    "docker/proxy/Dockerfile",
    "docker/proxy/tinyproxy.conf",
    "docker/proxy/filter",
]
# 대상 레포의 shim (엔진의 같은 경로 스크립트를 실행한다)
SHIMS = [
    "scripts/factory.py",
    "scripts/dispatch.py",
    "scripts/issues.py",
    "scripts/factory_settings.py",
    "scripts/monitor.py",
    "scripts/report.py",
    "scripts/brief.py",
    "scripts/hooks/guard.py",
]
# 대상 레포에 태그 그대로 복사하는 파일
COPIED = [
    "scripts/factory_engine.py",
    "scripts/proc.py",
    "factory/PROCESS.md",
    "factory/prompts/builder.md",
    ".claude/skills/factory/SKILL.md",
    ".claude/agents/builder.md",
    ".codex/hooks.json",
    ".agents/skills/factory/SKILL.md",
]
# 대상에 없을 때만 만드는 파일 (대상 경로 → 템플릿)
TEMPLATES = {
    "factory/config.json": "factory/templates/config.json",
    "AGENTS.md": "factory/templates/AGENTS.md",
    ".claude/settings.json": "factory/templates/claude-settings.json",
}
CONTRACT = ["scripts/verify.py", "scripts/check_red.py", "scripts/check_style.py"]
SHIM_TEMPLATE = '''"""팩토리 엔진 shim (factory_kit.py가 만듦). 엔진 버전: factory/kit.lock"""

import os
import sys
from pathlib import Path

# 심어 둔 바이트코드 캐시를 읽지도 쓰지도 않는다 (엔진 해시 대조를 우회하지 못하게)
sys.dont_write_bytecode = True
sys.pycache_prefix = str(Path.home() / ".factory" / "no-pycache" / os.urandom(8).hex())
sys.path.insert(0, str(Path(__file__).resolve().parents[{depth}]))
import factory_engine  # noqa: E402

factory_engine.run("{rel}")
'''


class KitError(Exception):
    pass


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=SOURCE, capture_output=True)


def _tag_commit(tag: str) -> str:
    if not TAG_PATTERN.match(tag):
        raise KitError(f"엔진 태그는 factory-vN 형식이어야 합니다: {tag}")
    done = _git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    if done.returncode != 0:
        raise KitError(f"원본 레포에 엔진 태그가 없습니다: {tag} (git tag {tag} 후 다시)")
    return done.stdout.decode().strip()


def _writable(path: Path) -> None:
    for item in [path, *path.rglob("*")]:
        os.chmod(item, stat.S_IREAD | stat.S_IWRITE | (stat.S_IEXEC if item.is_dir() else 0))


def extract_engine(tag: str) -> tuple[Path, str]:
    """태그의 키트 파일을 엔진 폴더에 읽기 전용으로 꺼낸다. 이미 같은 내용이면 그대로 쓴다."""
    commit = _tag_commit(tag)
    paths = sorted({*ENGINE_FILES, *COPIED, *TEMPLATES.values()})
    archive = _git("archive", "--format=tar", tag, "--", *paths)
    if archive.returncode != 0:
        raise KitError(f"엔진을 꺼낼 수 없습니다: {archive.stderr.decode(errors='replace')[-300:]}")
    home = engine_home()
    home.mkdir(parents=True, exist_ok=True)
    fresh = home / f".{tag}.tmp"
    if fresh.exists():
        _writable(fresh)
        shutil.rmtree(fresh)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(fresh, filter="data")
    final = home / tag
    if final.exists():
        same = all(
            (final / rel).is_file() and sha256(final / rel) == sha256(fresh / rel) for rel in paths
        )
        if same:
            _writable(fresh)
            shutil.rmtree(fresh)
            return final, commit
        _writable(final)
        shutil.rmtree(final)
    fresh.rename(final)
    for item in final.rglob("*"):
        if item.is_file():
            os.chmod(item, stat.S_IREAD)
    return final, commit


def _put(target: Path, data: bytes, force: bool) -> str:
    if target.exists() and not force:
        return "유지"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    target.write_bytes(data)
    return "복사"


def shim_text(rel: str) -> str:
    return SHIM_TEMPLATE.format(depth=rel.count("/") - 1, rel=rel)


def install(target: Path, tag: str, force: bool) -> list[str]:
    engine, commit = extract_engine(tag)
    lines = [f"엔진  {engine} ({tag}, {commit[:12]}. 위치는 {ENGINE_HOME_ENV})"]
    for rel in SHIMS:
        data = shim_text(rel).encode("utf-8")
        lines.append(f"{_put(target / rel, data, force)}  {rel} (shim)")
    for rel in COPIED:
        lines.append(f"{_put(target / rel, (engine / rel).read_bytes(), force)}  {rel}")
    for rel, template in TEMPLATES.items():
        state = _put(target / rel, (engine / template).read_bytes(), force=False)
        lines.append(f"{state}  {rel} (템플릿)")
    claude = target / "CLAUDE.md"
    if not claude.exists():
        claude.write_text("@AGENTS.md\n\n## Claude Code 전용\n", encoding="utf-8")
        lines.append("생성  CLAUDE.md")
    lock = {
        "source": "showjihyun/EIFactory",
        "engine": tag,
        "engine_commit": commit,
        "engine_files": {rel: sha256(engine / rel) for rel in ENGINE_FILES},
        "files": {rel: sha256(target / rel) for rel in [*SHIMS, *COPIED]},
    }
    path = target / KIT_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines.append(f"기록  {KIT_LOCK_PATH} ({tag})")
    return lines


def load_target_config(target: Path) -> Config:
    return load_config(target)


def _read_lock(target: Path) -> dict[str, Any] | None:
    try:
        lock = json.loads((target / KIT_LOCK_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return lock if isinstance(lock, dict) and "engine" in lock else None


def _edited(target: Path, lock: dict[str, Any]) -> list[str]:
    return [
        rel
        for rel, digest in lock.get("files", {}).items()
        if (target / rel).exists() and sha256(target / rel) != digest
    ]


def check(target: Path) -> list[str]:
    lock = _read_lock(target)
    problems = []
    if lock is None:
        problems.append(
            f"설치 기록 없음: {KIT_LOCK_PATH} (원본 레포에서 install --engine factory-vN으로 설치)"
        )
    else:
        problems += [
            f"키트 파일 없음: {rel}" for rel in lock.get("files", {}) if not (target / rel).exists()
        ]
        problems += [
            f"키트 파일이 설치 기록과 다름: {rel} (손으로 고쳤거나 다른 버전)"
            for rel in _edited(target, lock)
        ]
        problems += engine_problems(lock)
    problems += [
        f"검증 계약 스크립트 없음: {rel} (AGENTS.md의 '검증 계약' 참고)"
        for rel in CONTRACT
        if not (target / rel).exists()
    ]
    if not (target / ".git").exists():
        problems.append("Git 레포가 아님")
    try:
        cfg = load_config(target)
    except ConfigError as error:
        return [*problems, str(error)]
    if not cfg.acceptance_tests:
        problems.append("factory/config.json: acceptance_tests(수락 테스트 경로 규칙)가 비어 있음")
    if not cfg.test_patterns:
        problems.append("factory/config.json: test_patterns(기존 테스트 위치)가 비어 있음")
    for item in cfg.acceptance_tests:
        if "{id}" not in item.get("pattern", ""):
            problems.append(f"acceptance_tests 규칙에 {{id}}가 없음: {item.get('pattern')}")
    return problems


def upgrade(target: Path, tag: str, yes: bool) -> tuple[int, list[str]]:
    lock = _read_lock(target)
    if lock is None:
        return 1, [f"얇은 설치 기록이 없습니다 ({KIT_LOCK_PATH}). install --engine {tag} --force로"]
    edited = _edited(target, lock)
    if edited:
        return 1, ["레포의 키트 파일을 손으로 고쳐서 바꾸지 않습니다:", *edited]
    old = str(lock["engine"])
    _tag_commit(tag)
    paths = sorted({*ENGINE_FILES, *COPIED})
    diff = _git("diff", "--stat", old, tag, "--", *paths).stdout.decode("utf-8", "replace")
    diff += _git("diff", old, tag, "--", *paths).stdout.decode("utf-8", "replace")
    lines = [f"엔진 {old} → {tag} 차이:", diff.rstrip() or "(차이 없음)"]
    if not yes:
        return 0, [*lines, "", "바꾸려면 --yes를 붙여 다시 실행하세요."]
    return 0, [*lines, "", *install(target, tag, force=True)]


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(prog="factory_kit", description="팩토리 키트")
    sub = parser.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install")
    inst.add_argument("target", type=Path)
    inst.add_argument("--engine", required=True, help="엔진 버전 태그 (factory-vN)")
    inst.add_argument("--force", action="store_true", help="이미 있는 키트 파일도 덮어씀")
    chk = sub.add_parser("check")
    chk.add_argument("target", type=Path)
    upg = sub.add_parser("upgrade")
    upg.add_argument("target", type=Path)
    upg.add_argument("--engine", required=True, help="새 엔진 버전 태그 (factory-vN)")
    upg.add_argument("--yes", action="store_true", help="차이를 확인했고 바꾼다")
    args = parser.parse_args(argv)

    target = args.target.resolve()
    if args.command in ("install", "upgrade") and target == SOURCE:
        print("오류: 원본 레포에 설치할 수 없습니다")
        return 2
    try:
        if args.command == "install":
            print("\n".join(install(target, args.engine, args.force)))
            print("\n다음: factory/config.json과 검증 계약 스크립트를 채운 뒤 check")
            return 0
        if args.command == "upgrade":
            code, lines = upgrade(target, args.engine, args.yes)
            print("\n".join(lines))
            return code
    except KitError as error:
        print(f"오류: {error}")
        return 2
    problems = check(target)
    if problems:
        print("점검 실패:\n- " + "\n- ".join(problems))
        return 1
    print("점검 통과: 키트 파일, 엔진, 검증 계약, 레포별 설정")
    return 0


if __name__ == "__main__":
    sys.exit(main())
