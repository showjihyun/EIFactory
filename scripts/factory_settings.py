"""팩토리 설정 보기·바꾸기 (사람 전용).

사용법:
    python scripts/factory_settings.py show
    python scripts/factory_settings.py set <단계> model <auto|haiku|sonnet|opus>
    python scripts/factory_settings.py set <단계> auth <auto|subscription|api>

단계: orchestrator, builder, reviewer_code, reviewer_security, reviewer_test.
기본 checkout의 `factory/config.json`만 바꾼다. 이 파일은 잠금·HIGH 대상이라 팩토리 작업은 바꿀 수
없고, 에이전트 세션(FACTORY_AGENT_SESSION=1)에서는 이 명령도 거부한다. 이미 만든 작업의 Builder
모델은 바뀌지 않는다 (`new` 때 정해짐). 리뷰어·오케스트레이터는 다음 실행부터 적용된다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_config import (  # noqa: E402
    AUTH_CHOICES,
    BUDGET_DEFAULTS,
    CLAUDE_MODELS,
    CONFIG_PATH,
    MODEL_CHOICES,
    STAGES,
    ConfigError,
    _budgets,
    _stages,
    load_config,
)
from proc import use_utf8_output  # noqa: E402

# 자동(auto)일 때 고르는 모델 (factory.stage_model, dispatch.orchestrator_plan과 같은 규칙)
AUTO_RULES = {
    "orchestrator": "opus",
    "builder": "sonnet (실패 2회 뒤 opus로 승격)",
    "reviewer_code": "sonnet",
    "reviewer_security": "sonnet (HIGH 위험이면 opus)",
    "reviewer_test": "sonnet",
}
AUTH_NOTE = {
    "auto": "ANTHROPIC_API_KEY가 있으면 API, 없으면 구독",
    "subscription": "구독 (로그인 세션)",
    "api": "API 키 (환경변수)",
}


def show(root: Path) -> str:
    cfg = load_config(root)
    lines = ["| 단계 | 모델 | 실제 모델 | 인증 |", "|---|---|---|---|"]
    for stage in STAGES:
        item = cfg.stages[stage]
        if item["model"] == "auto":
            first = AUTO_RULES[stage].split(" ")[0]
            resolved = f"{CLAUDE_MODELS[first]} {AUTO_RULES[stage][len(first) :]}".strip()
        else:
            resolved = CLAUDE_MODELS[item["model"]]
        auth = item["auth"] if stage != "builder" else "auto (오케스트레이터를 따름)"
        lines.append(
            f"| {stage} | {item['model']} | {resolved} | {auth}: {AUTH_NOTE[item['auth']]} |"
        )
    lines += ["", "| 비용 상한 | 값 (USD, API 환산) |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in cfg.budgets.items()]
    lines += [
        "",
        "모델은 Claude 최신 세 가지만: " + ", ".join(f"{k}={v}" for k, v in CLAUDE_MODELS.items()),
        "한 번만 바꾸려면 환경변수 FACTORY_MODEL_<단계>=haiku|sonnet|opus "
        "(예: FACTORY_MODEL_BUILDER)",
        "컨테이너 격리(--isolation docker)는 API 키만 쓴다 (subscription 불가)",
    ]
    return "\n".join(lines)


def set_value(root: Path, stage: str, key: str, value: str) -> str:
    path = root / CONFIG_PATH
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if stage == "budgets":
        budgets = dict(data.get("budgets") or {})
        try:
            budgets[key] = float(value)
        except ValueError as error:
            raise ConfigError(f"budgets.{key}={value}는 숫자여야 합니다") from error
        if key not in BUDGET_DEFAULTS:
            raise ConfigError(f"모르는 상한 {key} (가능: {', '.join(BUDGET_DEFAULTS)})")
        _budgets(budgets)
        data["budgets"] = budgets
        _write(path, data)
        return f"budgets.{key} = {value} ({CONFIG_PATH}). 이후 new 되는 작업부터 적용"
    if key not in ("model", "auth"):
        raise ConfigError(f"바꿀 수 있는 항목은 model, auth입니다 ({key})")
    stages = dict(data.get("stages") or {})
    item = dict(stages.get(stage) or {})
    item[key] = value
    stages[stage] = item
    _stages(stages)  # 검사 (모르는 단계·값이면 ConfigError, 파일은 그대로)
    data["stages"] = stages
    _write(path, data)
    return f"{stage}.{key} = {value} ({CONFIG_PATH}). 커밋은 사람이 합니다"


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                    newline="\n")  # fmt: skip


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    root = root or Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(prog="factory_settings", description="팩토리 설정 (사람 전용)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    setp = sub.add_parser("set")
    setp.add_argument("stage")
    setp.add_argument("key")
    setp.add_argument("value")
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            print(show(root))
            return 0
        if os.environ.get("FACTORY_AGENT_SESSION") == "1":
            print("오류: factory_settings.py set은 사람 전용입니다 (에이전트 세션에서 거부)")
            return 2
        print(set_value(root, args.stage, args.key, args.value))
        return 0
    except ConfigError as error:
        choices = (
            f"단계 {', '.join(STAGES)} / model {', '.join(MODEL_CHOICES)} "
            f"/ auth {', '.join(AUTH_CHOICES)}"
        )
        print(f"오류: {error}\n가능한 값: {choices}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
