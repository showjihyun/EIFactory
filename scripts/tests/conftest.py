import os

import pytest


@pytest.fixture(autouse=True)
def clean_factory_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """바깥의 팩토리 설정(FACTORY_*)이 테스트에 새어 들지 않게 한다.

    컨테이너 세션 안에서 verify가 이 테스트를 돌릴 때 FACTORY_MERGE_ON_HOST 같은 실행 설정이
    환경에 있다. 각 테스트는 필요한 설정만 스스로 넣는다.
    """
    for name in list(os.environ):
        if name.startswith("FACTORY_"):
            monkeypatch.delenv(name)
