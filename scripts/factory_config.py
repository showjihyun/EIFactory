"""팩토리 키트의 레포별 설정 (`factory/config.json`, 기획안 Phase 4 "레포에 종속되지 않는 키트").

키트 공통 보호 경로(정책·절차 파일, 도구 래퍼, 격리 환경, 이 설정 파일)는 코드에 둔다.
레포마다 다른 값(검증 설정 파일, 기존 테스트 위치, 수락 테스트 경로 규칙, 위험 경로, 의존성 설치,
컨테이너 경로)만 설정 파일에 둔다. 설정 파일이 없어도 공통 보호는 항상 적용된다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = "factory/config.json"
DEFAULT_WORKDIR = "/workspaces/bookmark-factory"

# Claude 모델은 최신 세 가지만 쓴다. 바꿀 때는 기획안 11장 모델 교체 절차를 거친다 (기획안 1.2)
CLAUDE_MODELS = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
# 모델·인증을 따로 정할 수 있는 단위. 오케스트레이터 세션 하나가 spec~validate를 진행하므로
# 그 안의 단계마다 모델을 바꿀 수는 없다. Builder는 같은 세션의 서브에이전트라 인증을 따른다
STAGES = ("orchestrator", "builder", "reviewer_code", "reviewer_security", "reviewer_test")
MODEL_CHOICES = ("auto", *CLAUDE_MODELS)
AUTH_CHOICES = ("auto", "subscription", "api")
# 비용 상한 (API 환산 USD, 임시값. 기획안 7.4, 대시보드 설계안 4장 P2). `new` 때 작업에 고정된다
BUDGET_DEFAULTS = {"session": 5.0, "reviewer_call": 0.5, "reviewer_total": 1.5, "job_total": 6.5}

# 정책·절차 파일, 도구 래퍼, 격리 환경, 이 설정. 어느 레포에서나 잠그고 HIGH로 본다
KIT_POLICY = [
    "AGENTS.md",
    "CLAUDE.md",
    "factory/PROCESS.md",
    "factory/prompts/*",
    "factory/templates/*",
    CONFIG_PATH,
    "factory/kit.lock",
    ".claude/*",
    ".codex/*",
    ".agents/*",
    ".devcontainer/*",
    "docker/*",
]
KIT_LOCK = ["scripts/*", *KIT_POLICY]
KIT_NO_NEW = [
    "scripts/*",
    "factory/prompts/*",
    ".claude/*",
    ".codex/*",
    ".agents/*",
    ".devcontainer/*",
    "docker/*",
]
KIT_HIGH = [*KIT_POLICY, ".github/*"]
KIT_MEDIUM = ["scripts/*"]


class ConfigError(Exception):
    pass


def _merge(kit: list[str], extra: list[str]) -> list[str]:
    return list(dict.fromkeys([*extra, *kit]))


@dataclass(frozen=True)
class Config:
    lock_patterns: list[str]
    no_new_file_patterns: list[str]
    high_paths: list[str]
    medium_paths: list[str]
    test_patterns: list[str]
    changed_test: re.Pattern[str]
    acceptance_tests: list[dict[str, str]]
    setup: list[tuple[str, list[str]]]
    docker_workdir: str = DEFAULT_WORKDIR
    docker_setup_script: str = ""
    docker_cache_volumes: dict[str, str] = field(default_factory=dict)
    # 작업 검증에 덧붙이는 verify 인자 (예: 작업이 바꿀 수 없는 영역 건너뛰기). diff에
    # job_verify_full_if_changed 경로가 하나라도 있으면 붙이지 않고 전체를 검증한다
    job_verify_args: list[str] = field(default_factory=list)
    job_verify_full_if_changed: list[str] = field(default_factory=list)
    # 단계별 모델·인증 {단계: {"model": auto|haiku|sonnet|opus, "auth": auto|subscription|api}}
    stages: dict[str, dict[str, str]] = field(default_factory=dict)
    budgets: dict[str, float] = field(default_factory=lambda: dict(BUDGET_DEFAULTS))
    # dispatch 종료 Windows 알림 (기획안 6.9 P2, 기본 켬)
    notify_toast: bool = True
    # 사용자 흐름 테스트가 남긴 화면 스크린샷 (레포 기준 glob). 승인 요약·브리핑에 붙인다
    screenshots: list[str] = field(default_factory=list)

    def acceptance_patterns(self, job_id: str) -> list[re.Pattern[str]]:
        return [
            re.compile(item["pattern"].replace("{id}", re.escape(job_id)))
            for item in self.acceptance_tests
        ]

    def is_acceptance_test(self, path: str, job_id: str) -> bool:
        return any(p.match(path) for p in self.acceptance_patterns(job_id))

    def acceptance_examples(self, job_id: str) -> str:
        return " 또는 ".join(
            item["example"].replace("{id}", job_id) for item in self.acceptance_tests
        )


def _stages(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_PATH}: stages는 객체여야 합니다")
    unknown = sorted(k for k in set(raw) - set(STAGES) if not k.startswith("_"))  # _설명 허용
    if unknown:
        raise ConfigError(f"{CONFIG_PATH}: 모르는 단계 {unknown} (가능: {', '.join(STAGES)})")
    out: dict[str, dict[str, str]] = {}
    for stage in STAGES:
        item = raw.get(stage) or {}
        if not isinstance(item, dict) or set(item) - {"model", "auth", "_설명"}:
            raise ConfigError(f"{CONFIG_PATH}: stages.{stage}에는 model, auth만 씁니다")
        model, auth = str(item.get("model", "auto")), str(item.get("auth", "auto"))
        if model not in MODEL_CHOICES:
            raise ConfigError(
                f"{CONFIG_PATH}: stages.{stage}.model={model} (가능: {', '.join(MODEL_CHOICES)})"
            )
        if auth not in AUTH_CHOICES:
            raise ConfigError(
                f"{CONFIG_PATH}: stages.{stage}.auth={auth} (가능: {', '.join(AUTH_CHOICES)})"
            )
        if stage == "builder" and auth != "auto":
            raise ConfigError(
                f"{CONFIG_PATH}: stages.builder.auth는 auto만 됩니다 "
                "(Builder는 오케스트레이터 세션의 인증을 따른다)"
            )
        out[stage] = {"model": model, "auth": auth}
    return out


def _budgets(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_PATH}: budgets는 객체여야 합니다")
    unknown = sorted(k for k in raw if k not in BUDGET_DEFAULTS and not str(k).startswith("_"))
    if unknown:
        raise ConfigError(
            f"{CONFIG_PATH}: 모르는 상한 {unknown} (가능: {', '.join(BUDGET_DEFAULTS)})"
        )
    out = dict(BUDGET_DEFAULTS)
    for key in BUDGET_DEFAULTS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise ConfigError(f"{CONFIG_PATH}: budgets.{key}={value!r}는 양수여야 합니다")
        out[key] = float(value)
    if out["job_total"] < out["session"] + out["reviewer_total"]:
        raise ConfigError(
            f"{CONFIG_PATH}: budgets.job_total({out['job_total']})은 "
            f"session + reviewer_total({out['session'] + out['reviewer_total']}) 이상이어야 합니다"
        )
    return out


def _notify_toast(raw: Any) -> bool:
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_PATH}: notify는 객체여야 합니다")
    unknown = sorted(k for k in raw if k != "toast" and not str(k).startswith("_"))
    if unknown:
        raise ConfigError(f"{CONFIG_PATH}: 모르는 알림 설정 {unknown} (가능: toast)")
    toast = raw.get("toast", True)
    if not isinstance(toast, bool):
        raise ConfigError(f"{CONFIG_PATH}: notify.toast={toast!r}는 true 또는 false")
    return toast


def _screenshots(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise ConfigError(f"{CONFIG_PATH}: screenshots는 경로 규칙(glob) 문자열의 목록")
    for pattern in raw:
        if pattern.startswith(("/", "\\")) or ".." in pattern.replace("\\", "/").split("/"):
            raise ConfigError(f"{CONFIG_PATH}: screenshots는 레포 안 경로만 ({pattern})")
    return list(raw)


def load_config(root: Path) -> Config:
    path = root / CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConfigError(f"{CONFIG_PATH}를 읽을 수 없습니다: {error}") from error
    docker = data.get("docker") or {}
    job_verify = data.get("job_verify") or {}
    try:
        return Config(
            lock_patterns=_merge(KIT_LOCK, list(data.get("lock_patterns", []))),
            no_new_file_patterns=_merge(KIT_NO_NEW, list(data.get("no_new_file_patterns", []))),
            high_paths=_merge(KIT_HIGH, list(data.get("high_paths", []))),
            medium_paths=_merge(KIT_MEDIUM, list(data.get("medium_paths", []))),
            test_patterns=list(data.get("test_patterns", [])),
            # 기본값은 아무것도 맞지 않는 규칙 (바꾼 기존 테스트를 받지 않음)
            changed_test=re.compile(data.get("changed_test", r"(?!)")),
            acceptance_tests=[dict(item) for item in data.get("acceptance_tests", [])],
            setup=[(str(area), [str(c) for c in cmd]) for area, cmd in data.get("setup", [])],
            docker_workdir=str(docker.get("workdir", DEFAULT_WORKDIR)),
            docker_setup_script=str(docker.get("setup_script", "")),
            docker_cache_volumes={
                str(k): str(v) for k, v in docker.get("cache_volumes", {}).items()
            },
            stages=_stages(data.get("stages") or {}),
            budgets=_budgets(data.get("budgets") or {}),
            notify_toast=_notify_toast(data.get("notify", {})),
            screenshots=_screenshots(data.get("screenshots", [])),
            job_verify_args=[str(a) for a in job_verify.get("args", [])],
            # 키트 파일(scripts/, 정책·도구 래퍼·격리 환경)이 바뀌면 언제나 전체 검증
            job_verify_full_if_changed=_merge(
                KIT_LOCK, list(job_verify.get("full_if_changed", []))
            ),
        )
    except (re.error, TypeError, ValueError, KeyError) as error:
        raise ConfigError(f"{CONFIG_PATH} 형식 오류: {error}") from error
