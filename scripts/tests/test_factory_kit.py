import importlib.util
import json
import os
import py_compile
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import factory_kit  # noqa: E402

TAG = "factory-v900"

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
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """엔진 원본 레포(EIFactory)를 흉내 낸다: 지금 작업 트리의 키트 파일을 커밋하고 태그를 단다."""
    repo = tmp_path / "eifactory"
    rels = {*factory_kit.ENGINE_FILES, *factory_kit.COPIED, *factory_kit.TEMPLATES.values()}
    for rel in rels:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(factory_kit.SOURCE / rel, repo / rel)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "engine")
    git(repo, "tag", TAG)
    monkeypatch.setattr(factory_kit, "SOURCE", repo)
    monkeypatch.setenv(factory_kit.ENGINE_HOME_ENV, str(tmp_path / "engines"))
    return repo


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


def install(target: Path, *extra: str) -> int:
    return factory_kit.main(["install", str(target), "--engine", TAG, *extra])


def engine_dir(tag: str = TAG) -> Path:
    return Path(os.environ[factory_kit.ENGINE_HOME_ENV]) / tag


def run(target: Path, script: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(target / script), *args],
        cwd=target,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_install_puts_only_repo_files_in_target(source: Path, target: Path) -> None:
    (target / "AGENTS.md").write_text("# 기존 규칙\n", encoding="utf-8")
    assert install(target) == 0
    for rel in [*factory_kit.SHIMS, *factory_kit.COPIED]:
        assert (target / rel).exists(), rel
    # 엔진 본체는 대상 레포에 두지 않는다 (얇은 설치, 기획안 12.1)
    for rel in ("scripts/factory_config.py", "scripts/monitor_page.html",
                "factory/prompts/validator.md", "docker/proxy/filter"):  # fmt: skip
        assert not (target / rel).exists(), rel
        assert (engine_dir() / rel).exists(), rel
    # shim은 엔진을 부르기만 한다
    for rel in factory_kit.SHIMS:
        text = (target / rel).read_text(encoding="utf-8")
        assert "factory_engine" in text and len(text.splitlines()) < 15, rel
    # 레포가 이미 가진 정책 파일은 덮어쓰지 않는다
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "# 기존 규칙\n"
    assert (target / "CLAUDE.md").read_text(encoding="utf-8").startswith("@AGENTS.md")
    config = json.loads((target / "factory/config.json").read_text(encoding="utf-8"))
    assert config["acceptance_tests"] == []
    # 샘플 레포 전용 파일은 키트에 넣지 않는다
    assert not (target / "scripts/verify.py").exists()


def test_engine_comes_from_the_tag_not_the_working_tree(source: Path, target: Path) -> None:
    (source / "factory/PROCESS.md").write_text("커밋 안 한 변경\n", encoding="utf-8")
    assert install(target) == 0
    assert (engine_dir() / "factory/PROCESS.md").read_text(encoding="utf-8") != "커밋 안 한 변경\n"
    assert (target / "factory/PROCESS.md").read_bytes() == (
        engine_dir() / "factory/PROCESS.md"
    ).read_bytes()
    # 엔진 폴더는 읽기 전용으로 둔다 (세션이 실수로 고치지 않게)
    assert not os.access(engine_dir() / "scripts/factory.py", os.W_OK)


def test_install_needs_a_factory_tag(
    source: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert factory_kit.main(["install", str(target), "--engine", "main"]) == 2
    assert factory_kit.main(["install", str(target), "--engine", "factory-v999"]) == 2
    assert "factory-v" in capsys.readouterr().out
    assert not (target / "scripts/factory.py").exists()


def test_installed_repo_cannot_install_the_kit_further(source: Path, target: Path) -> None:
    # 설치된 레포가 또 다른 레포에 복사본의 복사본을 퍼뜨리지 않는다 (설치는 원본 레포에서만)
    install(target)
    assert "scripts/factory_kit.py" not in factory_kit.ENGINE_FILES
    assert not (target / "scripts/factory_kit.py").exists()
    assert not (engine_dir() / "scripts/factory_kit.py").exists()


def test_install_records_engine_version_and_hashes(source: Path, target: Path) -> None:
    install(target)
    lock = json.loads((target / "factory/kit.lock").read_text(encoding="utf-8"))
    assert lock["engine"] == TAG
    assert lock["engine_commit"] == git(source, "rev-parse", f"{TAG}^{{commit}}")
    assert set(lock["engine_files"]) == set(factory_kit.ENGINE_FILES)
    assert set(lock["files"]) == {*factory_kit.SHIMS, *factory_kit.COPIED}
    assert all(len(v) == 64 for v in [*lock["files"].values(), *lock["engine_files"].values()])
    # 설치 기록은 잠그고 HIGH로 본다 (작업이 바꾸면 사람 승인)
    cfg = factory_kit.load_target_config(target)
    assert "factory/kit.lock" in cfg.high_paths or any(
        p == "factory/kit.lock" for p in cfg.lock_patterns
    )


def test_check_reports_repo_files_changed_since_install(
    source: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(target)
    configure(target)
    capsys.readouterr()
    assert factory_kit.main(["check", str(target)]) == 0
    (target / "scripts/factory.py").write_text("# 손으로 고침\n", encoding="utf-8")
    capsys.readouterr()
    assert factory_kit.main(["check", str(target)]) == 1
    out = capsys.readouterr().out
    assert "scripts/factory.py" in out and "설치 기록과 다름" in out


def make_writable(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def test_check_reports_engine_changed_or_missing(
    source: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(target)
    configure(target)
    changed = engine_dir() / "scripts/dispatch.py"
    make_writable(changed)
    changed.write_text("# 엔진을 고침\n", encoding="utf-8")
    capsys.readouterr()
    assert factory_kit.main(["check", str(target)]) == 1
    out = capsys.readouterr().out
    assert "엔진" in out and "scripts/dispatch.py" in out
    for path in engine_dir().rglob("*"):
        make_writable(path)
    shutil.rmtree(engine_dir())
    capsys.readouterr()
    assert factory_kit.main(["check", str(target)]) == 1
    assert "엔진 없음" in capsys.readouterr().out


def test_shim_refuses_a_changed_engine(source: Path, target: Path) -> None:
    install(target)
    configure(target)
    assert run(target, "scripts/factory.py", "status").returncode == 0
    changed = engine_dir() / "scripts/factory_config.py"
    make_writable(changed)
    changed.write_text(changed.read_text(encoding="utf-8") + "\n# 바꿈\n", encoding="utf-8")
    done = run(target, "scripts/factory.py", "status")
    assert done.returncode != 0
    assert "엔진" in done.stdout + done.stderr


def test_shim_ignores_planted_bytecode_cache(source: Path, target: Path, tmp_path: Path) -> None:
    # 해시를 대조하지 않고 읽히는 .pyc를 엔진 옆에 심어도 엔진 원본이 실행된다
    install(target)
    configure(target)
    module = engine_dir() / "scripts/factory_config.py"
    evil = tmp_path / "evil.py"
    evil.write_text("print('PLANTED'); raise SystemExit(0)\n", encoding="utf-8")
    cfile = importlib.util.cache_from_source(str(module))
    py_compile.compile(
        str(evil), cfile=cfile, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH
    )
    done = run(target, "scripts/factory.py", "status")
    assert "PLANTED" not in done.stdout
    assert done.returncode == 0, done.stderr
    shutil.rmtree(Path(cfile).parent)
    run(target, "scripts/factory.py", "status")
    assert not Path(cfile).parent.exists()  # 엔진 폴더에 캐시를 쓰지 않는다


def test_hook_shim_runs_the_engine_guard_on_this_repo(source: Path, target: Path) -> None:
    install(target)
    configure(target)
    # 대상 레포의 설정을 읽으면 통과, 엔진 폴더를 레포로 잘못 보면 설정이 없어 막힌다
    done = run(target, "scripts/hooks/guard.py", stdin=json.dumps({"tool_name": "Read"}))
    assert done.returncode == 0, done.stdout + done.stderr


def test_install_does_not_overwrite_without_force(source: Path, target: Path) -> None:
    install(target)
    (target / "factory/PROCESS.md").write_text("바꾼 절차\n", encoding="utf-8")
    install(target)
    assert (target / "factory/PROCESS.md").read_text(encoding="utf-8") == "바꾼 절차\n"
    install(target, "--force")
    assert (target / "factory/PROCESS.md").read_text(encoding="utf-8") != "바꾼 절차\n"


def next_tag(source: Path, tag: str) -> None:
    (source / "factory/PROCESS.md").write_text("새 절차\n", encoding="utf-8")
    git(source, "commit", "-qam", "new engine")
    git(source, "tag", tag)


def test_upgrade_shows_the_difference_before_changing(
    source: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(target)
    configure(target)
    next_tag(source, "factory-v901")
    capsys.readouterr()
    assert factory_kit.main(["upgrade", str(target), "--engine", "factory-v901"]) == 0
    out = capsys.readouterr().out
    assert "factory/PROCESS.md" in out and "새 절차" in out and "--yes" in out
    lock = json.loads((target / "factory/kit.lock").read_text(encoding="utf-8"))
    assert lock["engine"] == TAG  # 보여 주기만 하고 바꾸지 않았다

    assert factory_kit.main(["upgrade", str(target), "--engine", "factory-v901", "--yes"]) == 0
    lock = json.loads((target / "factory/kit.lock").read_text(encoding="utf-8"))
    assert lock["engine"] == "factory-v901"
    assert (target / "factory/PROCESS.md").read_text(encoding="utf-8") == "새 절차\n"
    capsys.readouterr()
    assert factory_kit.main(["check", str(target)]) == 0


def test_upgrade_refuses_when_repo_files_were_edited(
    source: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(target)
    configure(target)
    (target / "factory/PROCESS.md").write_text("손으로 고친 절차\n", encoding="utf-8")
    next_tag(source, "factory-v901")
    capsys.readouterr()
    assert factory_kit.main(["upgrade", str(target), "--engine", "factory-v901", "--yes"]) == 1
    assert "factory/PROCESS.md" in capsys.readouterr().out
    assert (target / "factory/PROCESS.md").read_text(encoding="utf-8") == "손으로 고친 절차\n"


def test_check_reports_missing_contract_and_config(
    source: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(target)
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


def test_installed_kit_runs_a_job_in_another_layout(
    source: Path,
    target: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install(target)
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

    assert run(target, "scripts/factory.py", "new", "덧셈 결과를 정수로").returncode == 0
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
        done = run(target, "scripts/factory.py", step, "J001")
        assert done.returncode == 0, (step, done.stdout, done.stderr)
    (target / "src/calc.py").write_text("def add(a, b):\n    return int(a + b)\n", encoding="utf-8")
    for step in ("verify", "validate", "merge"):
        done = run(target, "scripts/factory.py", step, "J001")
        assert done.returncode == 0, (step, done.stdout, done.stderr)
    state = json.loads((target / "factory/state.json").read_text(encoding="utf-8"))
    assert state["jobs"]["J001"]["status"] == "MERGED"
    assert "int(a + b)" in (target / "src/calc.py").read_text(encoding="utf-8")
    assert not (engine_dir() / "factory/state.json").exists()


def test_installed_kit_uses_the_repo_risk_paths(source: Path, target: Path) -> None:
    install(target)
    configure(target)
    git(target, "add", "-A")
    git(target, "commit", "-qm", "팩토리 키트 설치")
    cfg = factory_kit.load_target_config(target)
    assert "src/secret.py" in cfg.high_paths
    assert "factory/config.json" in cfg.lock_patterns
