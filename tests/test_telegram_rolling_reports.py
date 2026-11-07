"""
Tests for the three quick-report buttons on the persistent bottom menu
(24h / 7d / 30d - src/telegram.py's _send_rolling_report()) and for the
"daily report is sent automatically every night" note appended to every
on-demand report.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.didar.deal_channel_report import DealReportError, aggregate_deals
from src.telegram import (
    _AUTO_DAILY_REPORT_NOTE,
    _REPORT_LAST_24H_BUTTON_TEXT,
    _REPORT_LAST_30D_BUTTON_TEXT,
    _REPORT_LAST_7D_BUTTON_TEXT,
    _REPORT_MENU_BUTTON_TEXT,
    TelegramNotifier,
    _report_reply_keyboard,
)
from tests.test_deal_channel_report import TITLE_BY_ID, _parse, _row

CHAT = 775753176


def _notifier() -> TelegramNotifier:
    n = TelegramNotifier()
    n._chat_ids = [CHAT]
    return n


class _FakeDidar:
    """Records the window it was asked for; 2 digikala + 1 basalam deal."""

    def __init__(self):
        self.calls = []

    def get_channel_report(self, since, until):
        self.calls.append((since, until))
        mid = since + (until - since) / 2
        rows = [
            _row(1, mid, 100_000, ["L-digi"]),
            _row(2, mid, 100_000, ["L-digi"]),
            _row(3, mid, 50_000, ["L-basalam"]),
        ]
        return aggregate_deals(rows, TITLE_BY_ID, since, until, _parse)


# ---------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------

def test_reply_keyboard_is_a_persistent_2x2_grid_with_all_four_buttons():
    kb = _report_reply_keyboard()
    rows = [[b["text"] for b in row] for row in kb["keyboard"]]
    assert rows == [
        [_REPORT_LAST_24H_BUTTON_TEXT, _REPORT_LAST_7D_BUTTON_TEXT],
        [_REPORT_LAST_30D_BUTTON_TEXT, _REPORT_MENU_BUTTON_TEXT],
    ]
    assert kb["is_persistent"] is True
    assert kb["resize_keyboard"] is True


# ---------------------------------------------------------------------
# Button presses
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "button, window, title",
    [
        (_REPORT_LAST_24H_BUTTON_TEXT, timedelta(hours=24), "📊 گزارش ۲۴ ساعت گذشته"),
        (_REPORT_LAST_7D_BUTTON_TEXT, timedelta(days=7), "📊 گزارش ۷ روز گذشته"),
        (_REPORT_LAST_30D_BUTTON_TEXT, timedelta(days=30), "📊 گزارش ۳۰ روز گذشته"),
    ],
)
def test_button_sends_rolling_report_with_note(button, window, title):
    n = _notifier()
    didar = _FakeDidar()
    before = datetime.now(timezone.utc)

    with patch.object(n, "_send_message_with_keyboard", return_value=55) as wait, \
         patch.object(n, "_edit_message") as edit, \
         patch.object(n, "_get_didar_client", return_value=didar):
        n._handle_report_message({"chat": {"id": CHAT}, "text": button})

    # Window is exactly `window` long and ends "now".
    (since, until), = didar.calls
    assert until - since == window
    assert before <= until <= datetime.now(timezone.utc)

    # A "please wait" message first, then edited into the report.
    assert wait.call_args[0][0] == CHAT
    chat_id, message_id, text = edit.call_args[0][:3]
    assert (chat_id, message_id) == (CHAT, 55)
    assert text.startswith(title)
    assert "└─ 3 معامله\n└─ 250,000 ریال" in text      # Total == sum of Channels
    assert "🔴 دیجی‌کالا\n└─ 2 سفارش\n└─ 200,000 ریال" in text
    assert text.rstrip().endswith(_AUTO_DAILY_REPORT_NOTE)


def test_didar_failure_replaces_wait_message_with_error_and_no_report():
    n = _notifier()

    class _Broken:
        def get_channel_report(self, since, until):
            raise DealReportError("page failed")

    with patch.object(n, "_send_message_with_keyboard", return_value=9), \
         patch.object(n, "_edit_message") as edit, \
         patch.object(n, "_get_didar_client", return_value=_Broken()):
        n._handle_report_message({"chat": {"id": CHAT}, "text": _REPORT_LAST_7D_BUTTON_TEXT})

    text = edit.call_args[0][2]
    assert "ناموفق" in text and "📊" not in text


def test_wait_message_failure_falls_back_to_a_new_message():
    n = _notifier()
    with patch.object(n, "_send_message_with_keyboard", return_value=None), \
         patch.object(n, "_edit_message") as edit, \
         patch.object(n, "_post_message") as post, \
         patch.object(n, "_get_didar_client", return_value=_FakeDidar()):
        n._handle_report_message({"chat": {"id": CHAT}, "text": _REPORT_LAST_24H_BUTTON_TEXT})

    edit.assert_not_called()
    assert post.call_args[0][0] == CHAT
    assert _AUTO_DAILY_REPORT_NOTE in post.call_args[0][1]


def test_button_from_unconfigured_chat_is_ignored():
    n = _notifier()
    with patch.object(n, "_send_message_with_keyboard") as send, \
         patch.object(n, "_get_didar_client") as get_client:
        n._handle_report_message({"chat": {"id": 999}, "text": _REPORT_LAST_24H_BUTTON_TEXT})
    send.assert_not_called()
    get_client.assert_not_called()


def test_other_admins_are_told_who_pulled_the_report():
    n = _notifier()
    n._chat_ids = [CHAT, 42]
    with patch.object(n, "_send_message_with_keyboard", return_value=1), \
         patch.object(n, "_edit_message"), \
         patch.object(n, "_post_message") as post, \
         patch.object(n, "_get_didar_client", return_value=_FakeDidar()):
        n._handle_report_message({"chat": {"id": CHAT}, "text": _REPORT_LAST_30D_BUTTON_TEXT})

    assert [c[0][0] for c in post.call_args_list] == [42]
    assert "گزارش ۳۰ روز گذشته" in post.call_args_list[0][0][1]


# ---------------------------------------------------------------------
# "🚀 شروع" button under the update announcement == /start
# ---------------------------------------------------------------------

def _start_callback(chat_id):
    return {
        "id": "q1",
        "message": {"chat": {"id": chat_id}, "message_id": 77},
        "data": "rpt:start",
    }


def test_start_button_applies_start_menu_and_removes_its_own_button(tmp_path):
    from src.db.repository import Repository

    n = _notifier()
    with patch.object(n, "_send_message_with_keyboard", return_value=1) as send, \
         patch.object(n, "_edit_message") as edit, \
         patch.object(n, "_answer_callback_query"):
        n._handle_report_callback(
            _start_callback(CHAT), Repository(db_path=str(tmp_path / "t.db")), []
        )

    # Announcement edited WITHOUT a keyboard (so it can't be pressed twice)...
    assert edit.call_args[0][:2] == (CHAT, 77)
    assert len(edit.call_args[0]) == 3
    # ...and the new persistent menu was sent, exactly like /start.
    menu_markup = send.call_args_list[0][0][2]
    assert menu_markup == _report_reply_keyboard()


def test_start_button_from_unconfigured_chat_is_ignored(tmp_path):
    from src.db.repository import Repository

    n = _notifier()
    with patch.object(n, "_send_message_with_keyboard") as send, \
         patch.object(n, "_edit_message") as edit, \
         patch.object(n, "_answer_callback_query"):
        n._handle_report_callback(
            _start_callback(999), Repository(db_path=str(tmp_path / "t.db")), []
        )
    send.assert_not_called()
    edit.assert_not_called()


def test_slash_start_still_sends_menu_then_welcome():
    n = _notifier()
    with patch.object(n, "_send_message_with_keyboard", return_value=1) as send:
        n._handle_report_message({"chat": {"id": CHAT}, "text": "/start"})
    assert send.call_args_list[0][0][2] == _report_reply_keyboard()
    assert "inline_keyboard" in send.call_args_list[1][0][2]
