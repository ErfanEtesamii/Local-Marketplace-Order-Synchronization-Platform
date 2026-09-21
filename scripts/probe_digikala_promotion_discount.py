"""
STAGE 0 (manual, one-off) probe, part 3 of the "تخفیف کالا ثبت نمیشه" fix.

WHY THIS EXISTS - WHAT'S BEEN RULED OUT SO FAR
-----------------------------------------------
Confirmed live, in order, this session:

1. /orders/history: never has a row for an SBS order that hasn't reached
   a later status yet (order_type is documented as processed/returned/
   canceled only) - 18/18 real orders in the client's logs missed it.
2. /open-api/v1/orders ("active order items"): order 383370810 (a real,
   confirmed SBS order - see src/marketplaces/digikala.py's SBS-only
   architecture) returned total_rows=0 via search[search_term], while a
   DIFFERENT, non-SBS order (375458425, confirmed via `Select-String
   375458425 order-sync.log` returning NOTHING - this project's sync
   never touched it) WAS found there. This means /orders and /orders/
   history both belong to Digikala's classic (non-SBS) order pipeline -
   an entirely separate numbering space from SBS orders. Neither
   endpoint can ever enrich an SBS order's price/discount, regardless of
   status or timing.
3. /ship-by-seller-orders' own variants[] was already confirmed (see
   digikala.py's module docstring, 2026-09) to expose only price+count -
   no discount field at all on that endpoint either.

WHAT THIS PROBE TESTS NEXT
---------------------------
The Promotions API - GET /open-api/v1/pricing/promotions/
variant-current-promotions/{product_variant_id} - is keyed by Digikala's
product VARIANT id, not by order_id, so it should be entirely independent
of which order pipeline (SBS vs classic) the order came through. The
seller-facing docs' own sample for /ship-by-seller-orders shows a
`variantId` field on each item DISTINCT from `productId` (e.g.
productId="123" vs variantId=456) - this project's own code has so far
only ever read `productId` (see digikala.py's `sku = ... v.get
("productId", "")`), never `variantId`. If `variantId` is what the
Promotions endpoint actually wants, this probe finds the real discount
without touching /orders or /orders/history at all.

This script:
  Step 1 - GET /ship-by-seller-orders with search[search_term]=<order_id>
           to get the REAL variantId(s) for that order's item(s) (never
           guessed/typed by hand).
  Step 2 - for each variantId found, GET /open-api/v1/pricing/promotions/
           variant-current-promotions/{variantId} and print the raw
           response.

Nothing is wired into production code from this until the raw output
below is compared BY HAND against the known true discount (e.g. the 5%
on product code 3818 seen in the Didar screenshot) for the SAME item.

TOKEN HANDLING - same convention/cache file as the other probe scripts
in this directory (copy of DigikalaAdapter's auth block; do not point
this at a separate token cache).

SAFETY: read-only. Only ever calls the two GET endpoints named above.

HOW TO RUN (from the project root, venv activated)
---------------------------------------------------
    python -m scripts.probe_digikala_promotion_discount <order_id>

Example:
    python -m scripts.probe_digikala_promotion_discount 383370810
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

from src.config import settings

SBS_LIST_PATH = "/open-api/v1/ship-by-seller-orders"
PROMOTION_PATH_TMPL = "/open-api/v1/pricing/promotions/variant-current-promotions/{variant_id}"


class Probe:
    """Minimal read-only client - copy of DigikalaAdapter's auth block,
    same token cache file as the project's other probe scripts."""

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


def _sbs_rows_of(payload: dict) -> list:
    data = payload.get("data")
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return items
    if isinstance(data, list):
        return data
    return []


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.probe_digikala_promotion_discount <order_id>")
        sys.exit(1)
    order_id = sys.argv[1]

    cfg = settings.digikala
    if not cfg.base_url or not (cfg.access_token or cfg.refresh_token):
        print("DIGIKALA_BASE_URL / DIGIKALA_ACCESS_TOKEN / DIGIKALA_REFRESH_TOKEN are not")
        print("set in .env - fill those in first (same values the live sync already uses).")
        return

    probe = Probe()
    print(f"# promotion-discount probe - {datetime.now().isoformat(timespec='seconds')}")
    print(f"# base_url = {cfg.base_url}   order_id = {order_id}")

    try:
        print("\n" + "=" * 78)
        print(f"STEP 1 - GET {SBS_LIST_PATH} search[search_term]={order_id}")
        print("=" * 78)
        resp = probe.get(SBS_LIST_PATH, {"page": 1, "size": 10, "search[search_term]": order_id})
        print(f"HTTP {resp.status_code} {resp.reason_phrase} for {resp.url}")
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

        rows = _sbs_rows_of(payload)
        if not rows:
            print(
                f"\nNo SBS row found for order_id={order_id!r} either - double check the "
                "order_id, or this order may already be too old for the SBS list's own "
                "default window (try search[past]=true)."
            )
            return

        variant_ids: list[int] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for v in row.get("variants") or []:
                vid = v.get("variantId")
                title = v.get("title")
                product_id = v.get("productId")
                print(
                    f"\nFound item: title={title!r}  productId={product_id!r}  "
                    f"variantId={vid!r}  price={v.get('price')!r}  count={v.get('count')!r}"
                )
                if vid is not None:
                    variant_ids.append(vid)

        if not variant_ids:
            print(
                "\nNo variantId field found on any item of this SBS row - the documented "
                "field may not exist on this account's real payload. Inspect the raw JSON "
                "above by hand for whatever IS there."
            )
            return

        for vid in variant_ids:
            path = PROMOTION_PATH_TMPL.format(variant_id=vid)
            print("\n" + "=" * 78)
            print(f"STEP 2 - GET {path}")
            print("=" * 78)
            presp = probe.get(path, {"page": 1, "size": 200})
            print(f"HTTP {presp.status_code} {presp.reason_phrase} for {presp.url}")
            if not presp.is_success:
                print("\nNon-success response - full body:")
                print(presp.text)
                continue
            try:
                ppayload = presp.json()
            except ValueError:
                print("Response is not JSON - raw text follows:")
                print(presp.text)
                continue
            print(json.dumps(ppayload, ensure_ascii=False, indent=2))
            items = (ppayload.get("data") or {}).get("items") or []
            if not items:
                print(
                    f"\nNo current promotion for variantId={vid} - either this item has no "
                    "active discount right now, or the price gap you're seeing (e.g. the "
                    "5% on the Didar screenshot) comes from something other than a "
                    "'promotion' in Digikala's own terms (e.g. a seller-set sale price with "
                    "no formal promotion attached). COMPARE BY HAND against the true known "
                    "discount for this item before concluding either way."
                )
            else:
                print(
                    f"\n{len(items)} current promotion(s) for variantId={vid} - compare "
                    "promotion_rrp_price / promotion_selling_price above against the known "
                    "true original/discounted price for this item by hand."
                )
    finally:
        probe.close()


if __name__ == "__main__":
    main()
