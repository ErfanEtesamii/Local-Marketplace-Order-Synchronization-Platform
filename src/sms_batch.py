"""Manual "send the customer SMS now" flow, driven from the Telegram bot.

Why: the phone's address changes (DHCP) and the phone is not always on, so
instead of firing a message 5 hours after every order, orders are only SAVED
(customer_sms_queue, status 'pending') and an operator sends the whole
saved queue with ONE button:

  1. An operator (CUSTOMER_SMS_OPERATOR_CHAT_IDS - only they see the
     button) presses "📨 ارسال پیامک مشتریان".
  2. The bot shows a PREVIEW (count, time estimate, first orders, warnings)
     and the operator confirms. Then, by CUSTOMER_SMS_ROUTE_MODE:
       cloud (default when Cloud creds exist) - no IP question at all;
       local - the fixed SMS_GATEWAY_URL (DHCP-reserved phone), Cloud fallback;
       ask   - the public-relations account (CUSTOMER_SMS_IP_CHAT_ID) is
               asked for the phone's IP (typed - private LAN addresses only -
               or "same IP as before" / "Cloud only").
     Orders older than CUSTOMER_SMS_MAX_AGE_HOURS are skipped, and a batch
     never runs inside the quiet hours (21-09 Tehran) unless the operator
     confirmed that explicitly.
  3. The queue is frozen as a batch (orders arriving later wait for the NEXT
     press). Messages go out in registration order, ONE per
     CUSTOMER_SMS_BATCH_INTERVAL_SECONDS (default 60): local route first,
     the Cloud route if the phone isn't reachable on that IP. Every message
     is re-checked against its marketplace first (cancelled orders are
     skipped) and its final state is confirmed through the gateway's status
     API before the next one is sent.
  4. If a message cannot be sent (both routes fail, the phone reports a
     failure, or it doesn't send within CUSTOMER_SMS_BATCH_STALL_MINUTES)
     the batch STOPS; the rest stay queued for the next press.
  5. A success/failure report goes to every operator and the IP account.

State lives in the report_progress key-value table (survives restarts):
  sms_batch_state  - JSON of the current batch
  sms_gateway_ip   - last IP given ("ip:port")
  sms_menu_v2:<id> - the new bottom menu was already pushed to that chat
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import uuid
from math import ceil
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.customer_sms import MESSAGE_TEXT, CustomerSmsError, CustomerSmsNotifier
from src.db.repository import Repository
from src.logger import get_logger

log = get_logger(__name__)

BUTTON_TEXT = "📨 ارسال پیامک مشتریان"
BUTTON_STATUS = "📊 وضعیت صف پیامک"
BUTTON_RETRY = "🔁 ارسال مجدد تأییدنشده‌ها"
CALLBACK_PREFIX = "sms:"

_STATE_KEY = "sms_batch_state"
_IP_KEY = "sms_gateway_ip"
_MENU_KEY = "sms_menu_v2:"
_IP_WAIT = timedelta(minutes=60)  # how long to wait for the IP answer

_IP_RE = re.compile(
    r"^(?:https?://)?(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{2,5}))?/?$"
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


def _ids(name: str) -> list[int]:
    out: list[int] = []
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


def parse_ip_checked(text: str) -> tuple[str | None, str | None]:
    """(ip:port, None) when valid, else (None, "invalid" | "not_private").

    Only PRIVATE LAN addresses are accepted (10.x, 172.16-31.x, 192.168.x):
    the gateway login is sent to whatever address is typed here, so a typo or
    a forged message must never be able to point it at a public host."""
    text = (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    m = _IP_RE.match(text)
    if not m:
        return None, "invalid"
    ip, port = m.group(1), m.group(2)
    if any(int(o) > 255 for o in ip.split(".")):
        return None, "invalid"
    if port is None:
        port = str(_default_port())
    if not 0 < int(port) < 65536:
        return None, "invalid"
    addr = ipaddress.ip_address(ip)
    if (
        not addr.is_private
        or addr.is_loopback
        or addr.is_unspecified
        or addr.is_link_local
        or addr.is_multicast
    ):
        return None, "not_private"
    return f"{ip}:{port}", None


def parse_ip(text: str) -> str | None:
    """"192.168.1.43", "192.168.1.43:8080" or "http://192.168.1.43:8080" ->
    "192.168.1.43:8080" (default port from SMS_GATEWAY_URL, else 8080).
    None if it isn't a valid PRIVATE IPv4 address."""
    return parse_ip_checked(text)[0]


_IPISH_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){2,}")


def _default_port() -> int:
    m = re.search(r":(\d{2,5})(?:/|$)", os.getenv("SMS_GATEWAY_URL", ""))
    return int(m.group(1)) if m else 8080


class SmsBatchController:
    def __init__(
        self,
        notifier: CustomerSmsNotifier,
        telegram,  # src.telegram.TelegramNotifier (the one that polls updates)
        status_check: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._sms = notifier
        self._tg = telegram
        self._status_check = status_check
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Who is who
    # ------------------------------------------------------------------
    @staticmethod
    def operators() -> list[int]:
        return _ids("CUSTOMER_SMS_OPERATOR_CHAT_IDS")

    @staticmethod
    def ip_chat() -> int | None:
        ids = _ids("CUSTOMER_SMS_IP_CHAT_ID")
        return ids[0] if ids else None

    def is_operator(self, chat_id) -> bool:
        return chat_id in self.operators()

    def _everyone(self) -> list[int]:
        out = list(self.operators())
        ip = self.ip_chat()
        if ip is not None and ip not in out:
            out.append(ip)
        return out

    def keyboard_rows(self, chat_id) -> list[list[dict]]:
        """Extra bottom-menu rows: the buttons are for operators only."""
        if self.is_operator(chat_id) and self._sms.mode() == "manual":
            return [
                [{"text": BUTTON_TEXT}],
                [{"text": BUTTON_STATUS}, {"text": BUTTON_RETRY}],
            ]
        return []

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def route_mode(self) -> str:
        """"cloud" | "local" | "ask" (see module docstring). Unset: Cloud
        when its credentials exist, else ask for the IP. A choice whose
        credentials are missing falls back to "ask"."""
        raw = os.getenv("CUSTOMER_SMS_ROUTE_MODE", "").strip().lower()
        cloud_ok = self._sms._cloud_route() is not None
        local_ok = self._sms._local_route() is not None
        if raw == "local":
            return "local" if local_ok else "ask"
        if raw == "ask":
            return "ask"
        if raw in ("", "cloud"):
            return "cloud" if cloud_ok else "ask"
        return "ask"

    @staticmethod
    def _confirm_enabled() -> bool:
        return os.getenv("CUSTOMER_SMS_CONFIRM", "true").strip().lower() != "false"

    @staticmethod
    def _max_age_hours() -> int:
        return max(0, _env_int("CUSTOMER_SMS_MAX_AGE_HOURS", 72))

    def _stale_cutoff(self, now: datetime) -> datetime | None:
        h = self._max_age_hours()
        return now - timedelta(hours=h) if h else None

    def _stale_count(self, repo: Repository, now: datetime) -> int:
        cutoff = self._stale_cutoff(now)
        return repo.count_stale_pending_customer_sms(cutoff) if cutoff else 0

    def _skip_stale(self, repo: Repository, now: datetime) -> int:
        cutoff = self._stale_cutoff(now)
        if not cutoff:
            return 0
        return repo.skip_stale_pending_customer_sms(
            cutoff, f"سفارش قدیمی‌تر از {self._max_age_hours()} ساعت؛ ارسال نشد"
        )

    def _in_quiet_hours(self, now: datetime) -> bool:
        return self._sms._apply_quiet_hours(now) != now

    @staticmethod
    def _quiet_label() -> str:
        return f"{_env_int('CUSTOMER_SMS_QUIET_START', 21):02d}:00 تا {_env_int('CUSTOMER_SMS_QUIET_END', 9):02d}:00"

    # ------------------------------------------------------------------
    # Telegram helpers (best effort, never raise)
    # ------------------------------------------------------------------
    def _say(self, chat_id, text: str, keyboard: dict | None = None) -> None:
        try:
            if keyboard is None:
                self._tg._send_plain_message(chat_id, text)
            else:
                self._tg._send_message_with_keyboard(chat_id, text, keyboard)
        except Exception:
            log.exception("sms_batch: could not message chat %s", chat_id)

    def _say_all(self, text: str) -> None:
        for chat_id in self._everyone():
            self._say(chat_id, text)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @staticmethod
    def _load(repo: Repository) -> dict | None:
        raw = repo.get_report_marker(_STATE_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    @staticmethod
    def _save(repo: Repository, state: dict | None) -> None:
        repo.set_report_marker(_STATE_KEY, json.dumps(state) if state else "")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def ensure_menu(self, repo: Repository) -> None:
        """Push the bottom menu with the buttons once to each operator
        (Telegram keeps showing the old keyboard until it is replaced)."""
        if self._sms.mode() != "manual":
            return
        for chat_id in self.operators():
            key = f"{_MENU_KEY}{chat_id}"
            if repo.get_report_marker(key):
                continue
            kb = self._tg._reply_keyboard_for(chat_id)
            self._say(
                chat_id,
                "✅ منوی بات به‌روز شد: «ارسال پیامک مشتریان»، «وضعیت صف پیامک» و "
                "«ارسال مجدد تأییدنشده‌ها».",
                kb,
            )
            repo.set_report_marker(key, "1")

    # ------------------------------------------------------------------
    # Incoming Telegram events
    # ------------------------------------------------------------------
    def handle_message(self, chat_id, text: str, repo: Repository) -> bool:
        """True if the message was consumed (a button press or an IP answer)."""
        if not self.is_operator(chat_id) and chat_id != self.ip_chat():
            return False
        with self._lock:
            if text in (BUTTON_TEXT, BUTTON_STATUS, BUTTON_RETRY):
                if not self.is_operator(chat_id):
                    return False
                if text == BUTTON_TEXT:
                    self._request(chat_id, repo)
                elif text == BUTTON_STATUS:
                    self._status(chat_id, repo)
                else:
                    self._retry_request(chat_id, repo)
                return True
            state = self._load(repo)
            if state and state.get("state") == "awaiting_ip":
                ip, problem = parse_ip_checked(text)
                if ip:
                    self._start(repo, ip, bool(state.get("override_quiet")))
                    return True
                if problem == "not_private" or _IPISH_RE.search(
                    (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
                ):
                    self._say(
                        chat_id,
                        "⚠️ این آی‌پی قابل قبول نیست. فقط آی‌پی شبکهٔ داخلی گوشی پذیرفته می‌شود "
                        "(مثل 192.168.1.43، 10.x.x.x یا 172.16–31.x.x).",
                    )
                    return True
            return False

    def handle_callback(self, chat_id, data: str, repo: Repository) -> bool:
        if not data.startswith(CALLBACK_PREFIX):
            return False
        if not self.is_operator(chat_id) and chat_id != self.ip_chat():
            return True  # swallow: not allowed
        action = data[len(CALLBACK_PREFIX):]
        with self._lock:
            if action in ("retry", "retrycancel"):
                if self.is_operator(chat_id):
                    self._retry_answer(chat_id, repo, action == "retry")
                return True
            state = self._load(repo)
            if not state or state.get("state") not in ("awaiting_confirm", "awaiting_ip"):
                self._say(chat_id, "این درخواست دیگر معتبر نیست. دوباره دکمه را بزنید.")
                return True
            if action == "cancel":
                self._save(repo, None)
                self._say_all("❌ ارسال پیامک‌ها لغو شد. پیام‌ها در صف می‌مانند.")
            elif action == "go":
                if state["state"] == "awaiting_confirm" and self.is_operator(chat_id):
                    self._after_confirm(chat_id, repo, state)
            elif action in ("same", "cloud") and state["state"] == "awaiting_ip":
                override = bool(state.get("override_quiet"))
                if action == "cloud":
                    self._start(repo, "", override)
                else:
                    prev = repo.get_report_marker(_IP_KEY)
                    if not prev:
                        m = re.search(r"//([^/:]+(?::\d+)?)", os.getenv("SMS_GATEWAY_URL", ""))
                        prev = parse_ip(m.group(1)) if m else None
                    if not prev:
                        self._say(chat_id, "آی‌پی قبلی ثبت نشده. آی‌پی را بنویسید یا «فقط Cloud» را بزنید.")
                        return True
                    self._start(repo, prev, override)
        return True

    # ------------------------------------------------------------------
    # Flow
    # ------------------------------------------------------------------
    def _request(self, chat_id, repo: Repository) -> None:
        state = self._load(repo)
        if state and state.get("state") == "running":
            done = len(state.get("handed", []))
            self._say(chat_id, f"⏳ ارسال در حال انجام است ({done} از {state['total']}).")
            return
        if state and state.get("state") == "awaiting_ip":
            self._send_ip_prompt(repo, state)
            self._say(chat_id, "منتظر آی‌پی از روابط عمومی هستیم؛ درخواست دوباره ارسال شد.")
            return
        if state and state.get("state") == "awaiting_confirm":
            self._send_preview(chat_id, repo, state)
            return
        if not self._sms.is_enabled():
            self._say(chat_id, "⚠️ ارسال پیامک مشتری غیرفعال است (CUSTOMER_SMS_ENABLED یا تنظیمات گیت‌وی).")
            return
        from src.customer_sms import settings as _settings
        if _settings.dry_run or os.getenv("CUSTOMER_SMS_DRY_RUN", "false").strip().lower() == "true":
            self._say(chat_id, "⚠️ حالت آزمایشی (DRY_RUN) روشن است؛ چیزی ارسال نمی‌شود.")
            return
        pending = repo.count_pending_customer_sms()
        if pending == 0:
            self._say(chat_id, "📭 صف خالی است؛ پیامکی برای ارسال نیست.")
            return
        now = self._now()
        stale = self._stale_count(repo, now)
        if pending - stale <= 0:
            skipped = self._skip_stale(repo, now)
            self._say(
                chat_id,
                f"📭 همهٔ {skipped} پیام صف قدیمی‌تر از {self._max_age_hours()} ساعت بودند و رد شدند؛ "
                "چیزی برای ارسال نیست.",
            )
            return
        mode = self.route_mode()
        if mode == "ask" and self.ip_chat() is None:
            self._say(chat_id, "⚠️ CUSTOMER_SMS_IP_CHAT_ID در تنظیمات مشخص نشده.")
            return
        quiet = self._in_quiet_hours(now)
        new_state: dict = {
            "state": "awaiting_confirm",
            "requested_by": chat_id,
            "requested_at": now.isoformat(),
            "pending": pending - stale,
            "stale": stale,
            "override_quiet": False,
        }
        if self._confirm_enabled() or quiet:
            self._save(repo, new_state)
            self._send_preview(chat_id, repo, new_state)
        else:
            self._after_confirm(chat_id, repo, new_state)

    def _send_preview(self, chat_id, repo: Repository, state: dict) -> None:
        n = state["pending"]
        interval = _env_int("CUSTOMER_SMS_BATCH_INTERVAL_SECONDS", 60)
        minutes = max(1, ceil(n * interval / 60))
        lines = [
            f"📨 آمادهٔ ارسال: {n} پیامک",
            f"⏱ زمان تقریبی: حدود {minutes} دقیقه (هر {interval} ثانیه یکی)",
        ]
        sample = repo.peek_pending_customer_sms(3)
        if sample:
            lines.append("\nنمونه (اولین‌ها):")
            for platform, order_id, phone in sample:
                phone = str(phone)
                masked = f"{phone[:4]}***{phone[-3:]}" if len(phone) >= 8 else phone
                lines.append(f"• {platform} {order_id} — {masked}")
        if state.get("stale"):
            lines.append(
                f"\n⏭ {state['stale']} پیام قدیمی‌تر از {self._max_age_hours()} ساعت است و ارسال نمی‌شود."
            )
        quiet = self._in_quiet_hours(self._now())
        if quiet:
            lines.append(
                f"\n🌙 الان داخل ساعت سکوت ({self._quiet_label()}) است؛ مشتری‌ها شب پیامک می‌گیرند. "
                "فقط اگر واقعاً لازم است ادامه دهید."
            )
        lines.append("\n(سفارش‌های لغو، ارسال‌شده یا تحویل‌شده هنگام ارسال خودکار رد می‌شوند.)")
        go = "⚠️ با این حال شروع کن" if quiet else "✅ شروع ارسال"
        kb = {"inline_keyboard": [
            [{"text": go, "callback_data": f"{CALLBACK_PREFIX}go"}],
            [{"text": "❌ لغو", "callback_data": f"{CALLBACK_PREFIX}cancel"}],
        ]}
        self._say(chat_id, "\n".join(lines), kb)

    def _after_confirm(self, chat_id, repo: Repository, state: dict) -> None:
        now = self._now()
        override = self._in_quiet_hours(now)  # confirmed knowingly (see preview)
        mode = self.route_mode()
        if mode == "ask":
            state = dict(
                state, state="awaiting_ip", requested_at=now.isoformat(), override_quiet=override
            )
            self._save(repo, state)
            self._send_ip_prompt(repo, state)
            if chat_id != self.ip_chat():
                self._say(
                    chat_id,
                    f"📨 درخواست ارسال {state['pending']} پیامک برای روابط عمومی فرستاده شد؛ "
                    "منتظر آی‌پی گوشی هستیم.",
                )
            return
        # cloud: no local route at all ("" ); local: the fixed .env address (None)
        self._start(repo, "" if mode == "cloud" else None, override)

    def _send_ip_prompt(self, repo: Repository, state: dict) -> None:
        prev = repo.get_report_marker(_IP_KEY)
        rows = []
        if prev:
            rows.append([(f"✅ همان آی‌پی قبلی ({prev})", f"{CALLBACK_PREFIX}same")])
        rows.append([("☁️ فقط Cloud (بدون آی‌پی)", f"{CALLBACK_PREFIX}cloud")])
        rows.append([("❌ لغو", f"{CALLBACK_PREFIX}cancel")])
        kb = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
        self._say(
            self.ip_chat(),
            f"📨 {state['pending']} پیامک مشتری آمادهٔ ارسال است.\n\n"
            "۱) لطفاً اپ SMS Gateway را روی گوشی استارت کنید (دکمهٔ START).\n"
            "۲) آی‌پی Local Address گوشی را همین‌جا بفرستید، مثلاً 192.168.1.43\n"
            + (f"\nاگر عوض نشده، دکمهٔ «همان آی‌پی قبلی» ({prev}) را بزنید." if prev else ""),
            kb,
        )

    def _start(self, repo: Repository, ip: str | None, override_quiet: bool = False) -> None:
        """ip: "host:port" = Local route at that address; "" = Cloud only;
        None = Local at the fixed SMS_GATEWAY_URL from .env."""
        now = self._now()
        stale = self._skip_stale(repo, now)
        batch_id = uuid.uuid4().hex[:12]
        total = repo.claim_pending_customer_sms(batch_id)
        if total == 0:
            self._save(repo, None)
            self._say_all("📭 پیامی برای ارسال نمانده بود (همه قدیمی یا رد شده بودند).")
            return
        if ip:
            repo.set_report_marker(_IP_KEY, ip)
        state: dict = {
            "state": "running",
            "batch_id": batch_id,
            "ip": ip,
            "total": total,
            "stale": stale,
            "override_quiet": override_quiet,
            "handed": [],       # [platform, order_id] handed to the phone
            "skipped": [],      # [platform, order_id, reason]
            "routes": {"local": 0, "cloud": 0},
            "started_at": now.isoformat(),
            "last_send_at": None,
        }
        self._save(repo, state)
        self._apply_route(state)
        self._sms._local_down_until = None  # try the local route first
        interval = _env_int("CUSTOMER_SMS_BATCH_INTERVAL_SECONDS", 60)
        if ip:
            route = f"مسیر: اول Local ({ip})، در صورت نشدن Cloud."
        elif ip == "":
            route = "مسیر: فقط Cloud."
        else:
            route = "مسیر: اول Local (آدرس ثابت تنظیم‌شده)، در صورت نشدن Cloud."
        text = (
            f"🚀 شروع ارسال {total} پیامک (هر {interval} ثانیه یکی، به ترتیب ثبت سفارش).\n{route}"
            "\nگوشی باید روشن و آنلاین و اپ SMS Gateway در حال اجرا باشد."
        )
        if stale:
            text += f"\n⏭ {stale} پیام قدیمی رد شد."
        self._say_all(text)

    # ------------------------------------------------------------------
    # Status / retry buttons
    # ------------------------------------------------------------------
    def _status(self, chat_id, repo: Repository) -> None:
        now = self._now()
        by = repo.count_customer_sms_by_status()
        pending = by.get("pending", 0)
        stale = self._stale_count(repo, now)
        lines = ["📊 وضعیت صف پیامک مشتریان"]
        lines.append(f"📥 در صف: {pending}" + (f" (قدیمی و ردشدنی: {stale})" if stale else ""))
        lines.append(f"⏳ تأییدنشده توسط گوشی: {by.get('accepted', 0)}")
        lines.append(f"✅ ارسال‌شده در ۲۴ ساعت اخیر: {repo.count_customer_sms_sent_since(now - timedelta(hours=24))}")
        lines.append(f"❌ ناموفق: {by.get('failed', 0)}")
        lines.append(f"⏭ ردشده: {by.get('skipped', 0)}")
        state = self._load(repo)
        if state:
            if state["state"] == "running":
                lines.append(f"\n🚀 ارسال در حال انجام: {len(state['handed'])} از {state['total']}")
            elif state["state"] == "awaiting_ip":
                lines.append("\n⌛ منتظر آی‌پی گوشی از روابط عمومی")
            elif state["state"] == "awaiting_confirm":
                lines.append("\n⌛ منتظر تأیید شروع ارسال")
        mode = {"cloud": "فقط Cloud", "local": "Local ثابت + Cloud", "ask": "پرسیدن آی‌پی"}[self.route_mode()]
        lines.append(f"\nروش ارسال: {mode}")
        if self._in_quiet_hours(now):
            lines.append(f"🌙 الان ساعت سکوت ({self._quiet_label()}) است.")
        self._say(chat_id, "\n".join(lines))

    def _retry_request(self, chat_id, repo: Repository) -> None:
        state = self._load(repo)
        if state and state.get("state") == "running":
            self._say(chat_id, "⏳ ارسال در حال انجام است؛ بعد از پایانش دوباره امتحان کنید.")
            return
        rows = repo.list_customer_sms_by_status("accepted", 20)
        if not rows:
            self._say(chat_id, "✅ پیام تأییدنشده‌ای نداریم.")
            return
        total = repo.count_customer_sms_by_status().get("accepted", len(rows))
        lines = [f"⏳ {total} پیام را گوشی پذیرفته ولی ارسالش تأیید نشده:"]
        for platform, order_id, phone, _err in rows[:10]:
            lines.append(f"• {platform} {order_id} — {phone}")
        if total > 10:
            lines.append(f"… و {total - 10} مورد دیگر")
        lines.append(
            "\n⚠️ اول در اپ SMS Gateway (بخش پیام‌ها) و پیام‌های ارسالی گوشی چک کنید. "
            "اگر مطمئن هستید ارسال نشده‌اند، تأیید کنید تا به صف برگردند؛ در غیر این صورت "
            "مشتری دو بار پیامک می‌گیرد."
        )
        kb = {"inline_keyboard": [
            [{"text": "✅ ارسال نشده‌اند، به صف برگردان", "callback_data": f"{CALLBACK_PREFIX}retry"}],
            [{"text": "↩️ انصراف", "callback_data": f"{CALLBACK_PREFIX}retrycancel"}],
        ]}
        self._say(chat_id, "\n".join(lines), kb)

    def _retry_answer(self, chat_id, repo: Repository, confirmed: bool) -> None:
        if not confirmed:
            self._say(chat_id, "انصراف داده شد؛ چیزی تغییر نکرد.")
            return
        state = self._load(repo)
        if state and state.get("state") == "running":
            self._say(chat_id, "⏳ ارسال در حال انجام است؛ بعد از پایانش دوباره امتحان کنید.")
            return
        n = repo.requeue_unconfirmed_customer_sms("به درخواست اپراتور دوباره در صف قرار گرفت")
        self._say_all(
            f"🔁 {n} پیام تأییدنشده به صف برگشت. برای ارسال، دکمهٔ «{BUTTON_TEXT}» را بزنید."
        )

    def _apply_route(self, state: dict) -> None:
        ip = state.get("ip")
        self._sms.local_disabled = ip == ""
        self._sms.local_url_override = f"http://{ip}" if ip else None

    # ------------------------------------------------------------------
    # Scheduler tick (every ~10 s)
    # ------------------------------------------------------------------
    def tick(self, repo: Repository, now: datetime | None = None) -> None:
        try:
            if not self._tg.is_configured():
                return
            with self._lock:
                self._tick(repo, now or self._now())
        except Exception:
            log.exception("sms_batch: unexpected error in tick")

    def _tick(self, repo: Repository, now: datetime) -> None:
        state = self._load(repo)
        if not state:
            return
        if state["state"] in ("awaiting_ip", "awaiting_confirm"):
            if now - datetime.fromisoformat(state["requested_at"]) > _IP_WAIT:
                self._save(repo, None)
                what = "آی‌پی دریافت نشد" if state["state"] == "awaiting_ip" else "تأیید دریافت نشد"
                self._say_all(f"⌛ {what} و درخواست ارسال لغو شد. پیام‌ها در صف می‌مانند.")
            return
        if state["state"] != "running":
            return

        self._apply_route(state)
        # What did the phone do with what we already handed over?
        failed = self._sms._reconcile(repo, None, now)
        mine = {(p, o) for p, o in state["handed"]}
        failed = [f for f in failed if (f[0], f[1]) in mine]
        if failed:
            self._finish(repo, state, f"گوشی ارسال را ناموفق اعلام کرد: {failed[0][2]}")
            return

        inflight = repo.get_accepted_customer_sms()
        if inflight:
            stall = timedelta(minutes=_env_int("CUSTOMER_SMS_BATCH_STALL_MINUTES", 10))
            oldest = min(datetime.fromisoformat(r[5]) for r in inflight)
            if now - oldest > stall:
                self._finish(
                    repo, state,
                    "گوشی پیام را ظرف چند دقیقه نفرستاد (وضعیت Pending). گوشی آنلاین و اپ روشن است؟",
                )
            return  # wait for confirmation before the next one

        interval = timedelta(seconds=_env_int("CUSTOMER_SMS_BATCH_INTERVAL_SECONDS", 60))
        last = state.get("last_send_at")
        if last and now - datetime.fromisoformat(last) < interval:
            return

        rows = repo.get_batch_pending_customer_sms(state["batch_id"], 1)
        if not rows:
            self._finish(repo, state, None)
            return
        if not state.get("override_quiet") and self._in_quiet_hours(now):
            self._finish(
                repo, state,
                f"ساعت سکوت ({self._quiet_label()}) شروع شد؛ مشتری‌ها نباید شب پیامک بگیرند. "
                "بقیه در صف ماندند و صبح با دکمه ارسال می‌شوند.",
            )
            return
        platform, order_id, phone, _attempts = rows[0]

        if self._status_check is not None:
            try:
                reason = self._status_check(platform, order_id)
            except Exception as exc:
                self._finish(repo, state, f"وضعیت سفارش {order_id} ({platform}) قابل بررسی نبود: {exc}")
                return
            if reason:
                repo.mark_customer_sms_skipped(platform, order_id, reason)
                state["skipped"].append([platform, order_id, reason])
                self._save(repo, state)
                return  # next one on the next tick, no waiting

        try:
            route, gateway_id = self._sms._send(phone, MESSAGE_TEXT, now)
        except CustomerSmsError as exc:
            self._finish(repo, state, f"ارسال سفارش {order_id} ({platform}) ممکن نشد: {exc}")
            return
        repo.mark_customer_sms_accepted(platform, order_id, route, gateway_id, now)
        state["handed"].append([platform, order_id])
        state["routes"][route] = state["routes"].get(route, 0) + 1
        state["last_send_at"] = now.isoformat()
        self._save(repo, state)
        log.info("sms_batch: handed %s order %s to the phone (%s)", platform, order_id, route)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    def _finish(self, repo: Repository, state: dict, stop_reason: str | None) -> None:
        ok = 0
        unconfirmed: list[str] = []
        bad: list[str] = []
        for platform, order_id in state["handed"]:
            row = repo.get_customer_sms_details(platform, order_id)
            status = row[0] if row else "?"
            phone = row[3] if row else ""
            if status == "sent":
                ok += 1
            elif status == "accepted":
                unconfirmed.append(f"{platform} {order_id} — {phone}")
            else:
                bad.append(f"{platform} {order_id} — {phone} ← {(row[1] if row else '') or status}")
        remaining = len(repo.get_batch_pending_customer_sms(state["batch_id"], 10**6))
        lines = [
            "📨 گزارش ارسال پیامک مشتریان",
            f"✅ ارسال‌شده: {ok}",
            f"❌ ناموفق: {len(bad)}",
        ]
        if unconfirmed:
            lines.append(f"⏳ تأیید نشده توسط گوشی: {len(unconfirmed)}")
        if state["skipped"]:
            lines.append(f"⏭ ردشده (لغو/ارسال‌شده/نامعتبر): {len(state['skipped'])}")
        if state.get("stale"):
            lines.append(f"🕰 ردشده به‌خاطر قدیمی بودن: {state['stale']}")
        lines.append(f"📥 باقی‌مانده در صف: {remaining}")
        r = state["routes"]
        lines.append(f"مسیر: Local {r.get('local', 0)} | Cloud {r.get('cloud', 0)}")
        if stop_reason:
            lines.append(f"\n🛑 ارسال متوقف شد: {stop_reason}")
            lines.append("پیام‌های باقی‌مانده در صف ماندند و دفعهٔ بعد که دکمه را بزنید ادامه می‌یابند.")
        else:
            lines.append("\n🏁 ارسال تمام شد.")
        if bad:
            lines.append("\nنیاز به پیگیری (ناموفق):\n" + "\n".join(bad[:10]))
        if unconfirmed:
            lines.append(
                "\nنیاز به چک روی گوشی (تأییدنشده؛ دوباره ارسال نمی‌شود مگر با دکمهٔ «ارسال مجدد»):\n"
                + "\n".join(unconfirmed[:10])
            )
        if state["skipped"]:
            lines.append(
                "\nردشده‌ها:\n"
                + "\n".join(f"{p} {o} ← {why}" for p, o, why in state["skipped"][:5])
            )
        self._save(repo, None)
        self._sms.local_url_override = None
        self._sms.local_disabled = False
        self._say_all("\n".join(lines))
        log.info("sms_batch: finished ok=%d bad=%d remaining=%d reason=%s", ok, len(bad), remaining, stop_reason)
