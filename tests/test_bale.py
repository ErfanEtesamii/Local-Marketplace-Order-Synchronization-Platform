import json

import respx
from httpx import Response

from src.bale import BaleNotifier


@respx.mock
def test_bale_configures_and_posts_to_bale_api(monkeypatch):
    monkeypatch.setenv("BALE_BOT_TOKEN", "bale-token")
    monkeypatch.setenv("BALE_CHAT_ID_1", "12345")
    respx.post("https://tapi.bale.ai/botbale-token/getMe").mock(
        return_value=Response(200, json={"ok": True, "result": {"id": 1}})
    )
    send = respx.post("https://tapi.bale.ai/botbale-token/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    notifier = BaleNotifier()
    assert notifier.is_configured()
    notifier._send("سلام")
    assert send.called
    assert json.loads(send.calls.last.request.content) == {"chat_id": 12345, "text": "سلام"}


def test_bale_reads_only_bale_environment(monkeypatch):
    monkeypatch.delenv("BALE_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_1", "12345")
    assert not BaleNotifier().is_configured()


def test_bale_retry_rows_are_namespaced():
    notifier = BaleNotifier()
    assert notifier._failure_ref("order:digikala:1") == "bale:order:digikala:1"
    assert notifier._owns_failure("bale:order:digikala:1")
    assert not notifier._owns_failure("order:digikala:1")


def test_bale_report_markers_are_independent_of_telegram(tmp_path, monkeypatch):
    """Regression: Telegram running first must not make Bale skip reports."""
    from src.db.repository import Repository
    from src.telegram import TelegramNotifier

    repo = Repository(str(tmp_path / "t.db"))
    for period, old in (("day", "1300-01-01"), ("week", "1300-01-01"),
                        ("month", "1300-01"), ("year", "1300")):
        repo.set_report_marker(period, old)
        repo.set_report_marker(f"bale_{period}", old)

    kinds = ("daily", "weekly", "monthly", "yearly")
    sent = []
    for cls, name in ((TelegramNotifier, "telegram"), (BaleNotifier, "bale")):
        for kind in kinds:
            monkeypatch.setattr(
                cls, f"_send_{kind}_report",
                (lambda n, k: lambda self, *a, **kw: sent.append((n, k)))(name, kind),
            )

    TelegramNotifier().check_and_send_reports(repo, [])
    BaleNotifier().check_and_send_reports(repo, [])

    assert [k for n, k in sent if n == "telegram"] == list(kinds)
    assert [k for n, k in sent if n == "bale"] == list(kinds)
    assert repo.get_report_marker("bale_day") == repo.get_report_marker("day")
