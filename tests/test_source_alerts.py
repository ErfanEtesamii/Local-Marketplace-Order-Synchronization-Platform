from datetime import datetime, timedelta, timezone

from src.source_alerts import SourceOutageAlerter, SourceStatusTracker

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


class Sender:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def __call__(self, text):
        self.messages.append(text)
        return self.ok


def make(sender=None):
    tracker = SourceStatusTracker()
    sender = sender or Sender()
    alerter = SourceOutageAlerter(
        tracker, sender, alert_after=timedelta(minutes=30), reminder_every=timedelta(minutes=120)
    )
    return tracker, sender, alerter


def test_no_alert_before_threshold():
    tracker, sender, alerter = make()
    tracker.record_failure("digikala", RuntimeError("401"), now=T0)
    alerter.check(now=T0 + timedelta(minutes=29))
    assert sender.messages == []


def test_alert_once_after_threshold_with_error_text():
    tracker, sender, alerter = make()
    tracker.record_failure("digikala", RuntimeError("Invalid User-Agent!"), now=T0)
    alerter.check(now=T0 + timedelta(minutes=31))
    alerter.check(now=T0 + timedelta(minutes=33))
    assert len(sender.messages) == 1
    assert "digikala" in sender.messages[0]
    assert "Invalid User-Agent!" in sender.messages[0]


def test_reminder_after_interval_then_recovery():
    tracker, sender, alerter = make()
    tracker.record_failure("digikala", RuntimeError("x"), now=T0)
    alerter.check(now=T0 + timedelta(minutes=31))
    alerter.check(now=T0 + timedelta(minutes=100))  # too soon for reminder
    assert len(sender.messages) == 1
    alerter.check(now=T0 + timedelta(minutes=152))  # 121 min after alert
    assert len(sender.messages) == 2
    tracker.record_success("digikala")
    alerter.check(now=T0 + timedelta(minutes=160))
    assert len(sender.messages) == 3
    assert "✅" in sender.messages[2]
    alerter.check(now=T0 + timedelta(minutes=170))
    assert len(sender.messages) == 3


def test_success_resets_clock_so_flapping_source_does_not_alert():
    tracker, sender, alerter = make()
    tracker.record_failure("basalam", RuntimeError("x"), now=T0)
    tracker.record_success("basalam")
    tracker.record_failure("basalam", RuntimeError("x"), now=T0 + timedelta(minutes=25))
    alerter.check(now=T0 + timedelta(minutes=40))  # only 15 min since the new first failure
    assert sender.messages == []


def test_failed_send_is_retried_next_cycle():
    sender = Sender(ok=False)
    tracker, _, alerter = make(sender)
    tracker.record_failure("digikala", RuntimeError("x"), now=T0)
    alerter.check(now=T0 + timedelta(minutes=31))
    alerter.check(now=T0 + timedelta(minutes=33))
    assert len(sender.messages) == 2  # tried twice, never counted as delivered
    sender.ok = True
    alerter.check(now=T0 + timedelta(minutes=35))
    alerter.check(now=T0 + timedelta(minutes=37))
    assert len(sender.messages) == 3  # delivered once, then silent


def test_sources_are_independent():
    tracker, sender, alerter = make()
    tracker.record_failure("digikala", RuntimeError("x"), now=T0)
    tracker.record_success("snappshop")
    alerter.check(now=T0 + timedelta(minutes=31))
    assert len(sender.messages) == 1
    assert "snappshop" not in sender.messages[0]


def test_engine_records_fetch_failure_and_success():
    from src.sync_engine import SyncEngine

    class Boom:
        name = "digikala"

        def fetch_new_orders(self, since):
            raise RuntimeError("401 Invalid User-Agent!")

    class FakeRepo:
        def get_ignored_ids(self, platform):
            return set()

    engine = SyncEngine.__new__(SyncEngine)
    engine._repo = FakeRepo()
    engine.source_status = SourceStatusTracker()
    engine._sync_source(Boom())
    state = engine.source_status.state("digikala")
    assert state.first_failure_at is not None
    assert "Invalid User-Agent" in state.last_error
