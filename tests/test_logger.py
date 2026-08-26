import time
from unittest.mock import patch

from src.logger import _WindowsSafeTimedRotatingFileHandler


def test_permission_error_during_rollover_is_handled_gracefully(tmp_path, capsys):
    """
    Regression test for a real production incident: a PermissionError
    during doRollover() (Windows file-lock, e.g. a second running
    instance) must not raise, and must leave rolloverAt in the future
    so it doesn't retry - and fail - on every single subsequent log
    call within the same day.
    """
    handler = _WindowsSafeTimedRotatingFileHandler(
        tmp_path / "test.log", when="midnight", backupCount=1
    )

    with patch(
        "logging.handlers.TimedRotatingFileHandler.doRollover",
        side_effect=PermissionError("file locked"),
    ):
        handler.doRollover()  # must not raise

    # The critical property: rolloverAt must be in the future relative
    # to "now", not left at a stale past value - a stale/past value is
    # exactly what causes shouldRollover() to keep firing (and failing)
    # on every subsequent log call.
    assert handler.rolloverAt > time.time()

    captured = capsys.readouterr()
    assert "log rotation skipped" in captured.err

    handler.close()