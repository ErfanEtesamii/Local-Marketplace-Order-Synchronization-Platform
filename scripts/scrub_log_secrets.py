"""
One-off helper: removes API keys / tokens (Didar ?apikey=..., Telegram bot
token, Bearer tokens) from EXISTING log files, using the same rules the
live logger now applies (src/log_redaction.py). New log lines are already
clean after the logger fix - this only cleans history written before it.

Run from the project root (venv activated). STOP THE SERVICE FIRST - on
Windows the active logs/order-sync.log (and the NSSM stdout/stderr files)
are locked while the service runs and can't be rewritten:
    python -m scripts.scrub_log_secrets --dry-run
    python -m scripts.scrub_log_secrets
    python -m scripts.scrub_log_secrets path\\to\\service-stdout.log path\\to\\service-stderr.log

With no paths it scrubs every file matching logs/*.log* in the project.
Files are rewritten byte-faithfully (line endings and non-UTF-8 bytes are
preserved); only the secret values change.

AFTER RUNNING: the old key was readable by anyone who had these files
(they've been shared/copied around) - rotate the Didar API key and, if the
bot token showed up, the Telegram bot token (BotFather -> /revoke) and
update .env.
"""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from src.log_redaction import redact_secrets

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def scrub_file(path: Path, dry_run: bool) -> int:
    """Return the number of changed lines (0 = file already clean)."""
    data = path.read_bytes()
    text = data.decode("utf-8", errors="surrogateescape")
    changed = 0
    out_lines = []
    for line in text.splitlines(keepends=True):
        new = redact_secrets(line)
        if new != line:
            changed += 1
        out_lines.append(new)
    if changed and not dry_run:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write("".join(out_lines).encode("utf-8", errors="surrogateescape"))
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="Log files (default: logs/*.log*)")
    parser.add_argument("--dry-run", action="store_true", help="Only report, don't modify files")
    args = parser.parse_args()

    paths = args.paths or sorted((_PROJECT_ROOT / "logs").glob("*.log*"))
    total = 0
    for p in paths:
        if not p.is_file():
            print(f"skip (not a file): {p}")
            continue
        try:
            n = scrub_file(p, args.dry_run)
        except PermissionError:
            print(f"LOCKED (stop the service first): {p}")
            continue
        total += n
        if n:
            verb = "would clean" if args.dry_run else "cleaned"
            print(f"{verb} {n} line(s): {p}")
    print(f"done - {total} line(s) {'would be ' if args.dry_run else ''}changed across {len(paths)} file(s)")


if __name__ == "__main__":
    main()
