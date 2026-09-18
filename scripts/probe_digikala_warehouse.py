"""
STAGE 0 (manual, one-off) probe for the "ارسال به انبار دیجی‌کالا" (FBD)
feature - GET /open-api/v1/orders, "getting details of all active order
items seller".

WHY THIS IS A SCRIPT AND NOT PRODUCTION CODE
--------------------------------------------
Every field name, date format, price unit and pagination shape needed by
the FBD adapter is UNCONFIRMED: the endpoint has never been called from
this project, and this repo's own history shows that Digikala's docs and
its real payloads disagree (see src/marketplaces/digikala.py's module
docstring: /orders/history's documented created-at filter does not filter
server-side at all, and pager.total_pages is reported as 0). So nothing
about FBD gets written as production code until a REAL response has been
seen. This script prints that response verbatim; it is deliberately
placed under scripts/ (outside pytest.ini's `testpaths = tests`), exactly
like scripts/list_deal_labels.py and scripts/list_activity_types.py.

TOKEN HANDLING - READ THIS BEFORE EDITING
-----------------------------------------
The auth logic below (_load_tokens / _save_tokens / _refresh_access_token
/ _get) is a deliberate COPY of DigikalaAdapter's, per this project's
source-isolation convention (copy, never import - see the convention note
in src/marketplaces/digikala2.py). Two consequences:

  1. It reads AND writes the SAME cache file the production adapter uses:
     data/digikala_tokens.json (path derived from settings.db_path,
     identical to DigikalaAdapter.__init__). This is intentional. If this
     script kept its own token file and Digikala rotated the refresh_token
     on refresh (it may - see _refresh_access_token), one side's refresh
     would silently invalidate the other's stored token and break the live
     sync. Never point this script at a separate cache file.
  2. Any bug fixed in digikala.py's auth block must be re-applied here by
     hand.

SAFETY
------
This script is READ-ONLY. It never calls
DELETE /open-api/v1/orders/{order_item_id} - that endpoint most likely
cancels/rejects the order item, and the whole FBD feature is read-only.
Do not add a write call here "just to see what happens".

HOW TO RUN (from the project root, venv activated)
--------------------------------------------------
    python -m scripts.probe_digikala_warehouse

Redirect to a file to share the raw output:
    python -m scripts.probe_digikala_warehouse > fbd_probe.txt 2>&1

WHAT TO DO WITH THE OUTPUT
--------------------------
Section A prints the raw JSON of one small page - unfiltered, nothing
dropped. Section B prints the result of each query-parameter experiment
(status + error body). Section C prints an automatic, best-effort reading
of the 8 Stage-0 questions from the real payload; Section C is a HINT
ONLY - the raw JSON in Section A is the authority. Paste Sections A-C
back into the feature thread before any Stage-1 code is written.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx

from src.config import settings

# The endpoint under investigation. Confirmed (docs) as the FBD /
# "active order items" list; everything about its RESPONSE is unconfirmed.
ORDERS_PATH = "/open-api/v1/orders"

# Deliberately small: this is a probe, not a sync. Five rows is enough to
# see the shape without dumping a whole account into a terminal.
PROBE_PAGE_SIZE = 5

# Parameter experiments for Stage-0 question 3 (server-side time filter).
# Each is tried on its own so one 422 doesn't hide the others' results.
# The last one is an explicitly UNDOCUMENTED guess - it is expected to
# either be ignored or rejected; both outcomes are informative.
PARAM_EXPERIMENTS: list[tuple[str, dict, str]] = [
    (
        "sort + order (documented on other Digikala list endpoints)",
        {"page": 1, "size": PROBE_PAGE_SIZE, "sort": "order_created_at", "order": "desc"},
        "If this works, client-side pagination + early-exit is viable "
        "(same shape as _fetch_sbs_rows_since_watermark).",
    ),
    (
        "search[created_today]",
        {"page": 1, "size": PROBE_PAGE_SIZE, "search[created_today]": "true"},
        "If accepted AND it actually narrows the result set, a cheap "
        "'today only' poll is possible.",
    ),
    (
        "search[order_created_at_from] (UNDOCUMENTED GUESS)",
        {
            "page": 1,
            "size": PROBE_PAGE_SIZE,
            "search[order_created_at_from]": "2099-01-01",
        },
        "Deliberately absurd date: if the row count is UNCHANGED, the "
        "filter is silently ignored server-side - exactly the /orders/"
        "history trap documented in digikala.py. Only a row count of 0 "
        "proves it filters.",
    ),
]


class WarehouseProbe:
    """Minimal read-only client - copy of DigikalaAdapter's auth block."""

    def __init__(self) -> None:
        cfg = settings.digikala
        self._base_url = cfg.base_url
        self._env_access_token = cfg.access_token
        self._env_refresh_token = cfg.refresh_token
        # Same path expression as DigikalaAdapter.__init__ - see the
        # TOKEN HANDLING note in this module's docstring.
        self._token_cache_path = Path(settings.db_path).resolve().parent / "digikala_tokens.json"
        self._access_token, self._refresh_token = self._load_tokens()
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"content-type": "application/json"},
            timeout=30.0,
        )

    def _load_tokens(self) -> tuple[str, str]:
        """Prefer a previously-refreshed pair over the static .env seed,
        since refresh_token rotates and .env is not rewritten at runtime."""
        if self._token_cache_path.exists():
            try:
                cached = json.loads(self._token_cache_path.read_text())
                return cached["access_token"], cached["refresh_token"]
            except (json.JSONDecodeError, KeyError, OSError):
                print(
                    f"[warn] could not read cached tokens at {self._token_cache_path}, "
                    "falling back to .env"
                )
        return self._env_access_token, self._env_refresh_token

    def _save_tokens(self) -> None:
        self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_cache_path.write_text(
            json.dumps({"access_token": self._access_token, "refresh_token": self._refresh_token})
        )

    def _refresh_access_token(self) -> None:
        # Confirmed via official docs: BOTH tokens are required in the
        # body, despite the endpoint's name.
        resp = self._client.post(
            "/open-api/v1/auth/refresh-token",
            json={"access_token": self._access_token, "refresh_token": self._refresh_token},
        )
        if not resp.is_success:
            raise RuntimeError(f"refresh-token failed: {resp.status_code} {resp.text}")
        data = resp.json().get("data", {})
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._save_tokens()
        print("[info] access token refreshed and written back to the shared cache file")

    def get(self, path: str, params: dict, _already_refreshed: bool = False) -> httpx.Response:
        """Like DigikalaAdapter._get's 401 -> refresh -> retry-once, but
        returns the raw Response instead of raising: a 403/422 body is
        precisely what this probe is here to show."""
        resp = self._client.get(
            path, params=params, headers={"Authorization": f"Bearer {self._access_token}"}
        )
        if resp.status_code == 401 and not _already_refreshed:
            print("[info] 401 - refreshing access token and retrying once")
            self._refresh_access_token()
            return self.get(path, params, _already_refreshed=True)
        return resp

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# Section C helpers - best-effort reading of the 8 Stage-0 questions.
# Every one of these is a HINT; the raw JSON of Section A is the authority.
# --------------------------------------------------------------------------


def _rows_of(payload: dict) -> tuple[list, str]:
    """Question 8 (response shape). SBS's docs and its real payload
    disagreed about where the rows live, and digikala.py tolerates both -
    so look in several places rather than assuming data.items[]."""
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("items", "orders", "order_items", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value, f"data.{key}[]"
    if isinstance(data, list):
        return data, "data[] (list directly under 'data')"
    for key in ("items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value, f"{key}[] (top level)"
    return [], "NOT FOUND - inspect Section A by hand"


def _looks_jalali(value: str) -> str:
    """Question 5 (date format). A Jalali year is ~1400; a Gregorian one
    ~2020. digikala.py needs _parse_jalali_date for one and
    _parse_history_date for the other - this must not be guessed."""
    text = str(value)
    if text[:2] in ("13", "14") and len(text) >= 8:
        return "JALALI (leading 13xx/14xx) -> needs _parse_jalali_date-style parsing"
    if text[:2] == "20":
        return "GREGORIAN/ISO -> needs _parse_history_date-style parsing"
    return "UNCLEAR - decide by hand from Section A"


def _analyze(rows: list, shape: str) -> None:
    print(f"Q8  response shape          : rows found at {shape}; {len(rows)} row(s) on this page")

    if not rows:
        print("    -> no rows returned. Either the account has no active FBD items right")
        print("       now, or the shape differs. Re-run when the panel shows at least one")
        print("       'ارسال به انبار' item; do NOT design around an empty payload.")
        return

    row = rows[0]
    if not isinstance(row, dict):
        print("    -> first row is not an object; inspect Section A by hand")
        return

    keys = sorted(row.keys())
    print(f"    top-level keys of row[0]: {keys}")

    # Q1 - status field
    status_keys = [k for k in row if "status" in k.lower()]
    print(f"Q1  status field(s)         : {status_keys or 'NONE'}")
    for key in status_keys:
        print(f"      {key} = {row.get(key)!r}")
    if not any(k for k in status_keys if not k.endswith("_at")):
        print("    -> only timestamp-ish status fields. Likely no textual status enum, which")
        print("       supports the reading that 'active' already means confirmed. CONFIRM WITH")
        print("       THE CLIENT what 'تأیید شده' means before adding ANY status filter.")

    # Q2 - dedupe key
    for key in ("order_item_id", "id", "order_id", "product_variant_id", "variant_id"):
        if key in row:
            print(f"Q2  candidate key {key:<20}= {row.get(key)!r}")
    if "order_item_id" not in row:
        print("    -> no order_item_id. Check across ALL rows whether "
              "(order_id, product_variant_id) is unique before using it as "
              "source_shipment_id.")
    pairs = [
        (r.get("order_id"), r.get("product_variant_id"))
        for r in rows
        if isinstance(r, dict)
    ]
    if pairs and len(set(pairs)) != len(pairs):
        print("    -> WARNING: (order_id, product_variant_id) is ALREADY duplicated within")
        print("       this 5-row page. It cannot be the dedupe key.")

    # Q5 - date formats
    date_keys = [k for k in row if any(t in k.lower() for t in ("date", "_at", "time"))]
    print(f"Q5  date-ish fields         : {date_keys or 'NONE'}")
    for key in date_keys:
        value = row.get(key)
        if value is not None:
            print(f"      {key} = {value!r}  -> {_looks_jalali(value)}")

    # Q6 - price unit / per-unit vs total
    price_keys = [k for k in row if any(t in k.lower() for t in ("price", "amount", "cost"))]
    print(f"Q6  price fields            : {price_keys or 'NONE'}")
    for key in price_keys:
        print(f"      {key} = {row.get(key)!r}")
    selling = row.get("selling_price")
    quantity = row.get("quantity")
    total = row.get("total_price")
    if selling is not None and quantity is not None and total is not None:
        try:
            product = float(selling) * float(quantity)
            verdict = (
                "selling_price is PER UNIT (selling_price * quantity == total_price)"
                if abs(product - float(total)) < 0.01
                else f"MISMATCH: selling_price*quantity={product} vs total_price={total} "
                     "- do NOT derive one from the other"
            )
            print(f"    -> {verdict}")
        except (TypeError, ValueError):
            print("    -> prices are not numeric; inspect by hand")
    print("    NOTE: whatever the field, the value must still go through "
          "to_rial(value, config.price_unit) from src/currency.py.")

    # Q7 - supplier_code samples
    supplier_codes = [r.get("supplier_code") for r in rows if isinstance(r, dict)]
    print(f"Q7  supplier_code samples   : {supplier_codes}")
    print("    -> if these are short hand-made numbers, they must NOT be used as the")
    print("       Didar product Code (the 2026-09 'wrong product' incident, order")
    print("       382920341) - fall back to product_title, per _is_collision_prone_sku.")

    # Image + pager, needed by Stage 5 / Stage 2
    image_keys = [k for k in row if any(t in k.lower() for t in ("image", "photo", "picture"))]
    print(f"--  image field(s)          : {image_keys or 'NONE'}")
    for key in image_keys:
        print(f"      {key} = {row.get(key)!r}")


def _analyze_pager(payload: dict) -> None:
    data = payload.get("data")
    pager = data.get("pager") if isinstance(data, dict) else None
    print(f"--  pager                   : {pager!r}")
    if isinstance(pager, dict) and pager.get("total_pages") in (0, None):
        print("    -> total_pages is 0/absent, same as SBS. Pagination MUST use the")
        print("       two-signal guard (a full page is itself a reason to continue).")


def main() -> None:
    cfg = settings.digikala
    if not cfg.base_url or not (cfg.access_token or cfg.refresh_token):
        print("DIGIKALA_BASE_URL / DIGIKALA_ACCESS_TOKEN / DIGIKALA_REFRESH_TOKEN are not")
        print("set in .env - fill those in first (same values the live sync already uses).")
        return

    probe = WarehouseProbe()
    print(f"# FBD probe - {datetime.now().isoformat(timespec='seconds')}")
    print(f"# base_url = {cfg.base_url}   path = {ORDERS_PATH}")
    print(f"# price_unit configured for this source = {cfg.price_unit!r}")

    try:
        # ---------------- Section A: raw payload ----------------
        print("\n" + "=" * 78)
        print("SECTION A - RAW RESPONSE  (page=1, size=5, no filters, nothing removed)")
        print("=" * 78)
        resp = probe.get(ORDERS_PATH, {"page": 1, "size": PROBE_PAGE_SIZE})
        print(f"HTTP {resp.status_code} {resp.reason_phrase}  for {resp.url}")

        if resp.status_code == 403:
            print("\n403 - Q4 ANSWERED: the current token does NOT cover this endpoint.")
            print("Body below; if it mentions a new permission, a separate scope has to be")
            print("requested from Digikala support / the client before anything else.")
            print(resp.text)
            return
        if not resp.is_success:
            print("\nNon-success response - full body:")
            print(resp.text)
            return

        print("Q4  scope/token            : OK - the existing access_token covers this endpoint")
        try:
            payload = resp.json()
        except ValueError:
            print("Response is not JSON - raw text follows:")
            print(resp.text)
            return
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        # ---------------- Section B: parameter experiments ----------------
        print("\n" + "=" * 78)
        print("SECTION B - QUERY PARAMETER EXPERIMENTS (Q3: is there a server-side filter?)")
        print("=" * 78)
        baseline_rows, _ = _rows_of(payload)
        print(f"baseline row count with no filter (size={PROBE_PAGE_SIZE}): {len(baseline_rows)}")
        for label, params, why in PARAM_EXPERIMENTS:
            print("\n- " + label)
            print(f"  params : {params}")
            print(f"  why    : {why}")
            try:
                exp = probe.get(ORDERS_PATH, params)
            except httpx.HTTPError as exc:  # transport-level only
                print(f"  result : transport error: {exc!r}")
                continue
            print(f"  status : HTTP {exp.status_code} {exp.reason_phrase}")
            if exp.is_success:
                try:
                    exp_rows, _ = _rows_of(exp.json())
                    print(f"  rows   : {len(exp_rows)}")
                    if len(exp_rows) == len(baseline_rows):
                        print("  verdict: row count UNCHANGED - accepted but possibly IGNORED.")
                        print("           Do not rely on it without a second check.")
                except ValueError:
                    print("  body   : not JSON")
                    print(f"  raw    : {exp.text[:500]}")
            else:
                print(f"  body   : {exp.text[:800]}")

        # ---------------- Section C: automatic reading ----------------
        print("\n" + "=" * 78)
        print("SECTION C - AUTOMATIC READING OF THE STAGE-0 QUESTIONS (HINTS ONLY)")
        print("=" * 78)
        rows, shape = _rows_of(payload)
        _analyze(rows, shape)
        _analyze_pager(payload)

        print("\n" + "-" * 78)
        print("STILL UNANSWERABLE FROM THIS OUTPUT - ask the client / Didar support:")
        print("  * real PipelineId + PipelineStageId for the FBD pipeline")
        print("  * does Deal.save_v2 accept a Deal with NO PersonId? (live test in Didar)")
        print("  * cold start: on the first run, sync the whole active list or nothing?")
        print("  * exact Deal Label Title (run: python -m scripts.list_deal_labels)")
        print("-" * 78)
    finally:
        probe.close()


if __name__ == "__main__":
    main()
