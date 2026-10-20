import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import factory  # noqa: E402

FAKE_CMD = r"""
import json, os, sys
control_path = os.environ["FAKE_CONTROL"]
control = json.load(open(control_path, encoding="utf-8"))
kind = sys.argv[1]
stdin = sys.stdin.read() if kind == "validator" else ""
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"kind": kind, "args": sys.argv[2:], "stdin": stdin}) + "\n")

def take(key):
    value = control[key]
    if isinstance(value, list):
        item = value.pop(0)
        json.dump(control, open(control_path, "w", encoding="utf-8"))
        return item
    return value

if kind == "check_red":
    expect = sys.argv[sys.argv.index("--expect") + 1]
    result = take("green" if expect == "green" else "red")
    print("verdict line")
    print(json.dumps(result))
    sys.exit(result["exit_code"])
if kind == "verify":
    result = take("verify")
    print(result["output"])
    sys.exit(result["exit"])
if kind == "validator":
    print(json.dumps({"type": "result", "result": take("validator")}))
"""

RED_OK = {
    "verdict": "RED_OK",
    "exit_code": 0,
    "tests": [{"id": "t", "outcome": "assertion_failed"}],
}
PASSING = {
    "verdict": "PASSING_BEFORE_IMPL",
    "exit_code": 1,
    "tests": [{"id": "t", "outcome": "passed"}],
}
GREEN_OK = {"verdict": "GREEN_OK", "exit_code": 0, "tests": [{"id": "t", "outcome": "passed"}]}
VERIFY_OK = {"exit": 0, "output": "verify: PASS (exit 0)"}


def verify_fail(step: str) -> dict[str, Any]:
    return {"exit": 1, "output": f"{step}  FAIL\nverify: FAIL (exit 1)"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


class Harness:
    def __init__(self, repo: Path, control: Path, log: Path, monkeypatch: pytest.MonkeyPatch):
        self.repo = repo
        self.control = control
        self.log = log
        self.monkeypatch = monkeypatch
        self.now = 1_000_000.0
        self.set_now(self.now)

    def set_now(self, value: float) -> None:
        self.now = value
        self.monkeypatch.setenv("FACTORY_FAKE_NOW", str(value))

    def advance(self, seconds: float) -> None:
        self.set_now(self.now + seconds)

    def configure(self, **control: Any) -> None:
        self.control.write_text(json.dumps(control), encoding="utf-8")

    def run(self, *argv: str) -> int:
        return factory.main(list(argv), root=self.repo)

    def job(self, job_id: str = "J001") -> dict[str, Any]:
        state = json.loads((self.repo / "factory" / "state.json").read_text(encoding="utf-8"))
        job: dict[str, Any] = state["jobs"][job_id]
        return job

    def state(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(
            (self.repo / "factory" / "state.json").read_text(encoding="utf-8")
        )
        return loaded

    def calls(self, kind: str) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if r["kind"] == kind]

    def write_spec(self, job_id: str = "J001", **overrides: Any) -> None:
        contract: dict[str, Any] = {
            "type": "feature",
            "size": "small",
            "risk": "LOW",
            "risk_reason": "작은 변경",
            "acceptance_criteria": ["무언가가 보인다"],
            "acceptance_tests": [f"backend/tests/acceptance/test_{job_id}.py"],
            "changed_tests": [],
            "allowed_config_changes": [],
        }
        contract.update(overrides)
        spec = self.repo / "factory" / "specs" / f"{job_id}.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(
            f"# {job_id}\n\n설명\n\n```json\n{json.dumps(contract, ensure_ascii=False)}\n```\n",
            encoding="utf-8",
        )
        for test in contract["acceptance_tests"]:
            path = self.repo / test
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_new():\n    assert False\n", encoding="utf-8")

    def to_building(self) -> None:
        self.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator="")
        assert self.run("new", "요구사항") == 0
        self.write_spec()
        assert self.run("spec", "J001") == 0
        assert self.run("red", "J001") == 0
        assert self.job()["status"] == "BUILDING"


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    repo = tmp_path / "repo"
    for rel, text in {
        "Makefile": "verify:\n\techo ok\n",
        "AGENTS.md": "# rules\n",
        "scripts/verify.py": "print('verify')\n",
        "backend/pyproject.toml": "[project]\nname='x'\n",
        "backend/src/bookmarks/auth.py": "TOKENS = set()\n",
        "backend/src/bookmarks/repository.py": "SCHEMA = 'CREATE TABLE x (id INTEGER)'\n",
        "backend/src/bookmarks/main.py": "def list_items():\n    return []\n",
        "backend/tests/test_existing.py": "def test_existing():\n    assert True\n",
        "backend/tests/acceptance/__init__.py": "",
        "frontend/src/App.test.tsx": "it('x', () => {});\n",
        "factory/PROCESS.md": "# process\n",
    }.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")

    fake = tmp_path / "fake_cmd.py"
    fake.write_text(FAKE_CMD, encoding="utf-8")
    control = tmp_path / "control.json"
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CONTROL", str(control))
    monkeypatch.setenv("FAKE_LOG", str(log))
    for kind in ("check_red", "verify", "validator"):
        monkeypatch.setenv(
            f"FACTORY_{kind.upper()}_CMD", json.dumps([sys.executable, str(fake), kind])
        )
    return Harness(repo, control, log, monkeypatch)


# ---------- new ----------


def test_new_creates_job_branch_and_state_files(h: Harness) -> None:
    assert h.run("new", "빈 화면 문구를 바꿔줘") == 0
    job = h.job()
    assert job["status"] == "QUEUED"
    assert job["requirement"] == "빈 화면 문구를 바꿔줘"
    assert job["branch"] == "factory/J001"
    assert git(h.repo, "branch", "--show-current") == "factory/J001"
    assert h.state()["active"] == "J001"
    assert "J001" in (h.repo / "factory-state.md").read_text(encoding="utf-8")


def test_new_refuses_while_a_job_is_active(h: Harness) -> None:
    assert h.run("new", "첫 번째") == 0
    assert h.run("new", "두 번째") != 0
    assert list(h.state()["jobs"]) == ["J001"]


def test_new_refuses_dirty_worktree(h: Harness) -> None:
    (h.repo / "AGENTS.md").write_text("changed\n", encoding="utf-8")
    assert h.run("new", "요구사항") != 0
    assert not (h.repo / "factory" / "state.json").exists()


# ---------- spec ----------


def test_spec_accepts_valid_contract(h: Harness) -> None:
    h.run("new", "요구사항")
    h.write_spec()
    assert h.run("spec", "J001") == 0
    job = h.job()
    assert job["status"] == "SPEC_READY"
    assert job["type"] == "feature"
    assert job["acceptance_tests"] == ["backend/tests/acceptance/test_J001.py"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "chore"},
        {"size": "large"},
        {"risk": "NONE"},
        {"acceptance_tests": []},
        {"acceptance_tests": ["backend/tests/test_bookmarks.py"]},
        {"acceptance_tests": ["frontend/src/List.accept.test.tsx"]},
    ],
)
def test_spec_rejects_invalid_contract(h: Harness, overrides: dict[str, Any]) -> None:
    h.run("new", "요구사항")
    h.write_spec(**overrides)
    assert h.run("spec", "J001") != 0
    assert h.job()["status"] == "QUEUED"


def test_spec_requires_a_json_contract_block(h: Harness) -> None:
    h.run("new", "요구사항")
    spec = h.repo / "factory" / "specs" / "J001.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 계약 없음\n", encoding="utf-8")
    assert h.run("spec", "J001") != 0


# ---------- red ----------


def test_red_ok_locks_files_and_moves_to_building(h: Harness) -> None:
    h.to_building()
    locks = h.job()["locks"]
    for path in (
        "backend/tests/acceptance/test_J001.py",
        "factory/specs/J001.md",
        "backend/tests/test_existing.py",
        "frontend/src/App.test.tsx",
        "Makefile",
        "scripts/verify.py",
        "backend/pyproject.toml",
    ):
        assert path in locks, path
    args = h.calls("check_red")[0]["args"]
    assert args[:2] == ["--expect", "red"]
    assert "backend/tests/acceptance/test_J001.py" in args


def test_red_failure_allows_one_spec_rewrite_then_blocks(h: Harness) -> None:
    h.configure(red=[PASSING, PASSING], green=GREEN_OK, verify=VERIFY_OK, validator="")
    h.run("new", "요구사항")
    h.write_spec()
    h.run("spec", "J001")
    assert h.run("red", "J001") != 0
    assert h.job()["status"] == "SPEC_READY"
    assert h.job()["spec_revisions"] == 1
    h.run("spec", "J001")
    assert h.run("red", "J001") != 0
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "SPEC_INVALID"


def test_red_for_refactor_expects_green(h: Harness) -> None:
    h.configure(red=PASSING, green=GREEN_OK, verify=VERIFY_OK, validator="")
    h.run("new", "리팩터링")
    h.write_spec(type="refactor", risk="MEDIUM")
    h.run("spec", "J001")
    assert h.run("red", "J001") == 0
    assert h.calls("check_red")[0]["args"][:2] == ["--expect", "green"]


def test_red_requires_spec_ready(h: Harness) -> None:
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator="")
    h.run("new", "요구사항")
    assert h.run("red", "J001") != 0
    assert h.calls("check_red") == []


# ---------- verify ----------


def test_verify_success_moves_to_verifying(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/src/bookmarks/main.py").write_text("def list_items():\n    return [1]\n")
    assert h.run("verify", "J001") == 0
    job = h.job()
    assert job["status"] == "VERIFYING"
    assert job["risk"] == "LOW"
    assert job["validation"][-1]["green_verdict"] == "GREEN_OK"
    assert job["validation"][-1]["verify_exit"] == 0


def test_verify_blocks_when_locked_test_changes(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/tests/acceptance/test_J001.py").write_text("def test_new():\n    pass\n")
    assert h.run("verify", "J001") != 0
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "PROTECTED_FILE_CHANGED"
    assert "backend/tests/acceptance/test_J001.py" in job["blocked"]["detail"]
    assert h.calls("verify") == []


def test_verify_blocks_when_verification_config_changes(h: Harness) -> None:
    h.to_building()
    (h.repo / "Makefile").write_text("verify:\n\ttrue\n", encoding="utf-8")
    assert h.run("verify", "J001") != 0
    assert h.job()["blocked"]["reason"] == "PROTECTED_FILE_CHANGED"


def test_verify_failure_requires_retry(h: Harness) -> None:
    h.to_building()
    h.configure(red=RED_OK, green=GREEN_OK, verify=[verify_fail("lint")], validator="")
    assert h.run("verify", "J001") != 0
    job = h.job()
    assert job["status"] == "RETRY_REQUIRED"
    assert job["attempts"] == 1


def test_same_failure_twice_blocks(h: Harness) -> None:
    h.to_building()
    h.configure(
        red=RED_OK, green=GREEN_OK, verify=[verify_fail("lint"), verify_fail("lint")], validator=""
    )
    h.run("verify", "J001")
    h.run("verify", "J001")
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "REPEATED_FAILURE"


def test_three_failures_block(h: Harness) -> None:
    h.to_building()
    h.configure(
        red=RED_OK,
        green=GREEN_OK,
        verify=[verify_fail("lint"), verify_fail("typecheck"), verify_fail("test")],
        validator="",
    )
    for _ in range(3):
        h.run("verify", "J001")
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "RETRY_LIMIT"


def test_acceptance_tests_not_green_is_a_failure(h: Harness) -> None:
    h.to_building()
    not_green = {"verdict": "NOT_GREEN", "exit_code": 1, "tests": []}
    h.configure(red=RED_OK, green=[not_green], verify=VERIFY_OK, validator="")
    assert h.run("verify", "J001") != 0
    assert h.job()["status"] == "RETRY_REQUIRED"


# ---------- risk ----------


def test_risk_auth_change_is_high(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/src/bookmarks/auth.py").write_text("TOKENS = {}\n", encoding="utf-8")
    h.run("verify", "J001")
    assert h.job()["risk"] == "HIGH"


def test_risk_new_delete_statement_is_high(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/src/bookmarks/main.py").write_text(
        "SQL = 'DELETE FROM bookmarks'\n", encoding="utf-8"
    )
    h.run("verify", "J001")
    assert h.job()["risk"] == "HIGH"


def test_risk_schema_change_is_medium(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/src/bookmarks/repository.py").write_text(
        "SCHEMA = 'CREATE TABLE x (id INTEGER, fav INTEGER)'\n", encoding="utf-8"
    )
    h.run("verify", "J001")
    assert h.job()["risk"] == "MEDIUM"


def test_risk_new_skip_marker_is_medium(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/tests/test_slow.py").write_text(
        "import pytest\n\n@pytest.mark.skip\ndef test_slow():\n    assert True\n",
        encoding="utf-8",
    )
    h.run("verify", "J001")
    job = h.job()
    assert job["risk"] == "MEDIUM"
    assert any("skip" in r for r in job["risk_reasons"])


def test_declared_risk_is_never_lowered(h: Harness) -> None:
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator="")
    h.run("new", "요구사항")
    h.write_spec(risk="HIGH")
    h.run("spec", "J001")
    h.run("red", "J001")
    h.run("verify", "J001")
    assert h.job()["risk"] == "HIGH"


# ---------- validate ----------


def test_validate_pass_reaches_approval_required(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/src/bookmarks/main.py").write_text("def list_items():\n    return [1]\n")
    h.run("verify", "J001")
    h.configure(
        red=RED_OK,
        green=GREEN_OK,
        verify=VERIFY_OK,
        validator='검토 완료\n{"verdict": "PASS", "issues": []}',
    )
    assert h.run("validate", "J001") == 0
    assert h.job()["status"] == "APPROVAL_REQUIRED"
    prompt = h.calls("validator")[0]["stdin"]
    assert "acceptance_tests" in prompt
    assert "return [1]" in prompt
    assert "GREEN_OK" in prompt


def test_validate_fail_requires_retry(h: Harness) -> None:
    h.to_building()
    h.run("verify", "J001")
    h.configure(
        red=RED_OK,
        green=GREEN_OK,
        verify=VERIFY_OK,
        validator='{"verdict": "FAIL", "issues": ["범위 밖 수정"]}',
    )
    assert h.run("validate", "J001") != 0
    job = h.job()
    assert job["status"] == "RETRY_REQUIRED"
    assert job["attempts"] == 1
    assert "범위 밖 수정" in json.dumps(job["validation"][-1], ensure_ascii=False)


def test_validate_unparseable_output_keeps_state(h: Harness) -> None:
    h.to_building()
    h.run("verify", "J001")
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator="잘 모르겠습니다")
    assert h.run("validate", "J001") != 0
    assert h.job()["status"] == "VERIFYING"
    assert h.job()["attempts"] == 0


def test_validate_requires_verifying(h: Harness) -> None:
    h.to_building()
    assert h.run("validate", "J001") != 0
    assert h.calls("validator") == []


# ---------- time ----------


def test_verify_after_hard_cap_blocks_with_timeout(h: Harness) -> None:
    h.to_building()
    h.advance(21 * 60)
    assert h.run("verify", "J001") != 0
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "TIMEOUT"
    assert h.calls("verify") == []


def test_failure_after_soft_cap_blocks_instead_of_retry(h: Harness) -> None:
    h.to_building()
    h.configure(red=RED_OK, green=GREEN_OK, verify=[verify_fail("lint")], validator="")
    h.advance(17 * 60)
    h.run("verify", "J001")
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "TIMEOUT"


def test_time_stops_while_waiting_for_approval(h: Harness) -> None:
    h.to_building()
    h.run("verify", "J001")
    h.configure(
        red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator='{"verdict": "PASS", "issues": []}'
    )
    h.run("validate", "J001")
    elapsed = h.job()["time"]["active_s"]
    h.advance(3600)
    h.run("status")
    assert factory.active_seconds(h.job(), now=h.now) == elapsed


def test_resume_extend_once(h: Harness) -> None:
    h.to_building()
    h.advance(21 * 60)
    h.run("verify", "J001")
    assert h.run("resume", "J001", "--extend") == 0
    job = h.job()
    assert job["status"] == "BUILDING"
    assert job["time"]["cap_s"] == 30 * 60
    h.advance(10 * 60)
    h.run("verify", "J001")
    assert h.job()["blocked"]["reason"] == "TIMEOUT"
    assert h.run("resume", "J001", "--extend") != 0


# ---------- human commands ----------


def test_approve_records_human_decision_and_releases_job(h: Harness) -> None:
    h.to_building()
    h.run("verify", "J001")
    h.configure(
        red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator='{"verdict": "PASS", "issues": []}'
    )
    h.run("validate", "J001")
    assert h.run("approve", "J001") == 0
    assert h.job()["status"] == "APPROVED"
    assert h.job()["approval"]["decision"] == "approved"
    assert h.state()["active"] is None


def test_approve_reject_sends_back_to_retry(h: Harness) -> None:
    h.to_building()
    h.run("verify", "J001")
    h.configure(
        red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator='{"verdict": "PASS", "issues": []}'
    )
    h.run("validate", "J001")
    assert h.run("approve", "J001", "--reject", "문구가 어색함") == 0
    assert h.job()["status"] == "RETRY_REQUIRED"


def test_approve_requires_approval_required(h: Harness) -> None:
    h.to_building()
    assert h.run("approve", "J001") != 0


def test_block_and_drop(h: Harness) -> None:
    h.to_building()
    assert h.run("block", "J001", "--reason", "TEST_INVALID", "--detail", "기대값 오타") == 0
    assert h.job()["status"] == "BLOCKED"
    assert h.job()["blocked"]["reason"] == "TEST_INVALID"
    assert h.run("drop", "J001") == 0
    assert h.job()["status"] == "DROPPED"
    assert h.state()["active"] is None


def test_resume_returns_to_checkpoint(h: Harness) -> None:
    h.to_building()
    h.run("block", "J001", "--reason", "MANUAL", "--detail", "잠시 중단")
    assert h.run("resume", "J001") == 0
    assert h.job()["status"] == "BUILDING"


def test_status_json(h: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    h.run("new", "요구사항")
    capsys.readouterr()
    assert h.run("status", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["active"] == "J001"
    assert data["jobs"]["J001"]["status"] == "QUEUED"


def test_approve_commits_job_branch_and_returns_to_base(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/src/bookmarks/main.py").write_text("def list_items():\n    return [1]\n")
    h.run("verify", "J001")
    h.configure(
        red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator='{"verdict": "PASS", "issues": []}'
    )
    h.run("validate", "J001")
    h.run("approve", "J001")
    assert git(h.repo, "branch", "--show-current") == "main"
    assert git(h.repo, "status", "--porcelain", "--untracked-files=normal") == ""
    changed = git(h.repo, "diff", "--name-only", "main", "factory/J001").splitlines()
    assert "backend/src/bookmarks/main.py" in changed
    assert "backend/tests/acceptance/test_J001.py" in changed
    assert "factory/specs/J001.md" in changed
    assert "factory/state.json" not in changed
    assert h.job()["commit"]


def test_next_job_starts_from_base_after_approve(h: Harness) -> None:
    test_approve_commits_job_branch_and_returns_to_base(h)
    assert h.run("new", "다음 작업") == 0
    assert h.job("J002")["base_commit"] == git(h.repo, "rev-parse", "main")


def test_drop_keeps_work_on_branch_and_returns_to_base(h: Harness) -> None:
    h.to_building()
    (h.repo / "backend/src/bookmarks/main.py").write_text("def list_items():\n    return [2]\n")
    h.run("drop", "J001")
    assert git(h.repo, "branch", "--show-current") == "main"
    assert "return []" in (h.repo / "backend/src/bookmarks/main.py").read_text()
    assert "return [2]" in git(h.repo, "show", "factory/J001:backend/src/bookmarks/main.py")


def test_new_skips_ids_whose_branch_already_exists(h: Harness) -> None:
    # 다른 worktree가 이미 factory/J001, J002를 쓰고 있는 경우 (브랜치는 worktree끼리 공유)
    git(h.repo, "branch", "factory/J001")
    git(h.repo, "branch", "factory/J002")
    assert h.run("new", "요구사항") == 0
    assert h.state()["active"] == "J003"
    assert git(h.repo, "branch", "--show-current") == "factory/J003"
