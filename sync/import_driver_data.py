"""
One-time import script: loads driver data and terminal card access from CSV/Excel into Supabase.

Files read:
  - Driver_terminal_cards.csv   → driver_terminal_cards table (primary source)
  - Active_Drivers_Data.xlsx    → validates active drivers

Run from the sync/ directory:
    python import_driver_data.py
"""

import os
import csv
import shutil
import tempfile
import logging
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client
import openpyxl

# Load .env from this script's directory
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
EXCEL_DIR = "C:/Users/kimbe/OneDrive - Quikway Group/RP Project"

# Primary source CSV for driver terminal cards
CSV_SOURCE = r"C:\Users\kimbe\Desktop\AutoRouting\VS 2.1\Driver_terminal_cards.csv"

client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def read_excel(filename, sheet_name):
    src = os.path.join(EXCEL_DIR, filename)
    tmp = tempfile.mktemp(suffix=".xlsx")
    shutil.copy2(src, tmp)
    try:
        wb = openpyxl.load_workbook(tmp, data_only=True)
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        headers = [str(h).strip().lower() if h else "" for h in rows[0]]
        data = []
        for row in rows[1:]:
            if all(v is None for v in row):
                continue
            data.append(dict(zip(headers, row)))
        log.info(f"Read {len(data)} rows from {filename} / {sheet_name}")
        return data
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def import_terminal_cards():
    """
    Import Driver_terminal_cards.csv → driver_terminal_cards table.
    CSV columns: driver_id, last_name, first_name, terminal_name, terminal_id (ODBC string)
    terminal_locations.terminal_id is also an ODBC string, so we use it directly.
    Also validates that each terminal_id exists in terminal_locations.
    """
    log.info("=== Importing driver terminal cards from CSV ===")
    log.info(f"Source: {CSV_SOURCE}")

    # Build set of known terminal_ids from Supabase (ODBC strings)
    term_rows = client.table("terminal_locations").select("terminal_id,terminal_name").execute().data
    known_terminals = {r["terminal_id"].strip(): r["terminal_name"] for r in term_rows if r.get("terminal_id")}
    log.info(f"Loaded {len(known_terminals)} terminals from Supabase")

    # Read CSV
    cards = []
    with open(CSV_SOURCE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cards.append(row)
    log.info(f"Read {len(cards)} rows from CSV")

    rows_to_upsert = []
    skipped = []
    missing_terminals = set()

    for row in cards:
        driver_id = row.get("driver_id", "").strip()
        terminal_id = (row.get("terminal_id") or "").strip()
        terminal_name = (row.get("terminal_name") or "").strip()

        if not driver_id:
            log.warning(f"Skipping row — blank driver_id, terminal_name='{terminal_name}'")
            skipped.append(row)
            continue

        if not terminal_id:
            log.warning(f"Skipping row — blank terminal_id for driver_id={driver_id}, terminal_name='{terminal_name}'")
            skipped.append(row)
            continue

        if terminal_id not in known_terminals:
            missing_terminals.add(f"{terminal_id} ({terminal_name})")
            skipped.append(row)
            continue

        rows_to_upsert.append({
            "driver_id": int(driver_id),
            "terminal_id": terminal_id,
        })

    if missing_terminals:
        log.warning(f"Terminal IDs NOT found in Supabase ({len(missing_terminals)}): {sorted(missing_terminals)}")

    # Deduplicate
    seen = set()
    unique_rows = []
    for r in rows_to_upsert:
        key = (r["driver_id"], r["terminal_id"])
        if key not in seen:
            seen.add(key)
            unique_rows.append(r)

    log.info(f"Ready to upsert {len(unique_rows)} unique driver-terminal pairs ({len(skipped)} rows skipped)")

    # Clear existing and replace — ensures removed cards are cleaned up
    client.table("driver_terminal_cards").delete().gt("driver_id", 0).execute()
    log.info("Cleared existing driver_terminal_cards")

    for i in range(0, len(unique_rows), 100):
        client.table("driver_terminal_cards").insert(unique_rows[i:i+100]).execute()

    log.info(f"driver_terminal_cards import complete — {len(unique_rows)} rows inserted")


def import_active_drivers():
    """
    Import Active_Drivers_Data.xlsx → updates driver info in Supabase.
    This table is a reference — not currently written to a dedicated table
    but logged here for visibility.
    """
    log.info("=== Reading Active_Drivers_Data.xlsx ===")
    drivers = read_excel("Active_Drivers_Data.xlsx", "active drivers")

    active = [d for d in drivers if d.get("active_driver") == 1]
    inactive = [d for d in drivers if d.get("active_driver") != 1]
    log.info(f"Active drivers: {len(active)}, Inactive: {len(inactive)}")

    for d in active:
        log.info(f"  Active: {d.get('driver_id')} — {d.get('first_name')} {d.get('last_name')} ({d.get('yard')})")


if __name__ == "__main__":
    import_terminal_cards()
    import_active_drivers()
    log.info("=== Import complete ===")
