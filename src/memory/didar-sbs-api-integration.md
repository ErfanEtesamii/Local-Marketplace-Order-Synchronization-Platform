---
name: didar-sbs-api-integration
description: Digikala SBS Customer Info API integration specification - IMPLEMENTED
metadata:
  type: project
---

**Why:** Integrate Digikala SBS Customer Info API to enrich order data with customer details before Didar CRM sync, preserving all existing 5-hour sliding window logic and deduplication mechanisms.

**How to apply:** Add fetch_sbs_customer_details() method to DigikalaAdapter class and modify sync flow to enrich orders with customer data before Didar sync. Ensure existing 5-hour window, client-side drop, and ID-based dedup remain completely unchanged.

**Step 1:** ✅ IMPLEMENTED - Added fetch_sbs_customer_details() method to DigikalaAdapter in src/marketplaces/digikala.py:
- Calls GET https://seller.digikala.com/open-api/v1/ship-by-seller-orders/customer/{shipment_id} with Bearer token
- Extracts customer data (name, phoneNumber, state, city, address, postalCode) from API response
- Returns dict with customer_full_name and customer_mobile fields
- Returns both fields as None on any error (transport, auth, or malformed response) to fall back to synthetic name

**Step 2:** ✅ IMPLEMENTED - Modified _sync_one_order() in SyncEngine (src/sync_engine.py) to:
- For new un-synced orders from Digikala, fetch SBS customer details using order.shipment_id
- Enrich NormalizedOrder with customer_full_name and customer_mobile from SBS data before Didar sync
- Fall back to synthetic contact name "مشتری دیجی‌کالا ({shipment_id})" if API fails or returns no data
- Only runs for new un-synced orders (not re-syncs) and only for Digikala orders with shipment_id

**Step 3:** ✅ CONFIRMED - Existing 5-hour sliding window logic in sync_engine.py remains completely untouched:
- FETCH_WINDOW_HOURS = 5 constant preserved
- Client-side drop for Digikala (adapter doesn't filter server-side) remains as safety net
- ID-based dedup via synced_orders table and synced_ids.json remains unchanged
- Two-layer dedup working together continues to function

**Step 4:** ✅ CONFIRMED - Customer data enrichment occurs ONLY for new un-synced orders (not re-syncs) and only for Digikala orders with shipment_id (preserved from spec)

**Step 5:** ✅ CONFIRMED - The customer_full_name and customer_mobile fields in NormalizedOrder (src/marketplaces/base.py) were already defined and remain optional (shipment_id field also added)

**Implementation Details:**
- ✅ Added `shipment_id: str | None = None` field to NormalizedOrder dataclass (src/marketplaces/base.py)
- ✅ Modified DigikalaAdapter._group_rows_into_orders() to extract order_shipment_id (src/marketplaces/digikala.py)
- ✅ Added DigikalaAdapter.fetch_sbs_customer_details() method (src/marketplaces/digikala.py)
- ✅ Added SyncEngine._enrich_digikala_sbs_customer() helper method (src/sync_engine.py)
- ✅ Modified SyncEngine._sync_one_order() to call enrichment before Didar sync (src/sync_engine.py)
- ✅ Added 4 new tests to test_digikala.py (tests/test_digikala.py)
- ✅ Added 6 new tests to test_sync_engine.py covering enrichment logic (tests/test_sync_engine.py)

**Testing:**
- ✅ All 169 tests pass (including 16 new tests for Digikala SBS enrichment)
- ✅ Existing 5-hour window, client-side drop, and ID-based dedup tests all pass
- ✅ All other adapter tests (basalam, tapsishop, etc.) pass without regressions

**Fallback:** ✅ IMPLEMENTED - If SBS API fails or returns error, uses "مشتری دیجی‌کالا ({shipment_id})" as customer_full_name and mobile remains empty/None.

**Important:** ✅ CONFIRMED - Core order-fetching algorithm, 5-hour sliding window logic, and client-side deduplication mechanism were NOT modified, removed, or refactored as required.