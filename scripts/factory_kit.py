"""팩토리 키트 설치·점검 (기획안 Phase 4 "레포에 종속되지 않는 키트").

사용법:
    python scripts/factory_kit.py install <대상 레포> [--force]
    python scripts/factory_kit.py check <대상 레포>

install: 이 레포의 팩토리 파일(상태 CLI, hook, dispatcher, 절차, 지시문, 도구 래퍼, 프록시)을 대상
레포에 복사한다. 이미 있는 파일은 `--force` 없이는 덮어쓰지 않는다. `factory/config.json`,
`AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`은 대상에 없을 때만 템플릿으로 만든다.
설치 기록(`factory/kit.lock`)에 원본 커밋과 파일별 해시를 남긴다. 이 스크립트 자신은 복사하지 않는다
(설치는 원본 레포에서만, 복사본의 복사본이 생기지 않게. 기획안 12.1).

check: 키트 파일, 설치 기록과 다른 키트 파일, 검증 계약 스크립트(verify, check_red, check_style),
레포별 설정을 점검한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_config import Config, ConfigError, load_config  # noqa: E402
from proc import use_utf8_output  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1]

# 키트 파일 (대상 경로 = 원본 경로)
KIT_FILES = [
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
    "scripts/proc.py",
    "scripts/hooks/guard.py",
    "factory/PROCESS.md",
    "factory/prompts/validator.md",
    "factory/prompts/review-code.md",
    "factory/prompts/review-security.md",
    "factory/prompts/review-test.md",
    "factory/prompts/builder.md",
    ".claude/skills/factory/SKILL.md",
    ".claude/agents/builder.md",
    ".codex/hooks.json",
    ".agents/skills/factory/SKILL.md",
    "docker/proxy/Dockerfile",
    "docker/proxy/tinyproxy.conf",
    "docker/proxy/filter",
]
# 대상에 없을 때만 만드는 파일 (대상 경로 → 템플릿)
TEMPLATES = {
    "factory/config.json": "factory/templates/config.json",
    "AGENTS.md": "factory/templates/AGENTS.md",
    ".claude/settings.json": "factory/templates/claude-settings.json",
}
CONTRACT = ["scripts/verify.py", "scripts/check_red.py", "scripts/check_style.py"]
KIT_LOCK_PATH = "factory/kit.lock"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_commit() -> str:
    """원본 레포의 커밋. 키트 파일에 커밋되지 않은 변경이 있으면 표시한다."""

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=SOURCE, capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else ""

    head = git("rev-parse", "HEAD") or "unknown"
    dirty = git("status", "--porcelain", "--", *KIT_FILES)
    return f"{head}+dirty" if dirty else head


def _copy(source: Path, target: Path, force: bool) -> str:
    if target.exists() and not force:
        return "유지"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return "복사"


def install(target: Path, force: bool) -> list[str]:
    lines = []
    for rel in KIT_FILES:
        lines.append(f"{_copy(SOURCE / rel, target / rel, force)}  {rel}")
    for rel, template in TEMPLATES.items():
        lines.append(f"{_copy(SOURCE / template, target / rel, force=False)}  {rel} (템플릿)")
    claude = target / "CLAUDE.md"
    if not claude.exists():
        claude.write_text("@AGENTS.md\n\n## Claude Code 전용\n", encoding="utf-8")
        lines.append("생성  CLAUDE.md")
    lock = {
        "source": "showjihyun/EIFactory",
        "source_commit": _source_commit(),
        "files": {rel: _sha(target / rel) for rel in KIT_FILES},
    }
    path = target / KIT_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines.append(f"기록  {KIT_LOCK_PATH} (원본 {lock['source_commit'][:12]})")
    return lines


def load_target_config(target: Path) -> Config:
    return load_config(target)


def check(target: Path) -> list[str]:
    problems = [f"키트 파일 없음: {rel}" for rel in KIT_FILES if not (target / rel).exists()]
    try:
        recorded = json.loads((target / KIT_LOCK_PATH).read_text(encoding="utf-8"))["files"]
    except (OSError, json.JSONDecodeError, KeyError):
        recorded = None
        problems.append(f"설치 기록 없음: {KIT_LOCK_PATH} (원본 레포에서 install로 다시 설치)")
    if recorded is not None:
        problems += [
            f"키트 파일이 설치 기록과 다름: {rel} (손으로 고쳤거나 다른 버전)"
            for rel in KIT_FILES
            if (target / rel).exists() and recorded.get(rel) != _sha(target / rel)
        ]
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


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(prog="factory_kit", description="팩토리 키트")
    sub = parser.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install")
    inst.add_argument("target", type=Path)
    inst.add_argument("--force", action="store_true", help="이미 있는 키트 파일도 덮어씀")
    chk = sub.add_parser("check")
    chk.add_argument("target", type=Path)
    args = parser.parse_args(argv)

    target = args.target.resolve()
    if args.command == "install":
        if target == SOURCE:
            print("오류: 원본 레포에 설치할 수 없습니다")
            return 2
        print("\n".join(install(target, args.force)))
        print("\n다음: factory/config.json과 검증 계약 스크립트를 채운 뒤 check")
        return 0
    problems = check(target)
    if problems:
        print("점검 실패:\n- " + "\n- ".join(problems))
        return 1
    print("점검 통과: 키트 파일, 검증 계약, 레포별 설정")
    return 0


if __name__ == "__main__":
    sys.exit(main())
