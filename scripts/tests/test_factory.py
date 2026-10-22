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
sys.stdin.reconfigure(encoding="utf-8")  # factory.py는 UTF-8로 넘긴다
stdin = sys.stdin.read() if kind == "validator" else ""
row = {"kind": kind, "args": sys.argv[2:], "stdin": stdin}
row["reviewer"] = os.environ.get("FACTORY_REVIEWER")
# 호출마다 파일 하나 (리뷰어는 병렬로 돌아 한 파일에 쓰면 줄이 섞인다)
import time
name = f"{time.time_ns():020d}-{os.getpid()}.json"
with open(os.path.join(os.environ["FAKE_LOG"], name), "w", encoding="utf-8") as log:
    json.dump(row, log)

def take(key):
    value = control[key]
    if key == "validator" and isinstance(value, dict):  # 리뷰어별 응답 (병렬로 돈다)
        value = value[os.environ["FACTORY_REVIEWER"]]
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
if kind == "style":
    result = control.get("style", {"ok": True, "problems": []})
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)
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
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.log.glob("*.json"))]
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

    def to_building(self, risk: str = "LOW") -> None:
        self.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator="")
        assert self.run("new", "요구사항") == 0
        self.write_spec(risk=risk)
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
    log = tmp_path / "calls"
    log.mkdir()
    monkeypatch.setenv("FAKE_CONTROL", str(control))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FACTORY_MERGE_WAIT_S", "0")
    for kind in ("check_red", "style", "verify", "validator"):
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
        {"changed_tests": ["frontend/--config=evil.js"]},
        {"changed_tests": ["backend/tests/../../secret.py"]},
        {"changed_tests": ["backend/src/bookmarks/main.py"]},
        {"changed_tests": ["backend/tests/test_missing.py"]},
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


def test_red_checks_style_of_tests_before_locking(h: Harness) -> None:
    h.configure(
        red=RED_OK,
        green=GREEN_OK,
        verify=VERIFY_OK,
        validator="",
        style={"ok": False, "problems": ["backend format: 다시 포맷 필요"]},
    )
    h.run("new", "요구사항")
    h.write_spec(changed_tests=["backend/tests/test_existing.py"])
    h.run("spec", "J001")
    assert h.run("red", "J001") != 0
    job = h.job()
    assert job["status"] == "SPEC_READY"
    assert job["spec_revisions"] == 0
    assert job["locks"] == {}
    assert h.calls("check_red") == []
    args = h.calls("style")[0]["args"]
    assert "backend/tests/acceptance/test_J001.py" in args
    assert "backend/tests/test_existing.py" in args


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


def test_validate_pass_low_is_auto_approved(h: Harness) -> None:
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
    job = h.job()
    assert job["status"] == "APPROVED"
    assert job["approval"]["by"] == "auto"
    assert h.state()["active"] == "J001"
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
    h.advance(16 * 60)
    assert h.run("verify", "J001") != 0
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "TIMEOUT"
    assert h.calls("verify") == []


def test_small_job_runs_until_fifteen_minutes(h: Harness) -> None:
    h.to_building()
    h.advance(14 * 60)
    assert h.run("verify", "J001") == 0
    assert h.job()["status"] == "VERIFYING"


def test_failure_before_soft_cap_still_retries(h: Harness) -> None:
    h.to_building()
    h.configure(red=RED_OK, green=GREEN_OK, verify=[verify_fail("lint")], validator="")
    h.advance(11 * 60)
    h.run("verify", "J001")
    assert h.job()["status"] == "RETRY_REQUIRED"


def test_failure_after_soft_cap_blocks_instead_of_retry(h: Harness) -> None:
    h.to_building()
    h.configure(red=RED_OK, green=GREEN_OK, verify=[verify_fail("lint")], validator="")
    h.advance(int(12.5 * 60))
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
    h.advance(16 * 60)
    h.run("verify", "J001")
    assert h.run("resume", "J001", "--extend") == 0
    job = h.job()
    assert job["status"] == "BUILDING"
    assert job["time"]["cap_s"] == int(22.5 * 60)
    h.advance(7 * 60)
    h.run("verify", "J001")
    assert h.job()["blocked"]["reason"] == "TIMEOUT"
    assert h.run("resume", "J001", "--extend") != 0


# ---------- human commands ----------


def test_approve_records_human_decision_and_merges(h: Harness) -> None:
    h.to_building(risk="MEDIUM")
    h.run("verify", "J001")
    h.configure(
        red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator='{"verdict": "PASS", "issues": []}'
    )
    h.run("validate", "J001")
    assert h.run("approve", "J001") == 0
    assert h.job()["status"] == "MERGED"
    assert h.job()["approval"]["decision"] == "approved"
    assert h.job()["approval"]["by"] == "human"
    assert h.state()["active"] is None


def test_approve_reject_sends_back_to_retry(h: Harness) -> None:
    h.to_building(risk="MEDIUM")
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


def test_approve_merges_job_branch_into_base(h: Harness) -> None:
    h.to_building(risk="MEDIUM")
    (h.repo / "backend/src/bookmarks/main.py").write_text("def list_items():\n    return [1]\n")
    h.run("verify", "J001")
    h.configure(
        red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator='{"verdict": "PASS", "issues": []}'
    )
    h.run("validate", "J001")
    h.run("approve", "J001")
    assert git(h.repo, "branch", "--show-current") == "main"
    assert git(h.repo, "status", "--porcelain", "--untracked-files=normal") == ""
    job = h.job()
    merged = git(h.repo, "diff", "--name-only", job["base_commit"], "main").splitlines()
    assert "backend/src/bookmarks/main.py" in merged
    assert "backend/tests/acceptance/test_J001.py" in merged
    assert "factory/specs/J001.md" in merged
    assert "factory/state.json" not in merged
    # 머지 커밋 하나로 남겨 git revert -m 1 한 번으로 되돌릴 수 있다
    assert git(h.repo, "rev-parse", "main") == job["merge_commit"]
    assert len(git(h.repo, "rev-list", "--parents", "-n", "1", "main").split()) == 3


def test_next_job_starts_from_base_after_approve(h: Harness) -> None:
    test_approve_merges_job_branch_into_base(h)
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


# ---------- Phase 2: 자동 승인·머지 ----------

PASS = '{"verdict": "PASS", "issues": []}'
MAIN_PY = "backend/src/bookmarks/main.py"


def to_validated(h: Harness, risk: str = "LOW", path: str = MAIN_PY, text: str = "") -> None:
    h.to_building(risk=risk)
    (h.repo / path).write_text(text or "def list_items():\n    return [1]\n", encoding="utf-8")
    assert h.run("verify", "J001") == 0
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator=PASS)
    assert h.run("validate", "J001") == 0


def advance_main(h: Harness, path: str, text: str) -> str:
    """다른 작업이 먼저 머지된 것처럼 main을 앞으로 보낸다 (작업 브랜치는 그대로)."""
    other = h.repo.parent / "other"
    git(h.repo, "worktree", "add", "-q", str(other), "main")
    (other / path).parent.mkdir(parents=True, exist_ok=True)
    (other / path).write_text(text, encoding="utf-8")
    git(other, "add", "-A")
    git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "다른 변경")
    head = git(other, "rev-parse", "HEAD")
    git(h.repo, "worktree", "remove", "--force", str(other))
    return head


def merge_lock(h: Harness) -> Path:
    return h.repo / ".git" / "factory-merge.lock"


def test_validate_pass_above_low_waits_for_human(h: Harness) -> None:
    to_validated(h, path="backend/src/bookmarks/auth.py", text="TOKENS = {}\n")
    job = h.job()
    assert job["status"] == "APPROVAL_REQUIRED"
    assert job["risk"] == "HIGH"
    assert job["approval"] is None


def test_merge_auto_approved_low_job(h: Harness) -> None:
    to_validated(h)
    verify_calls = len(h.calls("verify"))
    assert h.run("merge", "J001") == 0
    job = h.job()
    assert job["status"] == "MERGED"
    assert h.state()["active"] is None
    assert git(h.repo, "branch", "--show-current") == "main"
    assert "return [1]" in (h.repo / MAIN_PY).read_text(encoding="utf-8")
    assert git(h.repo, "rev-parse", "main") == job["merge_commit"]
    # main이 그대로면 검증한 트리와 같으므로 재검증하지 않는다
    assert len(h.calls("verify")) == verify_calls
    assert not merge_lock(h).exists()


def test_merge_requires_approval(h: Harness) -> None:
    h.to_building()
    h.run("verify", "J001")
    assert h.run("merge", "J001") != 0
    assert h.job()["status"] == "VERIFYING"


def test_merge_rebases_and_reverifies_when_base_moved(h: Harness) -> None:
    to_validated(h)
    advance_main(h, "frontend/src/Other.tsx", "export const x = 1;\n")
    verify_calls = len(h.calls("verify"))
    assert h.run("merge", "J001") == 0
    job = h.job()
    assert job["status"] == "MERGED"
    assert len(h.calls("verify")) == verify_calls + 1
    assert (h.repo / "frontend/src/Other.tsx").exists()
    assert "return [1]" in (h.repo / MAIN_PY).read_text(encoding="utf-8")


def test_merge_conflict_blocks_and_leaves_base_untouched(h: Harness) -> None:
    to_validated(h)
    main_head = advance_main(h, MAIN_PY, "def list_items():\n    return [9]\n")
    assert h.run("merge", "J001") != 0
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "CONFLICT"
    assert git(h.repo, "rev-parse", "main") == main_head
    assert git(h.repo, "branch", "--show-current") == "factory/J001"
    assert not (h.repo / ".git" / "rebase-merge").exists()
    assert not merge_lock(h).exists()


def test_merge_reverify_failure_blocks(h: Harness) -> None:
    to_validated(h)
    main_head = advance_main(h, "frontend/src/Other.tsx", "export const x = 1;\n")
    h.configure(red=RED_OK, green=GREEN_OK, verify=[verify_fail("test")], validator=PASS)
    assert h.run("merge", "J001") != 0
    job = h.job()
    assert job["status"] == "BLOCKED"
    assert job["blocked"]["reason"] == "MERGE_CHECK_FAILED"
    assert git(h.repo, "rev-parse", "main") == main_head


def test_merge_blocks_when_tree_changed_after_verify(h: Harness) -> None:
    to_validated(h)
    (h.repo / MAIN_PY).write_text("def list_items():\n    return [666]\n", encoding="utf-8")
    base = git(h.repo, "rev-parse", "main")
    assert h.run("merge", "J001") != 0
    assert h.job()["blocked"]["reason"] == "TREE_CHANGED"
    assert git(h.repo, "rev-parse", "main") == base


def test_merge_refuses_auto_approval_if_recomputed_risk_is_not_low(h: Harness) -> None:
    # 상태 파일이 조작되어 HIGH 작업이 자동 승인으로 둔갑한 경우
    to_validated(h, path="backend/src/bookmarks/auth.py", text="TOKENS = {}\n")
    state = h.state()
    job = state["jobs"]["J001"]
    job.update(status="APPROVED", risk="LOW")
    job["approval"] = {"by": "auto", "decision": "approved", "reason": "", "at": h.now}
    (h.repo / "factory" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    base = git(h.repo, "rev-parse", "main")
    assert h.run("merge", "J001") != 0
    job = h.job()
    assert job["status"] == "APPROVAL_REQUIRED"
    assert job["risk"] == "HIGH"
    assert git(h.repo, "rev-parse", "main") == base


def test_merge_waits_while_another_job_holds_the_lock(h: Harness) -> None:
    to_validated(h)
    merge_lock(h).write_text(json.dumps({"job": "J999", "at": h.now}), encoding="utf-8")
    base = git(h.repo, "rev-parse", "main")
    assert h.run("merge", "J001") != 0
    assert h.job()["status"] == "APPROVED"
    assert git(h.repo, "rev-parse", "main") == base
    assert merge_lock(h).exists()


def test_approve_blocks_when_tree_changed_after_validation(h: Harness) -> None:
    to_validated(h, risk="MEDIUM")
    assert h.job()["status"] == "APPROVAL_REQUIRED"
    (h.repo / MAIN_PY).write_text("def list_items():\n    return [666]\n", encoding="utf-8")
    base = git(h.repo, "rev-parse", "main")
    assert h.run("approve", "J001") != 0
    assert h.job()["blocked"]["reason"] == "TREE_CHANGED"
    assert git(h.repo, "rev-parse", "main") == base


def test_validate_returns_to_building_when_tree_changed_after_verify(h: Harness) -> None:
    h.to_building()
    h.run("verify", "J001")
    (h.repo / MAIN_PY).write_text("def list_items():\n    return [3]\n", encoding="utf-8")
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator=PASS)
    assert h.run("validate", "J001") != 0
    assert h.job()["status"] == "BUILDING"
    assert h.calls("validator") == []


def test_new_job_after_merge_starts_from_merged_base(h: Harness) -> None:
    to_validated(h)
    h.run("merge", "J001")
    assert h.run("new", "다음 작업") == 0
    assert h.job("J002")["base_commit"] == h.job()["merge_commit"]


def test_block_needs_decision_from_queued(h: Harness) -> None:
    h.run("new", "테스트를 건너뛰게 해줘")
    assert h.run("block", "J001", "--reason", "NEEDS_DECISION", "--detail", "검증 약화 요청") == 0
    assert h.job()["blocked"]["reason"] == "NEEDS_DECISION"


# ---------- Phase 2: 세션 복구 ----------


def write_heartbeat(h: Harness, session: str, at: float) -> None:
    path = h.repo / "factory" / "heartbeat.json"
    path.write_text(json.dumps({"session": session, "at": at}), encoding="utf-8")


def test_recover_refuses_while_previous_session_is_alive(h: Harness) -> None:
    h.to_building()
    write_heartbeat(h, "s1", h.now - 30)
    assert h.run("recover", "J001") != 0
    assert h.job()["status"] == "BUILDING"
    assert h.run("recover", "J001", "--force") == 0


def test_recover_excludes_downtime_and_keeps_checkpoint(h: Harness) -> None:
    h.to_building()
    h.advance(60)
    write_heartbeat(h, "s1", h.now)
    h.advance(3600)  # 세션이 죽어 있던 시간
    assert h.run("recover", "J001") == 0
    job = h.job()
    assert job["status"] == "BUILDING"
    assert factory.active_seconds(job, now=h.now) == pytest.approx(60, abs=1)
    assert job["recoveries"][-1]["gap_s"] == pytest.approx(3600, abs=1)
    assert not (h.repo / "factory" / "heartbeat.json").exists()
    h.run("verify", "J001")
    assert h.job()["status"] == "VERIFYING"


def test_recover_returns_to_job_branch(h: Harness) -> None:
    h.to_building()
    git(h.repo, "switch", "-q", "main")
    assert h.run("recover", "J001") == 0
    assert git(h.repo, "branch", "--show-current") == "factory/J001"


def test_recover_detects_merge_done_before_crash(h: Harness) -> None:
    to_validated(h)
    # merge 도중 상태를 저장하기 전에 세션이 죽은 경우: Git에는 머지가 있다
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    git(h.repo, "add", "-A")
    git(h.repo, *ident, "commit", "-qm", "job")
    git(h.repo, "switch", "-q", "main")
    git(h.repo, *ident, "merge", "-q", "--no-ff", "-m", "merge", "factory/J001")
    merge_lock(h).write_text(json.dumps({"job": "J001", "at": h.now}), encoding="utf-8")
    assert h.run("recover", "J001") == 0
    job = h.job()
    assert job["status"] == "MERGED"
    assert job["merge_commit"] == git(h.repo, "rev-parse", "main")
    assert not merge_lock(h).exists()
    assert h.state()["active"] is None


def test_recover_releases_own_merge_lock_and_merge_can_retry(h: Harness) -> None:
    to_validated(h)
    merge_lock(h).write_text(json.dumps({"job": "J001", "at": h.now}), encoding="utf-8")
    assert h.run("recover", "J001") == 0
    assert h.job()["status"] == "APPROVED"
    assert not merge_lock(h).exists()
    assert h.run("merge", "J001") == 0
    assert h.job()["status"] == "MERGED"


def test_recover_rejects_blocked_jobs(h: Harness) -> None:
    h.to_building()
    h.run("block", "J001", "--reason", "MANUAL", "--detail", "중단")
    assert h.run("recover", "J001") != 0


def test_merged_job_removes_heartbeat(h: Harness) -> None:
    to_validated(h)
    write_heartbeat(h, "s1", h.now)
    h.run("merge", "J001")
    assert not (h.repo / "factory" / "heartbeat.json").exists()


# ---------- Phase 3: worktree 병렬 실행 ----------


def make_worktree(h: Harness, name: str) -> Path:
    path = h.repo.parent / name
    git(h.repo, "worktree", "add", "-q", "--detach", str(path), "main")
    return path


def wt_run(wt: Path, *argv: str) -> int:
    return factory.main(list(argv), root=wt)


def wt_job(wt: Path, job_id: str) -> dict[str, Any]:
    state = json.loads((wt / "factory" / "state.json").read_text(encoding="utf-8"))
    job: dict[str, Any] = state["jobs"][job_id]
    return job


def wt_to_approved(h: Harness, wt: Path, filename: str) -> str:
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator=PASS)
    assert wt_run(wt, "new", f"{filename} 추가", "--base", "main") == 0
    state = json.loads((wt / "factory" / "state.json").read_text(encoding="utf-8"))
    job_id = str(state["active"])
    contract = {
        "type": "feature",
        "size": "small",
        "risk": "LOW",
        "risk_reason": "새 모듈",
        "acceptance_criteria": ["모듈이 있다"],
        "acceptance_tests": [f"backend/tests/acceptance/test_{job_id}.py"],
        "changed_tests": [],
        "allowed_config_changes": [],
    }
    spec = wt / "factory" / "specs" / f"{job_id}.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(f"```json\n{json.dumps(contract)}\n```\n", encoding="utf-8")
    (wt / contract["acceptance_tests"][0]).write_text("def test_x():\n    assert False\n")
    assert wt_run(wt, "spec", job_id) == 0
    assert wt_run(wt, "red", job_id) == 0
    (wt / "backend/src/bookmarks" / filename).write_text("VALUE = 1\n", encoding="utf-8")
    assert wt_run(wt, "verify", job_id) == 0
    assert wt_run(wt, "validate", job_id) == 0
    assert wt_job(wt, job_id)["status"] == "APPROVED"
    return job_id


def test_new_in_detached_worktree_requires_base(h: Harness) -> None:
    wt = make_worktree(h, "wt1")
    assert wt_run(wt, "new", "요구사항") != 0
    assert wt_run(wt, "new", "요구사항", "--base", "main") == 0
    job = wt_job(wt, "J001")
    assert job["base_branch"] == "main"
    assert job["base_commit"] == git(h.repo, "rev-parse", "main")
    assert git(wt, "branch", "--show-current") == "factory/J001"


def test_new_base_must_match_head(h: Harness) -> None:
    wt = make_worktree(h, "wt1")
    (h.repo / "frontend/src/Other.tsx").write_text("export const x = 1;\n", encoding="utf-8")
    git(h.repo, "add", "-A")
    git(h.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main 진행")
    assert wt_run(wt, "new", "요구사항", "--base", "main") != 0


def test_merge_from_worktree_merges_into_base_checkout(h: Harness) -> None:
    wt = make_worktree(h, "wt1")
    job_id = wt_to_approved(h, wt, "alpha.py")
    assert wt_run(wt, "merge", job_id) == 0
    job = wt_job(wt, job_id)
    assert job["status"] == "MERGED"
    # base(main)는 기본 checkout에 있으므로 그쪽에서 머지되고, 작업 worktree는 그대로 둔다
    assert git(h.repo, "branch", "--show-current") == "main"
    assert (h.repo / "backend/src/bookmarks/alpha.py").exists()
    assert git(h.repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert git(h.repo, "rev-parse", "main") == job["merge_commit"]
    assert len(git(h.repo, "rev-list", "--parents", "-n", "1", "main").split()) == 3
    assert git(wt, "branch", "--show-current") == f"factory/{job_id}"


def test_merge_waits_when_base_checkout_is_dirty(h: Harness) -> None:
    wt = make_worktree(h, "wt1")
    job_id = wt_to_approved(h, wt, "alpha.py")
    (h.repo / "AGENTS.md").write_text("사람이 고치는 중\n", encoding="utf-8")
    base = git(h.repo, "rev-parse", "main")
    assert wt_run(wt, "merge", job_id) != 0
    assert wt_job(wt, job_id)["status"] == "APPROVED"
    assert git(h.repo, "rev-parse", "main") == base
    assert not merge_lock(h).exists()


def test_parallel_worktree_jobs_merge_one_after_another(h: Harness) -> None:
    wt1, wt2 = make_worktree(h, "wt1"), make_worktree(h, "wt2")
    first = wt_to_approved(h, wt1, "alpha.py")
    second = wt_to_approved(h, wt2, "beta.py")
    assert first != second
    assert wt_run(wt1, "merge", first) == 0
    verify_calls = len(h.calls("verify"))
    assert wt_run(wt2, "merge", second) == 0
    # 두 번째 작업은 main이 앞서 있으므로 rebase 후 다시 검증한다
    assert len(h.calls("verify")) == verify_calls + 1
    assert (h.repo / "backend/src/bookmarks/alpha.py").exists()
    assert (h.repo / "backend/src/bookmarks/beta.py").exists()
    merges = git(h.repo, "rev-list", "--merges", "main").splitlines()
    assert len(merges) == 2


def test_drop_in_worktree_keeps_branch_without_switching(h: Harness) -> None:
    wt = make_worktree(h, "wt1")
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator=PASS)
    wt_run(wt, "new", "요구사항", "--base", "main")
    (wt / "backend/src/bookmarks/main.py").write_text("def list_items():\n    return [5]\n")
    assert wt_run(wt, "drop", "J001") == 0
    assert wt_job(wt, "J001")["status"] == "DROPPED"
    assert "return [5]" in git(h.repo, "show", "factory/J001:backend/src/bookmarks/main.py")


def test_status_all_lists_jobs_from_every_worktree(
    h: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    wt1, wt2 = make_worktree(h, "wt1"), make_worktree(h, "wt2")
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator=PASS)
    wt_run(wt1, "new", "첫 번째", "--base", "main")
    wt_run(wt2, "new", "두 번째", "--base", "main")
    capsys.readouterr()
    assert h.run("status", "--all") == 0
    out = capsys.readouterr().out
    assert "J001" in out and "J002" in out
    assert "wt1" in out and "wt2" in out


def test_merge_waits_for_lock_held_by_another_job(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    to_validated(h)
    merge_lock(h).write_text(json.dumps({"job": "J999", "at": h.now}), encoding="utf-8")
    monkeypatch.setenv("FACTORY_MERGE_WAIT_S", "10")
    threading.Timer(0.5, lambda: merge_lock(h).unlink()).start()
    assert h.run("merge", "J001") == 0
    assert h.job()["status"] == "MERGED"


def test_block_with_timeout_reason(h: Harness) -> None:
    h.to_building()
    assert h.run("block", "J001", "--reason", "TIMEOUT", "--detail", "실행 래퍼 시간 초과") == 0
    assert h.job()["blocked"]["reason"] == "TIMEOUT"


def test_merge_never_commits_python_caches(h: Harness) -> None:
    to_validated(h)
    cache = h.repo / "backend/src/bookmarks/__pycache__/main.cpython-312.pyc"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"\x00cache")
    assert h.run("merge", "J001") == 0
    assert "__pycache__" not in git(h.repo, "ls-files")


# ---------- Phase 3b: 컨테이너 실행 ----------


def test_merge_is_left_to_host_inside_container(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    to_validated(h)
    monkeypatch.setenv("FACTORY_MERGE_ON_HOST", "1")
    base = git(h.repo, "rev-parse", "main")
    assert h.run("merge", "J001") == 0
    assert h.job()["status"] == "APPROVED"
    assert git(h.repo, "rev-parse", "main") == base
    assert not merge_lock(h).exists()


def test_factory_root_can_point_at_another_checkout(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 호스트는 작업 worktree의 factory.py(에이전트가 바꿀 수 있음)가 아니라 기본 checkout의
    # factory.py를 FACTORY_ROOT로 작업 worktree에 겨눠 실행한다
    monkeypatch.setenv("FACTORY_ROOT", str(h.repo))
    assert factory.main(["new", "요구사항"]) == 0
    assert h.job()["status"] == "QUEUED"


def test_network_allowlist_change_blocks_the_job(h: Harness) -> None:
    # 격리 환경(이미지·네트워크 허용 목록)은 팩토리 작업으로 바꿀 수 없다
    h.to_building()
    (h.repo / "docker/proxy").mkdir(parents=True)
    (h.repo / "docker/proxy/filter").write_text("^.*$\n", encoding="utf-8")
    h.run("verify", "J001")
    job = h.job()
    assert job["blocked"]["reason"] == "PROTECTED_FILE_CHANGED"
    assert "docker/proxy/filter" in job["blocked"]["detail"]
    assert factory._matches("docker/proxy/filter", factory.HIGH_PATHS)


# ---------- Phase 3c: 리뷰어 분리, Approver, 모델 승격, 에이전트 세션 ----------

PASS = '{"verdict": "PASS", "issues": []}'


def to_verifying(h: Harness) -> None:
    h.to_building()
    (h.repo / MAIN_PY).write_text("def list_items():\n    return [1]\n", encoding="utf-8")
    assert h.run("verify", "J001") == 0


def test_validate_runs_code_security_and_test_reviewers(h: Harness) -> None:
    to_verifying(h)
    h.configure(red=RED_OK, green=GREEN_OK, verify=VERIFY_OK, validator=PASS)
    assert h.run("validate", "J001") == 0
    calls = h.calls("validator")
    assert sorted(c["reviewer"] for c in calls) == ["code", "security", "test"]
    for call in calls:
        # 리뷰어마다 자기 관점의 지시를 받고, 자료(diff 등)는 같다
        assert f"리뷰어: {call['reviewer']}" in call["stdin"]
        assert "return [1]" in call["stdin"]
    job = h.job()
    assert job["status"] == "APPROVED"
    assert sorted(r["reviewer"] for r in job["reviews"]) == ["code", "security", "test"]
    assert all((h.repo / r["path"]).exists() for r in job["reviews"])


def test_any_reviewer_fail_sends_job_back(h: Harness) -> None:
    to_verifying(h)
    fail = '{"verdict": "FAIL", "issues": ["입력 검증 누락"]}'
    h.configure(
        red=RED_OK,
        green=GREEN_OK,
        verify=VERIFY_OK,
        validator={"code": PASS, "security": fail, "test": PASS},
    )
    assert h.run("validate", "J001") != 0
    job = h.job()
    assert job["status"] == "RETRY_REQUIRED"
    assert job["validation"][-1]["issues"] == ["[security] 입력 검증 누락"]


def test_one_unreadable_reviewer_keeps_state(h: Harness) -> None:
    to_verifying(h)
    h.configure(
        red=RED_OK,
        green=GREEN_OK,
        verify=VERIFY_OK,
        validator={"code": PASS, "security": PASS, "test": "모르겠음"},
    )
    assert h.run("validate", "J001") != 0
    assert h.job()["status"] == "VERIFYING"
    assert h.job()["attempts"] == 0


def test_approver_writes_brief_for_human_approval(h: Harness) -> None:
    to_validated(h, path="backend/src/bookmarks/auth.py", text="TOKENS = {}\n")
    job = h.job()
    assert job["status"] == "APPROVAL_REQUIRED"
    brief = (h.repo / job["approval_brief"]).read_text(encoding="utf-8")
    assert "HIGH" in brief
    assert "auth.py" in brief
    for reviewer in ("code", "security", "test"):
        assert f"{reviewer}: PASS" in brief
    assert "approve J001" in brief


def test_builder_model_is_promoted_after_two_failures(
    h: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    h.to_building()
    assert h.job()["builder_model"] == "sonnet"
    h.configure(red=RED_OK, green=GREEN_OK, verify=[verify_fail("a"), verify_fail("b")])
    h.run("verify", "J001")
    assert h.job()["builder_model"] == "sonnet"
    capsys.readouterr()
    h.run("verify", "J001")
    job = h.job()
    assert job["status"] == "RETRY_REQUIRED"
    assert job["builder_model"] == "opus"
    assert "Builder 모델: opus" in capsys.readouterr().out
    assert {"attempt": 2, "model": "opus"} in [
        {k: m[k] for k in ("attempt", "model")} for m in job["model_changes"]
    ]


def test_builder_model_names_follow_agent_tool(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT", "codex")
    h.to_building()
    assert h.job()["builder_model"] == factory.MODELS["codex"]["builder"]
    assert h.job()["agent"] == "codex"


@pytest.mark.parametrize(
    "argv",
    [
        ["approve", "J001"],
        ["approve", "J001", "--reject", "x"],
        ["resume", "J001"],
        ["drop", "J001"],
        ["recover", "J001", "--force"],
    ],
)
def test_human_only_commands_refuse_agent_sessions(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    # 도구의 hook과 별개로 factory.py 자체가 막는다 (도구 공통 강제 장치)
    to_validated(h, path="backend/src/bookmarks/auth.py", text="TOKENS = {}\n")
    before = h.job()["status"]
    monkeypatch.setenv("FACTORY_AGENT_SESSION", "1")
    capsys.readouterr()
    assert h.run(*argv) == 2
    assert "사람 전용" in capsys.readouterr().out
    assert h.job()["status"] == before


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("claude", ["claude", "-p", "--model", "claude-sonnet-5-5"]),
        ("codex", ["codex", "exec", "--model", "gpt-6.1-sol", "--sandbox", "read-only"]),
    ],
)
def test_reviewer_command_per_agent_tool(agent: str, expected: list[str]) -> None:
    cmd = factory.reviewer_command(agent, Path("out.txt"))
    assert cmd[: len(expected)] == expected
    if agent == "codex":
        assert cmd[cmd.index("--output-last-message") + 1] == "out.txt"
        assert cmd[-1] == "-"


def test_red_output_names_builder_model(h: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    h.to_building()
    assert "Builder 모델 sonnet" in capsys.readouterr().out


def test_status_shows_builder_model(h: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    h.to_building()
    capsys.readouterr()
    h.run("status")
    assert "sonnet" in capsys.readouterr().out


@pytest.mark.parametrize("path", [".codex/hooks.json", ".agents/skills/factory/SKILL.md"])
def test_codex_config_is_protected(h: Harness, path: str) -> None:
    # Codex 전용 구성(hook, 스킬)도 Claude Code의 .claude/처럼 잠금·HIGH 대상이다
    h.to_building()
    target = h.repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    h.run("verify", "J001")
    assert h.job()["blocked"]["reason"] == "PROTECTED_FILE_CHANGED"
    assert factory._matches(path, factory.HIGH_PATHS)
