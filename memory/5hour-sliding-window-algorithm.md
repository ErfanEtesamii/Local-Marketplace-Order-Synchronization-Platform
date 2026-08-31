---
name: 5hour-sliding-window-algorithm
description: Implementation of 5-hour sliding window algorithm for order synchronization with file-based ID tracking
metadata:
  type: project
---

## Summary

Implemented a 5-hour sliding window algorithm for order synchronization with file-based ID tracking to prevent re-syncing old orders (specifically addressing the Digikala 2-month-old order bug).

## Technical Details

### Core Algorithm

1. **Two-layer deduplication**:
   - Layer 1: File-based ID tracking via `data/synced_ids.json` (lightweight, fast)
   - Layer 2: SQLite `synced_orders` table for persistence, failure tracking, and Didar deal IDs

2. **5-hour sliding window enforcement**:
   - Constant: `FETCH_WINDOW_HOURS = 5`
   - SyncEngine computes `since=now-5h` and passes to all adapters
   - Client-side window drop: Any order with `created_at < since` is dropped regardless of adapter behavior
   - This provides a safety net for adapters like Digikala that don't filter server-side by date

3. **File-based ID tracking**:
   - JSON array stored in `data/synced_ids.json`
   - Unique ID format: `{platform}-{source_order_id}`
   - Methods: `_load_synced_ids()`, `_save_order_id_to_file()`, `_synced_ids_path()`
   - Test isolation via `synced_ids_file_path` parameter in `SyncEngine.__init__()`

### Files Modified

**src/sync_engine.py**:
- Added `synced_ids_file_path` parameter to `__init__`
- Added `_synced_ids_path()` method for flexible path resolution
- Updated `_load_synced_ids()` and `_save_order_id_to_file()` to use configurable path
- Added module-level `FETCH_WINDOW_HOURS = 5` constant
- Enhanced `_sync_source()` with client-side window dropping logic

**tests/test_sync_engine.py**:
- Removed direct `settings` import, added `Path` and `Decimal` imports
- All `SyncEngine()` constructions include `synced_ids_file_path=str(tmp_path / "synced_ids.json")`
- Fixed timing assertion type error: `before - timedelta(hours=FETCH_WINDOW_HOURS)`
- Pre-seed `synced_ids.json` in tests requiring file-based dedup verification
- Tests now use isolated tmp_path files preventing cross-test contamination

### Verification

All 149 tests pass, confirming:
- 5-hour sliding window properly enforced via `since=now-5h` parameter
- Client-side window dropping prevents old orders from reaching Didar
- File-based deduplication works correctly across test runs
- Test isolation prevents pollution of real `data/synced_ids.json`
- No regressions in existing marketplace adapter functionality
- Failure tracking and retry mechanisms remain intact via SQLite repository

### Key Benefits

1. **Fixes Digikala 2-month-old order bug**: Even if Digikala returns entire order history, orders older than 5 hours are dropped client-side
2. **Lightweight deduplication**: JSON file provides fast in-memory deduplication set
3. **Persistence**: Synced IDs survive application restarts via file tracking
4. **Testability**: Isolated file paths prevent test cross-contamination
5. **Fallback safety**: Repository still tracks failures and Didar deal IDs for reliability

## Why This Approach

The two-layer approach provides both performance (file-based dedup) and reliability (SQLite persistence):
- Fast startup: Load IDs from JSON into memory set
- Persistence: JSON file survives restarts
- Reliability: SQLite handles failures, retries, and Didar deal mapping
- Safety: Client-side window drop protects against misbehaving adapters

This solves the original issue where Digikala's adapter was returning the entire account history despite date parameters, causing old orders to be synced to Didar.