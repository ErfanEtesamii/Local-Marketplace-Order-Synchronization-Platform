"""Which EXPRESS_ALERT_SMS_RECIPIENT_<N> env vars exist.

The express-order SMS alert (src/modir_payamak.py) used to read a hard-coded
list _1.._7, so adding an eighth number meant editing code. Now ANY number of
slots works: every env var named EXPRESS_ALERT_SMS_RECIPIENT_<digits> is a
recipient (its name comes from the same var with a _NAME suffix), in numeric
order. To add a person just add the next two lines to .env and restart.
"""
from __future__ import annotations

import os
import re

_RE = re.compile(r"^EXPRESS_ALERT_SMS_RECIPIENT_(\d+)$")


def express_recipient_vars() -> list[str]:
    """Env var names of all recipient slots present in the environment
    (blank values included - callers skip those), sorted by slot number."""
    found = []
    for key in os.environ:
        m = _RE.match(key)
        if m:
            found.append((int(m.group(1)), key))
    return [key for _, key in sorted(found)]
