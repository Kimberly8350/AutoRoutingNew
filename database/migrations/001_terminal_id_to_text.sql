-- ============================================================
-- Migration: terminal_id INTEGER -> TEXT
--
-- Real terminal IDs from the ODBC feed are alphanumeric codes
-- (e.g. "T-01-TX-0001", "CALLDIS") — they were never numeric.
-- sync.py was coercing them to numeric and silently dropping/
-- nulling every row that didn't parse as an integer, which is
-- why terminal_id has been ending up blank/wrong on orders and
-- driver terminal cards. Run this once, then redeploy the
-- updated sync.py / backend so real string IDs start flowing in.
--
-- Existing terminal_id values here are the old (meaningless)
-- coerced integers — this migration does NOT recover them, it
-- just changes the column type so the corrected sync can write
-- real values. Follow with a full re-sync (see notes at bottom).
-- ============================================================

ALTER TABLE terminal_locations
    ALTER COLUMN terminal_id TYPE TEXT USING terminal_id::TEXT;

ALTER TABLE load_details
    ALTER COLUMN terminal_id TYPE TEXT USING terminal_id::TEXT;

ALTER TABLE driver_terminal_cards
    ALTER COLUMN terminal_id TYPE TEXT USING terminal_id::TEXT;

ALTER TABLE dispatch_results
    ALTER COLUMN terminal_id TYPE TEXT USING terminal_id::TEXT;

-- ============================================================
-- After running this:
-- 1. Deploy the updated sync.py (terminal_id no longer coerced
--    to numeric in transform_terminal_locations,
--    transform_load_details, transform_driver_terminal_cards).
-- 2. Truncate and re-sync terminal_locations from the ODBC
--    terminal master file so it's keyed by the real string ID
--    instead of the old stale integer PK:
--      TRUNCATE terminal_locations;
--    then run sync.py once to repopulate it.
-- 3. Re-run sync for load_details and driver_terminal_cards
--    (or wait for the next scheduled 5-min sync) so terminal_id
--    is populated with real string codes going forward.
-- 4. Historical rows already in load_details/driver_terminal_cards
--    with a null/stale terminal_id won't retroactively fix
--    themselves — only new/updated rows get the real ID on next
--    sync. If backfilling old rows matters, match them by
--    terminal_name against the refreshed terminal_locations table.
-- ============================================================
