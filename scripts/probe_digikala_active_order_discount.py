"""
STAGE 0 (manual, one-off) probe for the "تخفیف کالا ثبت نمیشه" fix -
GET /open-api/v1/orders, "getting details of all active order items
seller", searched by a specific order_id via search[search_term].

WHY THIS EXISTS
---------------
Confirmed live (client's server logs, 2026-09, 18/18 digikala orders):
/orders/history NEVER contains a row for an order that hasn't reached a
later status yet (order_type there is documented as processed/returned/
canceled only - see src/marketplaces/digikala.py's _fetch_history_price_map
docstring), so it's unusable for enriching price/discount at the moment a
shipment is first synced. The client confirmed this directly: "کالاهای ما
وضعیتشون نهایی نیس".

/open-api/v1/orders is a DIFFERENT, more promising endpoint - the docs'
own description says "active order items", and it's already used
elsewhere in this project (scripts/probe_digikala_warehouse.py, for the
separate FBD warehouse-sync feature) - but its response has ONLY ever
been inspected for THAT feature's fields (status/dedupe-key/dates/image).
Its price fields (selling_price, amazing_discount, discount_manager,
total_price) have never been checked against a real order, and the docs'
own example is internally ambiguous:

    selling_price: 645800, amazing_discount: 1200,
    discount_manager: 123, total_price: 645800  (quantity: 1)

selling_price == total_price here, which could mean either
(a) selling_price is ALREADY the post-discount price (in which case the
    ORIGINAL pre-discount price isn't in this row at all and would have
    to be reconstructed from amazing_discount/discount_manager - unknown
    units, could be currency amounts OR percentages OR campaign ids), or
(b) this particular example row simply has zero effective discount and
    amazing_discount/discount_manager are unrelated flags.

Nothing about this gets wired into src/marketplaces/digikala.py until a
REAL row - for an order whose true discount we already know from the
seller panel/screenshot - has been seen. This script prints that row
verbatim. Placed under scripts/ (outside pytest.ini's `testpaths =
tests`), same convention as probe_digikala_warehouse.py.

TOKEN HANDLING - same convention as scripts/probe_digikala_warehouse.py:
the auth block below is a deliberate COPY of DigikalaAdapter's (project's
source-isolation convention - copy, never import). It reads AND writes
the SAME token cache file the production adapter uses
(data/digikala_tokens.json) - do not point this at a separate file.

SAFETY: read-only. Only ever calls GET /open-api/v1/orders.

HOW TO RUN (from the project root, venv activated)
---------------------------------------------------
    python -m scripts.probe_digikala_active_order_discount <order_id>

Example, using an order from the client's own logs:
    python -m scripts.probe_digikala_active_order_discount 375356010

Redirect to a file to share the raw output:
    python -m scripts.probe_digikala_active_order_discount 375356010 > discount_probe.txt 2>&1

WHAT TO DO WITH THE OUTPUT
---------------------------
Paste the full output back into the feature thread. Ideally run this for
an order where you ALSO know the true unit price and discount from the
Digikala seller panel (e.g. the same order shown in the Didar screenshot),
so the row's fields can be checked against a known-correct answer, not
just eyeballed.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

from src.config import settings

ORDERS_PATH = "/open-api/v1/orders"


class ActiveOrdersProbe:
    """Minimal read-only client - copy of DigikalaAdapter's auth block
    (see TOKEN HANDLING note in this module's docstring)."""

    def __init__(self) -> None:
        cfg = settings.digikala
        self._base_url = cfg.base_url
        self._env_access_token = cfg.access_token
        self._env_refresh_token = cfg.refresh_token
        self._token_cache_path = Path(settings.db_path).resolve().parent / "digikala_tokens.json"
        self._access_token, self._refresh_token = self._load_tokens()
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"content-type": "application/json"},
            timeout=30.0,
        )

    def _load_tokens(self) -> tuple[str, str]:
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


def _rows_of(payload: dict) -> tuple[list, str]:
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
    return [], "NOT FOUND - inspect the raw JSON above by hand"


def _analyze_price_fields(rows: list) -> None:
    if not rows:
        print("No rows returned for this order_id via search[search_term] - either the")
        print("order doesn't appear in THIS endpoint either, or the search param name/")
        print("value format is different from what's documented. Try re-running with")
        print("no search param at all (page=1) and manually looking for the order_id")
        print("in the dump to compare.")
        return

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        print(f"\n--- row {i} ---")
        for key in (
            "order_id", "product_variant_id", "supplier_code", "quantity",
            "selling_price", "amazing_discount", "discount_manager", "total_price",
        ):
            print(f"  {key:<20} = {row.get(key)!r}")

        selling = row.get("selling_price")
        quantity = row.get("quantity")
        total = row.get("total_price")
        if selling is not None and quantity is not None and total is not None:
            try:
                product = float(selling) * float(quantity)
                if abs(product - float(total)) < 0.01:
                    print(
                        "  -> selling_price * quantity == total_price: selling_price "
                        "looks like a PER-UNIT price with NO further discount applied "
                        "on top at the total_price stage."
                    )
                else:
                    print(
                        f"  -> MISMATCH: selling_price*quantity={product} vs "
                        f"total_price={total} - something else (fee, tax, or a discount "
                        "applied between the two) is happening; do not assume which."
                    )
            except (TypeError, ValueError):
                pass

        amazing = row.get("amazing_discount")
        manager = row.get("discount_manager")
        if selling is not None and (amazing is not None or manager is not None):
            print(
                "  -> COMPARE BY HAND against the known true price for this order "
                "(seller panel / Didar screenshot): is (selling_price + amazing_discount "
                "+ discount_manager) the ORIGINAL pre-discount price? Is selling_price "
                "itself already the discounted price, or the original? Do not assume - "
                "this is exactly what this probe exists to answer."
            )


def main() -> None:
    # order_id is now OPTIONAL (2026-09, follow-up to the first probe run:
    # order 383370810 came back with total_rows=0 via search[search_term] -
    # meaning EITHER the order already fell out of the "active" list
    # (shipped/processed since the ~18h-earlier sync run this project's
    # own logs show for it - "active" may only be a short pre-processing
    # window, not a lasting one) OR search_term doesn't behave as
    # documented. Running with no order_id at all lists whatever IS
    # currently active, unfiltered - answers both questions: whether the
    # account has ANY active rows right now, and what their real
    # order_id/price fields look like, so a genuinely still-active order
    # can be picked for a second, targeted search[search_term] run.
    order_id = sys.argv[1] if len(sys.argv) > 1 else None

    cfg = settings.digikala
    if not cfg.base_url or not (cfg.access_token or cfg.refresh_token):
        print("DIGIKALA_BASE_URL / DIGIKALA_ACCESS_TOKEN / DIGIKALA_REFRESH_TOKEN are not")
        print("set in .env - fill those in first (same values the live sync already uses).")
        return

    probe = ActiveOrdersProbe()
    print(f"# active-orders discount probe - {datetime.now().isoformat(timespec='seconds')}")
    print(f"# base_url = {cfg.base_url}   path = {ORDERS_PATH}   order_id = {order_id!r}")

    params = {"page": 1, "size": 10}
    if order_id is not None:
        params["search[search_term]"] = order_id

    try:
        print("\n" + "=" * 78)
        label = f"search[search_term]={order_id}" if order_id is not None else "NO FILTER (unfiltered page 1)"
        print(f"SECTION A - RAW RESPONSE for {label}")
        print("=" * 78)
        resp = probe.get(ORDERS_PATH, params)
        print(f"HTTP {resp.status_code} {resp.reason_phrase} for {resp.url}")

        if resp.status_code == 403:
            print("\n403 - the current token does not cover this endpoint.")
            print(resp.text)
            return
        if not resp.is_success:
            print("\nNon-success response - full body:")
            print(resp.text)
            return

        try:
            payload = resp.json()
        except ValueError:
            print("Response is not JSON - raw text follows:")
            print(resp.text)
            return
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        print("\n" + "=" * 78)
        print("SECTION B - PRICE FIELD READING FOR THIS order_id (HINTS ONLY)")
        print("=" * 78)
        rows, shape = _rows_of(payload)
        print(f"rows found at {shape}; {len(rows)} row(s)")
        if order_id is not None:
            matching = [
                r for r in rows
                if isinstance(r, dict) and str(r.get("order_id")) == str(order_id)
            ]
            if not matching:
                print(
                    f"\nNone of the returned rows has order_id == {order_id!r} - "
                    "search[search_term] may not filter the way assumed, or match on a "
                    "different field. Showing ALL returned rows instead:"
                )
                matching = rows
        else:
            matching = rows
            print(
                "\nAll currently 'active' order items on the account (no filter) - "
                "check whether this list is empty (would confirm the order already "
                "left the active state) and, if not empty, pick one you know is still "
                "genuinely pending/unshipped to re-run with its order_id."
            )
        _analyze_price_fields(matching)
    finally:
        probe.close()


if __name__ == "__main__":
    main()
