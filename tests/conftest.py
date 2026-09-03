import pytest


@pytest.fixture(autouse=True)
def _isolate_telegram_env(monkeypatch):
    """Strip every TELEGRAM_* recipient/token var from the real OS
    environment before each test.

    src.telegram._collect_raw_chat_ids() reads TELEGRAM_CHAT_ID plus
    TELEGRAM_CHAT_ID_1..TELEGRAM_CHAT_ID_10 directly from os.environ.
    Individual tests only set/delete the specific vars they care about,
    so a developer machine (or CI runner) that happens to have e.g.
    TELEGRAM_CHAT_ID_1 exported for manual bot testing will silently
    leak an extra recipient into every test's _chat_ids list. Clearing
    all of them here first, autouse, makes every test start from a
    truly empty slate regardless of the host environment.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for i in range(1, 11):
        monkeypatch.delenv(f"TELEGRAM_CHAT_ID_{i}", raising=False)
