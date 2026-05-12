"""
AutoRouting Data Sync Script
Reads Excel files (refreshed from ODBC) and pushes to Supabase every 5 minutes.
Run: python sync.py
     python sync.py --once   (single run, no loop)
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
EXCEL_DIR = Path(os.getenv("EXCEL_DIR", "."))
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))  # 5 min default

EXCEL_FILES = {
    "yard_locations": "Yard_Locations.xlsx",
    "terminal_locations": "terminal_locations.xlsx",
    "site_details": "site_details.xlsx",
    "driver_schedules": "Auto_Routing_Drivers_Schedule.xlsx",
    "driver_terminal_cards": "Driver_terminal_cards.xlsx",
    "load_details": "load_details.xlsx",
}


def get_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ---------- transformers per table ----------

def transform_yard_locations(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df = df.rename(columns={"yard_address": "yard_address"})
    df["zip"] = df["zip"].astype(str).str.strip()
    return df.where(pd.notnull(df), None).to_dict("records")


def transform_terminal_locations(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df = df.rename(columns={"terminal_abreviation": "terminal_abbreviation"})
    df["terminal_id"] = pd.to_numeric(df["terminal_id"], errors="coerce")
    df = df.dropna(subset=["terminal_id"])
    df["terminal_id"] = df["terminal_id"].astype(int)
    if "is_diesel_wet" not in df.columns:
        df["is_diesel_wet"] = 0
    return df.where(pd.notnull(df), None).to_dict("records")


def transform_site_details(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df["site_id"] = pd.to_numeric(df["site_id"], errors="coerce")
    df = df.dropna(subset=["site_id"])
    df["site_id"] = df["site_id"].astype(int)
    df["zip"] = df["zip"].astype(str).str.strip()
    df["pump_certified"] = pd.to_numeric(df["pump_certified"], errors="coerce").fillna(0).astype(int)
    return df.where(pd.notnull(df), None).to_dict("records")


def transform_driver_schedules(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df["record_id"] = pd.to_numeric(df["record_id"], errors="coerce")
    df["driver_id"] = pd.to_numeric(df["driver_id"], errors="coerce")
    df = df.dropna(subset=["record_id"])
    df["record_id"] = df["record_id"].astype(int)
    df["driver_id"] = df["driver_id"].fillna(0).astype(int)

    if "shift_date" in df.columns:
        df["shift_date"] = pd.to_datetime(df["shift_date"], errors="coerce")
        df["shift_date"] = df["shift_date"].dt.strftime("%Y-%m-%d")

    if "driver_start_time" in df.columns:
        df["driver_start_time"] = df["driver_start_time"].astype(str).str.strip()
        df["driver_start_time"] = df["driver_start_time"].apply(
            lambda x: x if ":" in str(x) else None
        )

    for col in ["driver_schedule", "attendance_expected", "pump_trained"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    if "max_shift_hours" not in df.columns:
        df["max_shift_hours"] = 12.0

    return df.where(pd.notnull(df), None).to_dict("records")


def transform_driver_terminal_cards(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df["driver_id"] = pd.to_numeric(df["driver_id"], errors="coerce")
    df["terminal_id"] = pd.to_numeric(df["terminal_id"], errors="coerce")
    df = df.dropna(subset=["driver_id", "terminal_id"])
    df["driver_id"] = df["driver_id"].astype(int)
    df["terminal_id"] = df["terminal_id"].astype(int)
    return df.where(pd.notnull(df), None).to_dict("records")


def _parse_excel_datetime(series: pd.Series) -> pd.Series:
    """Handle Excel serial date numbers or string datetimes."""
    def _convert(v):
        if pd.isna(v):
            return None
        if isinstance(v, (int, float)):
            try:
                base = datetime(1899, 12, 30)
                dt = base + timedelta(days=float(v))
                return dt.isoformat()
            except Exception:
                return None
        try:
            return pd.to_datetime(v).isoformat()
        except Exception:
            return None
    return series.apply(_convert)


def transform_load_details(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df["ce_id"] = pd.to_numeric(df["ce_id"], errors="coerce")
    df = df.dropna(subset=["ce_id"])
    df["ce_id"] = df["ce_id"].astype(int)

    for dt_col in ["window_start", "window_end", "delivery_eta", "arrived_at_rack",
                   "left_rack", "arrived_at_site"]:
        if dt_col in df.columns:
            df[dt_col] = _parse_excel_datetime(df[dt_col])

    if "delivery_date" in df.columns:
        df["delivery_date"] = _parse_excel_datetime(df["delivery_date"])
        df["delivery_date"] = df["delivery_date"].apply(
            lambda x: x[:10] if x else None
        )

    if "load_status" in df.columns:
        df["load_status"] = pd.to_numeric(df["load_status"], errors="coerce").astype("Int64")

    # Derive is_anytime: window_start 00:00 and window_end 23:00
    if "window_start" in df.columns and "window_end" in df.columns:
        def is_anytime(row):
            ws = str(row.get("window_start") or "")
            we = str(row.get("window_end") or "")
            return 1 if ("T00:00" in ws and "T23:00" in we) else 0
        df["is_anytime"] = df.apply(is_anytime, axis=1)

    return df.where(pd.notnull(df), None).to_dict("records")


TRANSFORMERS = {
    "yard_locations": transform_yard_locations,
    "terminal_locations": transform_terminal_locations,
    "site_details": transform_site_details,
    "driver_schedules": transform_driver_schedules,
    "driver_terminal_cards": transform_driver_terminal_cards,
    "load_details": transform_load_details,
}


# ---------- sync logic ----------

def sync_table(client: Client, table: str, filename: str) -> dict:
    start = time.time()
    filepath = EXCEL_DIR / filename

    if not filepath.exists():
        log.warning(f"File not found: {filepath}")
        return {"table": table, "status": "skipped", "reason": "file not found"}

    try:
        df = pd.read_excel(filepath)
        log.info(f"Read {len(df)} rows from {filename}")
    except Exception as e:
        log.error(f"Failed to read {filename}: {e}")
        return {"table": table, "status": "error", "error": str(e)}

    transform_fn = TRANSFORMERS.get(table)
    if not transform_fn:
        return {"table": table, "status": "error", "error": "No transformer defined"}

    try:
        records = transform_fn(df)
    except Exception as e:
        log.error(f"Transform failed for {table}: {e}")
        return {"table": table, "status": "error", "error": str(e)}

    if not records:
        return {"table": table, "status": "ok", "rows_upserted": 0}

    try:
        # Upsert in chunks to avoid request size limits
        chunk_size = 500
        total_upserted = 0
        for i in range(0, len(records), chunk_size):
            chunk = records[i : i + chunk_size]
            client.table(table).upsert(chunk).execute()
            total_upserted += len(chunk)

        duration_ms = int((time.time() - start) * 1000)
        log.info(f"✓ {table}: {total_upserted} rows upserted in {duration_ms}ms")

        # Log to sync_log table
        client.table("sync_log").insert({
            "table_name": table,
            "rows_upserted": total_upserted,
            "rows_deleted": 0,
            "status": "success",
            "duration_ms": duration_ms,
        }).execute()

        return {"table": table, "status": "ok", "rows_upserted": total_upserted}

    except Exception as e:
        log.error(f"Upsert failed for {table}: {e}")
        try:
            client.table("sync_log").insert({
                "table_name": table,
                "status": "error",
                "error_message": str(e)[:500],
            }).execute()
        except Exception:
            pass
        return {"table": table, "status": "error", "error": str(e)}


def run_sync():
    log.info("=" * 50)
    log.info(f"Starting sync at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    client = get_client()
    results = []
    for table, filename in EXCEL_FILES.items():
        result = sync_table(client, table, filename)
        results.append(result)
    errors = [r for r in results if r.get("status") == "error"]
    if errors:
        log.warning(f"Sync complete with {len(errors)} error(s)")
    else:
        log.info("Sync complete — all tables OK")
    return results


def main():
    parser = argparse.ArgumentParser(description="AutoRouting Excel → Supabase Sync")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=SYNC_INTERVAL_SECONDS,
                        help="Sync interval in seconds (default: 300)")
    args = parser.parse_args()

    if args.once:
        run_sync()
        return

    log.info(f"Starting scheduled sync every {args.interval}s. Press Ctrl+C to stop.")
    while True:
        try:
            run_sync()
        except KeyboardInterrupt:
            log.info("Sync stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error in sync loop: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
