"""
Tests for the interactive /report custom date-range picker
(src/telegram.py's poll_updates() and friends).

Covers: Jalali days-in-month math, the inline-keyboard builders, the
stateless callback_data walk from "pick a start year" through to a
sent report, the chat-id whitelist (only configured recipients can
drive the picker), and the getUpdates offset bookkeeping (including
the first-run backlog fast-forward).
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import httpx
import jdatetime
import pytest
import respx

from src.db.repository import Repository
from src.telegram import (
    TelegramNotifier,
    _current_jalali_year,
    _jalali_days_in_month,
    _jalali_key,
    _report_day_keyboard,
    _report_month_keyboard,
    _report_year_keyboard,
)

_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_API = f"https://api.telegram.org/bot{_TOKEN}"


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


# ---------------------------------------------------------------------
# Jalali days-in-month
# ---------------------------------------------------------------------

def test_days_in_month_first_half_of_year_is_31():
    for month in range(1, 7):
        assert _jalali_days_in_month(1405, month) == 31


def test_days_in_month_second_half_before_esfand_is_30():
    for month in range(7, 12):
        assert _jalali_days_in_month(1405, month) == 30


def test_days_in_month_esfand_matches_jdatetime_probe():
    # Whatever the real Jalali leap-year rule says for 1405's Esfand,
    # jdatetime.date itself is the ground truth (it raises ValueError
    # for an invalid day) - the same probe _jalali_days_in_month() uses.
    year = 1405
    try:
        jdatetime.date(year, 12, 30)
        expected = 30
    except ValueError:
        expected = 29
    assert _jalali_days_in_month(year, 12) == expected
    assert _jalali_days_in_month(year, 12) in (29, 30)


# ---------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------

def test_year_keyboard_offers_current_and_previous_year():
    keyboard = _report_year_keyboard("rpt:sy")
    rows = keyboard["inline_keyboard"]
    year_row = rows[0]
    current = _current_jalali_year()

    assert len(year_row) == 2
    callback_years = {btn["callback_data"] for btn in year_row}
    assert callback_years == {f"rpt:sy:{current - 1}", f"rpt:sy:{current}"}
    # Last row is always the cancel button.
    assert rows[-1][0]["callback_data"] == "rpt:cancel"


def test_month_keyboard_has_all_12_months_plus_cancel():
    keyboard = _report_month_keyboard("rpt:sm:1405")
    rows = keyboard["inline_keyboard"]

    month_buttons = [btn for row in rows[:-1] for btn in row]
    assert len(month_buttons) == 12
    callback_data = {btn["callback_data"] for btn in month_buttons}
    assert callback_data == {f"rpt:sm:1405:{m}" for m in range(1, 13)}
    assert rows[-1][0]["callback_data"] == "rpt:cancel"


def test_day_keyboard_covers_every_day_of_a_31_day_month():
    keyboard = _report_day_keyboard("rpt:sd:1405:4", 1405, 4)  # Tir - 31 days
    rows = keyboard["inline_keyboard"]

    day_buttons = [btn for row in rows[:-1] for btn in row]
    assert len(day_buttons) == 31
    callback_data = {btn["callback_data"] for btn in day_buttons}
    assert callback_data == {f"rpt:sd:1405:4:{d}" for d in range(1, 32)}
    assert rows[-1][0]["callback_data"] == "rpt:cancel"


def test_day_keyboard_covers_every_day_of_a_30_day_month():
    keyboard = _report_day_keyboard("rpt:sd:1405:9", 1405, 9)  # Azar - 30 days
    day_buttons = [btn for row in keyboard["inline_keyboard"][:-1] for btn in row]
    assert len(day_buttons) == 30


# ---------------------------------------------------------------------
# Chat-id whitelist on inbound messages
# ---------------------------------------------------------------------

def test_report_command_ignored_from_unconfigured_chat():
    notifier = TelegramNotifier()
    notifier._chat_ids = [775753176]

    with patch.object(notifier, "_send_message_with_keyboard") as mock_send:
        notifier._handle_report_message({"chat": {"id": 999}, "text": "/report"})

    mock_send.assert_not_called()


def test_report_command_starts_the_picker_for_configured_chat():
    notifier = TelegramNotifier()
    notifier._chat_ids = [775753176]

    with patch.object(notifier, "_send_message_with_keyboard") as mock_send:
        notifier._handle_report_message({"chat": {"id": 775753176}, "text": "/report"})

    mock_send.assert_called_once()
    chat_id, text, reply_markup = mock_send.call_args[0]
    assert chat_id == 775753176
    assert "inline_keyboard" in reply_markup


# ---------------------------------------------------------------------
# Full callback walk: year -> month -> day -> year -> month -> day
# ---------------------------------------------------------------------

def test_full_range_pick_sends_report_with_correct_period(repo):
    notifier = TelegramNotifier()
    notifier._chat_ids = [775753176]
    source_names = ["digikala", "basalam"]

    with patch.object(notifier, "_edit_message") as mock_edit, \
         patch.object(notifier, "_answer_callback_query"), \
         patch.object(notifier, "_aggregate", return_value=(1000000, 50000, 1050000, 4)):

        def fire(data):
            notifier._handle_report_callback(
                {"id": "q1", "message": {"chat": {"id": 775753176}, "message_id": 42},
                 "data": data},
                repo, source_names,
            )

        fire("rpt:sy:1405")
        fire("rpt:sm:1405:4")
        fire("rpt:sd:1405:4:3")     # start = 1405-04-03
        fire("rpt:ey:1405-04-03:1405")
        fire("rpt:em:1405-04-03:1405:9")
        fire("rpt:ed:1405-04-03:1405:9:6")   # end = 1405-09-06

    # Last _edit_message call is the finished report.
    final_chat_id, final_message_id, final_text = mock_edit.call_args_list[-1][0][:3]
    assert final_chat_id == 775753176
    assert final_message_id == 42
    assert "از" in final_text and "تا" in final_text
    assert "1405" not in final_text  # dates are rendered in Persian digits
    assert "└─ 4 سفارش" in final_text


def test_end_before_start_shows_error_instead_of_report(repo):
    notifier = TelegramNotifier()
    notifier._chat_ids = [775753176]

    with patch.object(notifier, "_edit_message") as mock_edit, \
         patch.object(notifier, "_answer_callback_query"), \
         patch.object(notifier, "_aggregate") as mock_aggregate:
        notifier._send_custom_range_report(
            775753176, 42, _jalali_key(jdatetime.date(1405, 9, 6)),
            jdatetime.date(1405, 4, 3),  # end before start
            repo, ["digikala"],
        )

    mock_aggregate.assert_not_called()
    text = mock_edit.call_args[0][2]
    assert "نمی‌تواند قبل از تاریخ شروع" in text


def test_callback_from_unconfigured_chat_is_ignored(repo):
    notifier = TelegramNotifier()
    notifier._chat_ids = [775753176]

    with patch.object(notifier, "_edit_message") as mock_edit, \
         patch.object(notifier, "_answer_callback_query") as mock_answer:
        notifier._handle_report_callback(
            {"id": "q1", "message": {"chat": {"id": 999}, "message_id": 42},
             "data": "rpt:sy:1405"},
            repo, ["digikala"],
        )

    mock_edit.assert_not_called()
    mock_answer.assert_called_once()  # spinner still cleared


# ---------------------------------------------------------------------
# poll_updates(): offset bookkeeping
# ---------------------------------------------------------------------

@respx.mock
def test_poll_updates_first_run_seeds_offset_without_processing(repo):
    """No marker yet - must fast-forward past any backlog (offset=-1)
    rather than replay/act on old updates, and must NOT touch
    _handle_report_update at all on this seeding call."""
    notifier = TelegramNotifier()
    notifier._client = httpx.Client(base_url=_API)
    notifier._chat_ids = [775753176]
    notifier._configured = True
    respx.post(f"{_API}/getUpdates").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": [{"update_id": 999, "message": {}}]}
        )
    )

    with patch.object(notifier, "_handle_report_update") as mock_handle:
        notifier.poll_updates(repo, ["digikala"])

    mock_handle.assert_not_called()
    assert repo.get_report_marker("telegram_update_offset") == "999"


@respx.mock
def test_poll_updates_advances_offset_past_processed_updates(repo):
    notifier = TelegramNotifier()
    notifier._client = httpx.Client(base_url=_API)
    notifier._chat_ids = [775753176]
    notifier._configured = True
    repo.set_report_marker("telegram_update_offset", "500")

    respx.post(f"{_API}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 501, "message": {"chat": {"id": 775753176}, "text": "hi"}},
                    {"update_id": 502, "message": {"chat": {"id": 775753176}, "text": "hi"}},
                ],
            },
        )
    )

    with patch.object(notifier, "_handle_report_update") as mock_handle:
        notifier.poll_updates(repo, ["digikala"])

    assert mock_handle.call_count == 2
    assert repo.get_report_marker("telegram_update_offset") == "502"


def test_poll_updates_noops_when_not_configured(repo, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = TelegramNotifier()

    # Must not raise even with no client at all.
    notifier.poll_updates(repo, ["digikala"])

    assert repo.get_report_marker("telegram_update_offset") is None
