import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import factory_settings  # noqa: E402

REPO_CONFIG = SCRIPTS.parent / "factory" / "config.json"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "config.json").write_text(
        REPO_CONFIG.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def stages(repo: Path) -> dict[str, dict[str, str]]:
    data = json.loads((repo / "factory" / "config.json").read_text(encoding="utf-8"))
    result: dict[str, dict[str, str]] = data["stages"]
    return result


def test_show_lists_every_stage_with_resolved_auto(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert factory_settings.main(["show"], root=repo) == 0
    out = capsys.readouterr().out
    for stage in ("orchestrator", "builder", "reviewer_code", "reviewer_security", "reviewer_test"):
        assert stage in out
    assert "claude-opus-5-5" in out  # 오케스트레이터 자동 = Opus
    assert "auto" in out


def test_set_model_and_auth(repo: Path) -> None:
    assert factory_settings.main(["set", "reviewer_test", "model", "haiku"], root=repo) == 0
    assert factory_settings.main(["set", "orchestrator", "auth", "subscription"], root=repo) == 0
    assert stages(repo)["reviewer_test"]["model"] == "haiku"
    assert stages(repo)["orchestrator"]["auth"] == "subscription"
    # 다른 설정 값은 그대로
    data = json.loads((repo / "factory" / "config.json").read_text(encoding="utf-8"))
    original = json.loads(REPO_CONFIG.read_text(encoding="utf-8"))
    assert data["lock_patterns"] == original["lock_patterns"]


@pytest.mark.parametrize(
    "argv",
    [
        ["set", "reviewer_test", "model", "gpt-4"],
        ["set", "planner", "model", "opus"],
        ["set", "builder", "auth", "api"],
        ["set", "reviewer_code", "color", "red"],
    ],
)
def test_invalid_set_is_refused_and_file_unchanged(repo: Path, argv: list[str]) -> None:
    before = (repo / "factory" / "config.json").read_text(encoding="utf-8")
    assert factory_settings.main(argv, root=repo) == 2
    assert (repo / "factory" / "config.json").read_text(encoding="utf-8") == before


def test_refused_inside_agent_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT_SESSION", "1")
    assert factory_settings.main(["set", "builder", "model", "opus"], root=repo) == 2
    assert stages(repo)["builder"]["model"] == "auto"


def test_written_with_lf_and_utf8(repo: Path) -> None:
    factory_settings.main(["set", "builder", "model", "opus"], root=repo)
    raw = (repo / "factory" / "config.json").read_bytes()
    assert b"\r\n" not in raw
    assert "레포별 설정".encode() in raw
