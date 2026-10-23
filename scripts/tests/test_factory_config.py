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


# ---------- 단계별 모델·인증 (stages) ----------


def test_claude_models_are_the_latest_three() -> None:
    assert factory_config.CLAUDE_MODELS == {
        "haiku": "claude-haiku-5-5",
        "sonnet": "claude-sonnet-5-5",
        "opus": "claude-opus-5-5",
    }


def test_stage_settings_default_to_auto(tmp_path: Path) -> None:
    cfg = factory_config.load_config(tmp_path)
    assert set(cfg.stages) == set(factory_config.STAGES)
    for stage in factory_config.STAGES:
        assert cfg.stages[stage] == {"model": "auto", "auth": "auto"}


def test_stage_settings_from_config(tmp_path: Path) -> None:
    write(tmp_path, {"stages": {"reviewer_test": {"model": "haiku", "auth": "subscription"}}})
    cfg = factory_config.load_config(tmp_path)
    assert cfg.stages["reviewer_test"] == {"model": "haiku", "auth": "subscription"}
    assert cfg.stages["orchestrator"] == {"model": "auto", "auth": "auto"}


@pytest.mark.parametrize(
    "stages",
    [
        {"reviewer_code": {"model": "claude-3-opus"}},  # 최신 세 모델 별칭만
        {"reviewer_code": {"auth": "free"}},
        {"planner": {"model": "opus"}},  # 모르는 단계
        {"builder": {"auth": "api"}},  # Builder는 오케스트레이터 세션의 인증을 따른다
    ],
)
def test_invalid_stage_settings_are_rejected(tmp_path: Path, stages: dict[str, object]) -> None:
    write(tmp_path, {"stages": stages})
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_config(tmp_path)


# ---------- 비용 상한 (budgets, 대시보드 선행 P2) ----------


def test_budgets_default(tmp_path: Path) -> None:
    cfg = factory_config.load_config(tmp_path)
    assert cfg.budgets == {"session": 5.0, "reviewer_call": 0.5, "reviewer_total": 1.5,
                           "job_total": 6.5}  # fmt: skip


def test_budgets_from_config(tmp_path: Path) -> None:
    write(tmp_path, {"budgets": {"reviewer_total": 2.0, "job_total": 7.0}})
    cfg = factory_config.load_config(tmp_path)
    assert cfg.budgets["reviewer_total"] == 2.0 and cfg.budgets["session"] == 5.0


@pytest.mark.parametrize(
    "budgets",
    [
        {"session": -1},
        {"session": "많이"},
        {"tokens": 3},
        {"session": 6.0},  # 작업 총액(6.5) < 세션 6 + 리뷰어 합계 1.5
    ],
)
def test_invalid_budgets(tmp_path: Path, budgets: dict[str, object]) -> None:
    write(tmp_path, {"budgets": budgets})
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_config(tmp_path)
