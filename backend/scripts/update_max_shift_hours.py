"""
Update max_shift_hours in driver_schedules using per-driver, per-day-of-week
averages from the Driver Hours workbook.

Source: Data/Driver_Hours_2025-06-2026-05.xlsx → "Avg Hours by Day" sheet

This script reads the historical average hours each driver works on each day of
the week, then updates the driver_schedules table so the routing engine uses
realistic shift limits instead of the flat 12-hour default.

Usage:
    # Dry run — prints SQL without executing
    python backend/scripts/update_max_shift_hours.py --dry-run

    # Execute against Supabase
    python backend/scripts/update_max_shift_hours.py

Requires: openpyxl, supabase-py, python-dotenv
"""

import os
import sys
import argparse
import logging
from pathlib import Path

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import openpyxl
except ImportError:
    print("ERROR: openpyxl required. Install with: pip install openpyxl")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Day-of-week mapping: Excel column index → Python weekday (0=Monday)
# Excel columns: Driver(0), Unit(1), Type(2), Mon(3), Tue(4), Wed(5), Thu(6), Fri(7), Sat(8), Sun(9), Overall(10)
DAY_COLUMNS = {
    0: 3,   # Monday → column index 3
    1: 4,   # Tuesday → column index 4
    2: 5,   # Wednesday → column index 5
    3: 6,   # Thursday → column index 6
    4: 7,   # Friday → column index 7
    5: 8,   # Saturday → column index 8
    6: 9,   # Sunday → column index 9
}

OVERALL_AVG_COL = 10


def load_driver_hours(excel_path: str) -> dict[str, dict]:
    """
    Returns: {
        "driver_name": {
            "unit": "1106",
            "type": "OO",
            "by_day": {0: 10.44, 1: 10.83, ...},  # Monday=0
            "overall": 10.4
        }
    }
    """
    wb = openpyxl.load_workbook(excel_path, read_only=True)
    ws = wb["Avg Hours by Day"]
    rows = list(ws.iter_rows(min_row=4, values_only=True))  # skip title + subtitle + header

    drivers = {}
    for row in rows:
        name = row[0]
        if not name:
            continue

        name = str(name).strip()
        by_day = {}
        for weekday, col_idx in DAY_COLUMNS.items():
            val = row[col_idx]
            if val is not None:
                try:
                    by_day[weekday] = float(val)
                except (ValueError, TypeError):
                    pass

        overall = None
        if row[OVERALL_AVG_COL] is not None:
            try:
                overall = float(row[OVERALL_AVG_COL])
            except (ValueError, TypeError):
                pass

        drivers[name] = {
            "unit": str(row[1] or ""),
            "type": str(row[2] or ""),
            "by_day": by_day,
            "overall": overall,
        }

    wb.close()
    log.info(f"Loaded average hours for {len(drivers)} drivers from Excel")
    return drivers


def get_max_shift_for_day(driver_data: dict, weekday: int) -> float:
    """
    Get the max_shift_hours for a driver on a given weekday.
    Uses day-specific average if available, otherwise overall average,
    otherwise default 12.0.
    """
    hours = driver_data["by_day"].get(weekday)
    if hours is None:
        hours = driver_data.get("overall")
    if hours is None:
        hours = 12.0
    return round(hours, 2)


def match_driver_name(excel_name: str, db_first: str, db_last: str) -> bool:
    """Match Excel name (e.g. 'Anthony Lopez') to DB first/last name."""
    excel_lower = excel_name.lower().strip()
    db_full = f"{db_first} {db_last}".lower().strip()
    return excel_lower == db_full


def run_dry(driver_hours: dict):
    """Print what updates would be made."""
    print("\n--- DRY RUN: Proposed max_shift_hours per driver per day ---\n")
    print(f"{'Driver':<25} {'Mon':>6} {'Tue':>6} {'Wed':>6} {'Thu':>6} {'Fri':>6} {'Sat':>6} {'Sun':>6} {'Overall':>8}")
    print("-" * 95)
    for name, data in sorted(driver_hours.items()):
        days = [f"{get_max_shift_for_day(data, d):>6.2f}" for d in range(7)]
        overall = f"{data['overall']:.2f}" if data['overall'] else "N/A"
        print(f"{name:<25} {' '.join(days)} {overall:>8}")
    print(f"\nTotal drivers: {len(driver_hours)}")
    print("\nTo apply these to your database, run without --dry-run flag.")


def run_update(driver_hours: dict):
    """Update driver_schedules in Supabase with per-day max_shift_hours."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    from supabase import create_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY required in backend/.env")
        sys.exit(1)

    client = create_client(url, key)

    # Fetch all driver_schedules records
    log.info("Fetching driver_schedules from Supabase...")
    all_records = []
    page_size = 1000
    offset = 0
    while True:
        batch = (
            client.table("driver_schedules")
            .select("record_id, driver_id, first_name, last_name, shift_date, max_shift_hours")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        all_records.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    log.info(f"Found {len(all_records)} schedule records")

    # Build name → excel data lookup (lowercased)
    excel_lookup = {name.lower(): data for name, data in driver_hours.items()}

    updates = []
    matched_drivers = set()
    unmatched_drivers = set()

    for rec in all_records:
        first = rec.get("first_name") or ""
        last = rec.get("last_name") or ""
        full_name = f"{first} {last}".strip().lower()

        if full_name not in excel_lookup:
            unmatched_drivers.add(full_name)
            continue

        matched_drivers.add(full_name)
        data = excel_lookup[full_name]

        # Get day of week from shift_date
        shift_date_str = rec.get("shift_date")
        if not shift_date_str:
            continue

        from datetime import date as date_cls
        try:
            sd = date_cls.fromisoformat(str(shift_date_str)[:10])
        except ValueError:
            continue

        weekday = sd.weekday()  # Monday=0
        new_hours = get_max_shift_for_day(data, weekday)
        current_hours = float(rec.get("max_shift_hours") or 12.0)

        # Only update if different
        if abs(new_hours - current_hours) > 0.01:
            updates.append({
                "record_id": rec["record_id"],
                "max_shift_hours": new_hours,
            })

    log.info(f"Matched {len(matched_drivers)} drivers, {len(unmatched_drivers)} unmatched")
    if unmatched_drivers:
        log.warning(f"Unmatched drivers (no Excel data): {sorted(unmatched_drivers)[:10]}...")

    log.info(f"Updating {len(updates)} records with new max_shift_hours...")

    # Batch update in chunks
    chunk_size = 100
    updated_count = 0
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i:i + chunk_size]
        for u in chunk:
            try:
                client.table("driver_schedules").update(
                    {"max_shift_hours": u["max_shift_hours"]}
                ).eq("record_id", u["record_id"]).execute()
                updated_count += 1
            except Exception as e:
                log.error(f"Failed to update record_id={u['record_id']}: {e}")

    log.info(f"Successfully updated {updated_count}/{len(updates)} records")


def main():
    parser = argparse.ArgumentParser(
        description="Update max_shift_hours from Driver Hours Excel data"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print proposed values without updating the database"
    )
    parser.add_argument(
        "--excel", default="Data/Driver_Hours_2025-06-2026-05.xlsx",
        help="Path to Driver Hours Excel file"
    )
    args = parser.parse_args()

    if not os.path.exists(args.excel):
        log.error(f"Excel file not found: {args.excel}")
        sys.exit(1)

    driver_hours = load_driver_hours(args.excel)

    if args.dry_run:
        run_dry(driver_hours)
    else:
        run_update(driver_hours)


if __name__ == "__main__":
    main()
