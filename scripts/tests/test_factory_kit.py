import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import factory_kit  # noqa: E402

# 다른 레포의 검증 도구를 흉내 낸다 (검증 계약: check_red --json, verify 종료 코드, style JSON)
FAKE_TOOL = r"""
import json, sys
kind = sys.argv[1]
if kind == "check_red":
    expect = sys.argv[sys.argv.index("--expect") + 1]
    verdict = "GREEN_OK" if expect == "green" else "RED_OK"
    outcome = "passed" if expect == "green" else "assertion_failed"
    tests = [{"id": "t", "outcome": outcome}]
    print(json.dumps({"verdict": verdict, "exit_code": 0, "tests": tests}))
elif kind == "style":
    print(json.dumps({"ok": True, "problems": []}))
elif kind == "verify":
    print("verify: PASS (exit 0)")
elif kind == "validator":
    sys.stdin.read()
    print(json.dumps({"type": "result", "result": '{"verdict": "PASS", "issues": []}'}))
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """백엔드·프론트엔드 구조가 아닌 다른 레포 (src/, tests/)."""
    repo = tmp_path / "other"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "src" / "secret.py").write_text("TOKEN = None\n", encoding="utf-8")
    (repo / "tests" / "acceptance").mkdir(parents=True)
    (repo / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n", "utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def test_install_copies_kit_and_creates_templates(target: Path) -> None:
    (target / "AGENTS.md").write_text("# 기존 규칙\n", encoding="utf-8")
    assert factory_kit.main(["install", str(target)]) == 0
    for rel in factory_kit.KIT_FILES:
        assert (target / rel).exists(), rel
    # 레포가 이미 가진 정책 파일은 덮어쓰지 않는다
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "# 기존 규칙\n"
    assert (target / "CLAUDE.md").read_text(encoding="utf-8").startswith("@AGENTS.md")
    config = json.loads((target / "factory/config.json").read_text(encoding="utf-8"))
    assert config["acceptance_tests"] == []
    # 샘플 레포 전용 파일은 키트에 넣지 않는다
    assert not (target / "scripts/verify.py").exists()


def test_install_does_not_overwrite_without_force(target: Path) -> None:
    factory_kit.main(["install", str(target)])
    (target / "factory/PROCESS.md").write_text("바꾼 절차\n", encoding="utf-8")
    factory_kit.main(["install", str(target)])
    assert (target / "factory/PROCESS.md").read_text(encoding="utf-8") == "바꾼 절차\n"
    factory_kit.main(["install", str(target), "--force"])
    assert (target / "factory/PROCESS.md").read_text(encoding="utf-8") != "바꾼 절차\n"


def test_check_reports_missing_contract_and_config(
    target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    factory_kit.main(["install", str(target)])
    capsys.readouterr()
    assert factory_kit.main(["check", str(target)]) == 1
    out = capsys.readouterr().out
    assert "scripts/verify.py" in out
    assert "acceptance_tests" in out


def configure(target: Path) -> None:
    config = {
        "lock_patterns": ["tests/*.py", "pyproject.toml"],
        "high_paths": ["src/secret.py"],
        "test_patterns": ["tests/*"],
        "changed_test": r"^tests/(\w+/)*test_\w+\.py$",
        "acceptance_tests": [
            {
                "pattern": r"^tests/acceptance/test_{id}\.py$",
                "example": "tests/acceptance/test_{id}.py",
            }
        ],
    }
    (target / "factory/config.json").write_text(json.dumps(config), encoding="utf-8")
    for name in ("verify.py", "check_red.py", "check_style.py"):
        (target / "scripts" / name).write_text("# 레포의 검증 도구\n", encoding="utf-8")


def factory(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(target / "scripts/factory.py"), *args],
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_installed_kit_runs_a_job_in_another_layout(
    target: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory_kit.main(["install", str(target)])
    configure(target)
    capsys.readouterr()
    assert factory_kit.main(["check", str(target)]) == 0
    git(target, "add", "-A")
    git(target, "commit", "-qm", "팩토리 키트 설치")
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    for kind in ("check_red", "style", "verify", "validator"):
        monkeypatch.setenv(
            f"FACTORY_{kind.upper()}_CMD", json.dumps([sys.executable, str(tool), kind])
        )

    assert factory(target, "new", "덧셈 결과를 정수로").returncode == 0
    contract = {
        "type": "feature", "size": "small", "risk": "LOW", "risk_reason": "작은 변경",
        "acceptance_criteria": ["add가 정수를 돌려준다"],
        "acceptance_tests": ["tests/acceptance/test_J001.py"],
        "changed_tests": [], "allowed_config_changes": [],
    }  # fmt: skip
    (target / "factory/specs").mkdir(parents=True, exist_ok=True)
    (target / "factory/specs/J001.md").write_text(
        f"# J001\n\n```json\n{json.dumps(contract)}\n```\n", encoding="utf-8"
    )
    (target / "tests/acceptance/test_J001.py").write_text(
        "def test_x():\n    assert False\n", "utf-8"
    )
    for step in ("spec", "red"):
        assert factory(target, step, "J001").returncode == 0, step
    (target / "src/calc.py").write_text("def add(a, b):\n    return int(a + b)\n", encoding="utf-8")
    for step in ("verify", "validate", "merge"):
        done = factory(target, step, "J001")
        assert done.returncode == 0, (step, done.stdout)
    state = json.loads((target / "factory/state.json").read_text(encoding="utf-8"))
    assert state["jobs"]["J001"]["status"] == "MERGED"
    assert "int(a + b)" in (target / "src/calc.py").read_text(encoding="utf-8")


def test_installed_kit_uses_the_repo_risk_paths(
    target: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory_kit.main(["install", str(target)])
    configure(target)
    git(target, "add", "-A")
    git(target, "commit", "-qm", "팩토리 키트 설치")
    sys.path.insert(0, str(target / "scripts"))
    cfg = factory_kit.load_target_config(target)
    assert "src/secret.py" in cfg.high_paths
    assert "factory/config.json" in cfg.lock_patterns
