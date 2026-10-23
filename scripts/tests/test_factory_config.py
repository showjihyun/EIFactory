import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import factory_config  # noqa: E402

REPO = SCRIPTS.parent


def write(root: Path, data: dict[str, object]) -> None:
    (root / "factory").mkdir(parents=True, exist_ok=True)
    (root / "factory" / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_project_values_are_merged_with_kit_protection(tmp_path: Path) -> None:
    write(tmp_path, {"lock_patterns": ["pyproject.toml"], "high_paths": ["src/app/auth.py"]})
    cfg = factory_config.load_config(tmp_path)
    assert "pyproject.toml" in cfg.lock_patterns
    assert "src/app/auth.py" in cfg.high_paths
    # 키트 공통 보호는 레포 설정이 없어도 항상 들어간다
    for path in ("scripts/*", "AGENTS.md", ".claude/*", ".codex/*", "factory/config.json"):
        assert path in cfg.lock_patterns
    assert "factory/config.json" in cfg.high_paths


def test_missing_config_keeps_kit_protection(tmp_path: Path) -> None:
    cfg = factory_config.load_config(tmp_path)
    assert "factory/PROCESS.md" in cfg.lock_patterns
    assert cfg.acceptance_tests == []


def test_acceptance_patterns_take_the_job_id(tmp_path: Path) -> None:
    write(
        tmp_path,
        {"acceptance_tests": [{"pattern": "^tests/acceptance/test_{id}\\.py$", "example": "x"}]},
    )
    cfg = factory_config.load_config(tmp_path)
    assert cfg.is_acceptance_test("tests/acceptance/test_J001.py", "J001")
    assert not cfg.is_acceptance_test("tests/acceptance/test_J002.py", "J001")
    assert not cfg.is_acceptance_test("tests/acceptance/test_J001.py.bak", "J001")


def test_invalid_config_is_reported(tmp_path: Path) -> None:
    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_config(tmp_path)


def test_sample_repo_config_is_valid() -> None:
    cfg = factory_config.load_config(REPO)
    assert cfg.is_acceptance_test("backend/tests/acceptance/test_E03.py", "E03")
    assert cfg.is_acceptance_test("frontend/src/components/List.E01.accept.test.tsx", "E01")
    assert cfg.is_acceptance_test("frontend/e2e/login.J001.accept.e2e.ts", "J001")
    assert cfg.changed_test.match("frontend/e2e/bookmarks.e2e.ts")
    assert cfg.docker_workdir == "/workspaces/bookmark-factory"


def test_job_verify_defaults_to_full_verify(tmp_path: Path) -> None:
    cfg = factory_config.load_config(tmp_path)
    assert cfg.job_verify_args == []
    # 키트 공통 보호 경로는 언제나 전체 검증 조건이다
    assert "scripts/*" in cfg.job_verify_full_if_changed


def test_sample_repo_skips_scripts_for_jobs() -> None:
    cfg = factory_config.load_config(REPO)
    assert cfg.job_verify_args == ["--skip", "scripts"]
    for path in ("scripts/*", "frontend/package.json", ".devcontainer/*", "backend/uv.lock"):
        assert path in cfg.job_verify_full_if_changed
