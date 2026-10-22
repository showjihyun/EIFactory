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

# 정책·절차 파일, 도구 래퍼, 격리 환경, 이 설정. 어느 레포에서나 잠그고 HIGH로 본다
KIT_POLICY = [
    "AGENTS.md",
    "CLAUDE.md",
    "factory/PROCESS.md",
    "factory/prompts/*",
    "factory/templates/*",
    CONFIG_PATH,
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


def load_config(root: Path) -> Config:
    path = root / CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConfigError(f"{CONFIG_PATH}를 읽을 수 없습니다: {error}") from error
    docker = data.get("docker") or {}
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
        )
    except (re.error, TypeError, ValueError, KeyError) as error:
        raise ConfigError(f"{CONFIG_PATH} 형식 오류: {error}") from error
