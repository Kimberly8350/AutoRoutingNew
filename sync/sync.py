"""
AutoRouting Data Sync Script
- load_details: queried directly from CE Connect MySQL (vw_undelivered_loads)
- all other tables: read from local Excel files
Run: python sync.py
     python sync.py --once   (single run, no loop)
"""

import os
import sys
import time
import shutil
import tempfile
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pymysql
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

# CE Connect MySQL connection (load_details only)
CE_CONNECT_HOST = os.getenv("CE_CONNECT_HOST", "")
CE_CONNECT_PORT = int(os.getenv("CE_CONNECT_PORT", "3306"))
CE_CONNECT_USER = os.getenv("CE_CONNECT_USER", "")
CE_CONNECT_PASSWORD = os.getenv("CE_CONNECT_PASSWORD", "")
CE_CONNECT_DATABASE = os.getenv("CE_CONNECT_DATABASE", "")

# load_details is now sourced from MySQL — not Excel
EXCEL_FILES = {
    "yard_locations": "Yard_Locations.xlsx",
    "terminal_locations": "terminal_locations.xlsx",
    "site_details": "site_details.xlsx",
    "driver_schedules": "Auto_Routing_Driver_Schedule.xlsx",
    "driver_terminal_cards": "Driver_terminal_cards.xlsx",
}

SHEET_NAMES: dict[str, str] = {}  # no multi-sheet Excel files remain


def get_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def fetch_loads_from_mysql() -> pd.DataFrame:
    """Query vw_undelivered_loads from CE Connect and return a DataFrame."""
    conn = pymysql.connect(
        host=CE_CONNECT_HOST,
        port=CE_CONNECT_PORT,
        user=CE_CONNECT_USER,
        password=CE_CONNECT_PASSWORD,
        database=CE_CONNECT_DATABASE,
        connect_timeout=15,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        # Reconstruct load feed from base tables — equivalent to vw_undelivered_loads.
        # Excludes deleted (status=0) and limits to last 60 days.
        query = """
            SELECT
                od.drop_id                                                              AS CE_ID,
                DATE(od.drop_schedule_date)                                             AS Delivery_Date,
                cust.customer_name                                                      AS Customer_Name,
                od.drop_order_number                                                    AS Order_Number,
                od.location_id                                                          AS Site_ID,
                term.terminal_id                                                        AS Terminal_ID,
                term.terminal_name                                                      AS Terminal_Name,
                prod.product_name                                                       AS Product_Name,
                COALESCE(NULLIF(odd.drop_detail_gross_gallons, 0), odd.drop_detail_ordered) AS Gallons_Ordered,
                dest.destination_name                                                   AS Destination_Name,
                dest.destination_address                                                AS Address,
                dest.destination_city                                                   AS City,
                dest.destination_state                                                  AS State,
                drv.driver_first_name                                                   AS First_Name,
                drv.driver_last_name                                                    AS Last_Name,
                od.drop_start_window                                                    AS Window_Start,
                od.drop_end_window                                                      AS Window_End,
                od.drop_eta_time                                                        AS Delivery_ETA,
                od.drop_arrived_at_rack_time                                            AS Arrived_At_Rack_Time,
                od.drop_finalized_delivery_time                                         AS Completed_Delivery_Time,
                od.drop_status_id                                                       AS Load_Status,
                od.drop_is_split                                                        AS Split,
                od.drop_split_with_id                                                   AS Split_With_CE_ID
            FROM dlb_order_drops od
            JOIN  dlb_order_drop_details odd ON odd.drop_id      = od.drop_id
            LEFT JOIN dl_destinations    dest ON dest.destination_id = od.location_id
            LEFT JOIN dl_customers       cust ON cust.customer_id    = od.customer_id
            LEFT JOIN dl_products        prod ON prod.product_id     = odd.product_id
            LEFT JOIN dl_cards           card ON card.card_id        = odd.card_id
            LEFT JOIN dl_terminals       term ON term.terminal_id    = card.terminal_id
            LEFT JOIN dlb_routes         rt   ON rt.route_id         = od.route_id
            LEFT JOIN dl_drivers         drv  ON drv.driver_id       = rt.driver_id
            WHERE od.drop_schedule_date >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
              AND od.drop_status_id != 0
              AND dest.destination_name IS NOT NULL
              AND od.location_id IS NOT NULL
        """
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
        return pd.DataFrame(rows)
    finally:
        conn.close()


# ---------- transformers per table ----------

def transform_yard_locations(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df = df.rename(columns={"yard_address": "yard_address"})
    df["zip"] = df["zip"].astype(str).str.strip()
    return df.where(pd.notnull(df), None).to_dict("records")


def transform_terminal_locations(df: pd.DataFrame) -> list[dict]:
    df.columns = [c.lower().strip() for c in df.columns]
    df = df.rename(columns={"terminal_abreviation": "terminal_abbreviation"})
    # terminal_id is the ODBC string code (e.g. "T-01-TX-0001"), not numeric — keep as-is
    df["terminal_id"] = df["terminal_id"].astype(str).str.strip()
    df = df[df["terminal_id"].str.len() > 0]
    if "is_diesel_wet" not in df.columns:
        df["is_diesel_wet"] = 0
    # Deduplicate within the batch to prevent ON CONFLICT errors — terminal_id
    # is the upsert key, and Postgres can't apply ON CONFLICT twice for the
    # same key within one statement.
    df = df.drop_duplicates(subset=["terminal_id"], keep="last")
    return df.where(pd.notnull(df), None).to_dict("records")


def transform_site_details(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df.columns = [c.lower().strip() for c in df.columns]
    df["site_id"] = pd.to_numeric(df["site_id"], errors="coerce")
    df = df.dropna(subset=["site_id"])
    df["site_id"] = df["site_id"].astype(int)
    # Drop rows missing site_name (NOT NULL in DB)
    df = df.dropna(subset=["site_name"])
    df["zip"] = df["zip"].astype(str).str.strip()
    # Clamp pump_certified to 0 or 1 — any value outside that range maps to 0
    df["pump_certified"] = pd.to_numeric(df["pump_certified"], errors="coerce").fillna(0).astype(int)
    df["pump_certified"] = df["pump_certified"].apply(lambda x: 1 if x == 1 else 0)
    # Replace all NaN/NA/inf with None to avoid JSON serialization errors
    df = df.replace([float("nan"), float("inf"), float("-inf")], None)
    records = df.to_dict("records")
    return [{k: (None if (v != v) else v) for k, v in r.items()} for r in records]


def transform_driver_schedules(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df.columns = [c.lower().strip() for c in df.columns]

    # Normalize CamelCase column names from the live ODBC Excel file to snake_case DB names
    df = df.rename(columns={
        "recordid": "record_id",
        "driverid": "driver_id",
        "shiftdate": "shift_date",
    })

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

    # Derive board_location from division_prefix + default_shift_name (e.g. "TX" + "AM" → "TX-AM")
    VALID_BOARD_LOCS = {"TX-AM", "TX-PM", "FW-AM", "FW-PM", "ET-AM"}
    if "division_prefix" in df.columns and "default_shift_name" in df.columns:
        df["board_location"] = (
            df["division_prefix"].fillna("").str.strip().str.upper()
            + "-"
            + df["default_shift_name"].fillna("").str.strip().str.upper()
        )
        df["board_location"] = df["board_location"].apply(
            lambda x: x if x in VALID_BOARD_LOCS else None
        )

    # Only keep columns that exist in the DB schema — drop extra ODBC columns
    DB_COLS = {
        "record_id", "driver_id", "first_name", "last_name", "driver_start_time",
        "division_prefix", "default_shift_name", "board_location", "yard",
        "shift_date", "driver_schedule", "attendance_expected", "pump_trained",
        "max_shift_hours",
    }
    df = df[[c for c in df.columns if c in DB_COLS]]

    return df.where(pd.notnull(df), None).to_dict("records")


def transform_driver_terminal_cards(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df.columns = [c.lower().strip() for c in df.columns]
    df["driver_id"] = pd.to_numeric(df["driver_id"], errors="coerce")
    df = df.dropna(subset=["driver_id"])
    df["driver_id"] = df["driver_id"].astype(int)
    # terminal_id is the ODBC string code (e.g. "T-01-TX-0001"), not numeric — keep as-is
    df["terminal_id"] = df["terminal_id"].astype(str).str.strip()
    df = df[df["terminal_id"].str.len() > 0]
    # Deduplicate within the batch to prevent ON CONFLICT errors
    df = df.drop_duplicates(subset=["driver_id", "terminal_id"], keep="last")
    df = df.replace([float("nan"), float("inf"), float("-inf")], None)
    records = df.to_dict("records")
    return [{k: (None if (v != v) else v) for k, v in r.items()} for r in records]


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
    df = df.copy()
    df.columns = [c.lower().strip() for c in df.columns]

    # Normalize vw_undelivered_loads column names to the DB schema.
    df = df.rename(columns={
        "gallons_ordered":      "gross_gallons",   # Gallons_Ordered  → gross_gallons
        "destination_name":     "site_name",       # Destination_Name → site_name
        "address":              "site_address",    # Address          → site_address
        "arrived_at_rack_time": "arrived_at_rack", # Arrived_At_Rack_Time → arrived_at_rack
    })

    # Status 0 = deleted loads — exclude entirely, do not upsert to DB.
    if "load_status" in df.columns:
        before = len(df)
        df = df[pd.to_numeric(df["load_status"], errors="coerce").fillna(-1) != 0].copy()
        removed = before - len(df)
        if removed:
            import logging as _log
            _log.getLogger(__name__).info(f"load_details: dropped {removed} deleted (status=0) rows")

    df["ce_id"] = pd.to_numeric(df["ce_id"], errors="coerce")
    df = df.dropna(subset=["ce_id"])
    df["ce_id"] = df["ce_id"].astype(int)
    # Coerce integer FK columns
    for int_col in ["site_id", "load_status"]:
        if int_col in df.columns:
            df[int_col] = pd.to_numeric(df[int_col], errors="coerce")
            # Convert valid floats (e.g. 2.0) to int, leave NaN as None
            df[int_col] = df[int_col].apply(
                lambda x: int(x) if pd.notna(x) else None
            )

    if "split" in df.columns:
        df["split"] = pd.to_numeric(df["split"], errors="coerce").fillna(0).astype(int)

    if "split_with_ce_id" in df.columns:
        df["split_with_ce_id"] = pd.to_numeric(df["split_with_ce_id"], errors="coerce")
        # 0 means "not paired yet" in CE Connect, not a real ce_id reference
        df["split_with_ce_id"] = df["split_with_ce_id"].apply(
            lambda x: int(x) if pd.notna(x) and int(x) != 0 else None
        )

    # terminal_id is the ODBC string code (e.g. "T-75-TX-2664"), not numeric — keep as-is
    if "terminal_id" in df.columns:
        df["terminal_id"] = df["terminal_id"].apply(
            lambda x: str(x).strip() if pd.notna(x) and str(x).strip().lower() != "nan" else None
        )

    for dt_col in ["window_start", "window_end", "delivery_eta", "arrived_at_rack",
                   "left_rack", "arrived_at_site", "completed_delivery_time"]:
        if dt_col in df.columns:
            df[dt_col] = _parse_excel_datetime(df[dt_col])

    if "delivery_date" in df.columns:
        df["delivery_date"] = _parse_excel_datetime(df["delivery_date"])
        df["delivery_date"] = df["delivery_date"].apply(
            lambda x: x[:10] if x else None
        )

    if "load_status" in df.columns:
        df["load_status"] = pd.to_numeric(df["load_status"], errors="coerce").apply(
            lambda x: int(x) if pd.notna(x) else None
        )

    # Drop is_anytime — it's derived on read, not stored as a column in the DB
    if "is_anytime" in df.columns:
        df = df.drop(columns=["is_anytime"])

    # Only keep columns that exist in the DB schema — drop extras like customer_id
    DB_COLS = {
        "ce_id", "delivery_date", "customer_name", "order_number", "site_id",
        "terminal_id", "terminal_name", "product_name", "gross_gallons",
        "load_status_description", "city", "state", "site_name", "site_address",
        "first_name", "last_name", "window_start", "window_end", "delivery_eta",
        "load_status", "arrived_at_rack", "left_rack", "arrived_at_site",
        "completed_delivery_time", "split", "split_with_ce_id",
    }
    df = df[[c for c in df.columns if c in DB_COLS]]

    # Deduplicate on PK to avoid ON CONFLICT batch errors
    df = df.drop_duplicates(subset=["ce_id", "product_name"], keep="last")

    df = df.replace([float("nan"), float("inf"), float("-inf")], None)
    records = df.to_dict("records")

    # Final pass: force integer types for all known DB integer columns
    # (guards against pandas CoW keeping float dtype on object columns)
    LOAD_INT_COLS = {"ce_id", "site_id", "load_status", "split", "split_with_ce_id"}
    cleaned = []
    for r in records:
        row = {}
        for k, v in r.items():
            if v != v or v is None:  # NaN or None → None
                row[k] = None
            elif k in LOAD_INT_COLS and isinstance(v, float):
                row[k] = int(v)
            else:
                row[k] = v
        cleaned.append(row)
    return cleaned


TRANSFORMERS = {
    "yard_locations": transform_yard_locations,
    "terminal_locations": transform_terminal_locations,
    "site_details": transform_site_details,
    "driver_schedules": transform_driver_schedules,
    "driver_terminal_cards": transform_driver_terminal_cards,
}


# ---------- sync logic ----------

def sync_load_details_from_mysql(client: Client) -> dict:
    """Fetch load_details from CE Connect MySQL and upsert into Supabase."""
    start = time.time()

    if not all([CE_CONNECT_HOST, CE_CONNECT_USER, CE_CONNECT_PASSWORD, CE_CONNECT_DATABASE]):
        log.warning("CE Connect MySQL not configured — skipping load_details sync. "
                    "Set CE_CONNECT_HOST/USER/PASSWORD/DATABASE in .env")
        return {"table": "load_details", "status": "skipped", "reason": "MySQL not configured"}

    try:
        df = fetch_loads_from_mysql()
        log.info(f"Fetched {len(df)} rows from vw_undelivered_loads")
    except Exception as e:
        log.error(f"MySQL fetch failed: {e}")
        try:
            client.table("sync_log").insert({
                "table_name": "load_details",
                "status": "error",
                "error_message": str(e)[:500],
            }).execute()
        except Exception:
            pass
        return {"table": "load_details", "status": "error", "error": str(e)}

    try:
        records = transform_load_details(df)
    except Exception as e:
        log.error(f"Transform failed for load_details: {e}")
        return {"table": "load_details", "status": "error", "error": str(e)}

    if not records:
        return {"table": "load_details", "status": "ok", "rows_upserted": 0}

    try:
        # Skip rows already locked at status=1 (ready-to-route in Supabase)
        locked_rows = (
            client.table("load_details")
            .select("ce_id,product_name")
            .eq("load_status", 1)
            .execute()
            .data
        )
        locked_keys = {(r["ce_id"], r["product_name"]) for r in locked_rows}
        if locked_keys:
            before = len(records)
            records = [
                r for r in records
                if (r.get("ce_id"), r.get("product_name")) not in locked_keys
            ]
            log.info(f"load_details: skipping {before - len(records)} locked (status=1) rows")

        chunk_size = 500
        total_upserted = 0
        for i in range(0, len(records), chunk_size):
            chunk = records[i : i + chunk_size]
            client.table("load_details").upsert(chunk, on_conflict="ce_id,product_name").execute()
            total_upserted += len(chunk)

        duration_ms = int((time.time() - start) * 1000)
        log.info(f"✓ load_details: {total_upserted} rows upserted in {duration_ms}ms")

        client.table("sync_log").insert({
            "table_name": "load_details",
            "rows_upserted": total_upserted,
            "rows_deleted": 0,
            "status": "success",
            "duration_ms": duration_ms,
        }).execute()

        return {"table": "load_details", "status": "ok", "rows_upserted": total_upserted}

    except Exception as e:
        log.error(f"Upsert failed for load_details: {e}")
        try:
            client.table("sync_log").insert({
                "table_name": "load_details",
                "status": "error",
                "error_message": str(e)[:500],
            }).execute()
        except Exception:
            pass
        return {"table": "load_details", "status": "error", "error": str(e)}


def sync_driver_exceptions_from_mysql(client: Client) -> dict:
    """Pull driver OUT exceptions from CE Connect and set attendance_expected=0 in Supabase."""
    start = time.time()

    if not all([CE_CONNECT_HOST, CE_CONNECT_USER, CE_CONNECT_PASSWORD, CE_CONNECT_DATABASE]):
        return {"table": "driver_exceptions", "status": "skipped", "reason": "MySQL not configured"}

    try:
        conn = pymysql.connect(
            host=CE_CONNECT_HOST, port=CE_CONNECT_PORT,
            user=CE_CONNECT_USER, password=CE_CONNECT_PASSWORD,
            database=CE_CONNECT_DATABASE, connect_timeout=15,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            # Pull OUT exceptions from both CE Connect exception tables.
            # dlb_driver_future_schedule_exceptions: current/future exceptions with is_working flag
            # tbldriverscheduleexceptions: legacy table, Status='OUT' means not working
            query = """
                SELECT driver_id, DATE(exception_date) AS exc_date
                FROM dlb_driver_future_schedule_exceptions
                WHERE (is_working = 0 OR exception_status = 'OUT')
                  AND exception_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                  AND exception_date <= DATE_ADD(CURDATE(), INTERVAL 60 DAY)

                UNION

                SELECT DriverID AS driver_id, DATE(ExceptionDate) AS exc_date
                FROM tbldriverscheduleexceptions
                WHERE Status = 'OUT'
                  AND ExceptionDate >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                  AND ExceptionDate <= DATE_ADD(CURDATE(), INTERVAL 60 DAY)
            """
            with conn.cursor() as cur:
                cur.execute(query)
                exceptions = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"MySQL driver exception fetch failed: {e}")
        return {"table": "driver_exceptions", "status": "error", "error": str(e)}

    if not exceptions:
        return {"table": "driver_exceptions", "status": "ok", "rows_updated": 0}

    updated = 0
    for exc in exceptions:
        driver_id = exc.get("driver_id")
        exc_date = exc.get("exc_date")
        if not driver_id or not exc_date:
            continue
        exc_date_str = exc_date.isoformat() if hasattr(exc_date, "isoformat") else str(exc_date)
        try:
            client.table("driver_schedules") \
                .update({"attendance_expected": 0}) \
                .eq("driver_id", driver_id) \
                .eq("shift_date", exc_date_str) \
                .execute()
            updated += 1
        except Exception as e:
            log.warning(f"driver_exceptions: could not update driver {driver_id} on {exc_date_str}: {e}")

    duration_ms = int((time.time() - start) * 1000)
    log.info(f"✓ driver_exceptions: {updated} attendance rows cleared in {duration_ms}ms")
    return {"table": "driver_exceptions", "status": "ok", "rows_updated": updated}


def sync_driver_clock_events_from_mysql(client: Client) -> dict:
    """Sync driver clock-in/out times from vw_driver_details_feed into driver_clock_events."""
    start = time.time()

    if not all([CE_CONNECT_HOST, CE_CONNECT_USER, CE_CONNECT_PASSWORD, CE_CONNECT_DATABASE]):
        return {"table": "driver_clock_events", "status": "skipped", "reason": "MySQL not configured"}

    try:
        conn = pymysql.connect(
            host=CE_CONNECT_HOST, port=CE_CONNECT_PORT,
            user=CE_CONNECT_USER, password=CE_CONNECT_PASSWORD,
            database=CE_CONNECT_DATABASE, connect_timeout=15,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            # Query dlb_routes directly — QWReporting has access to this table
            # (same source used in the loads query). shift_date is the board date
            # derived from route_start_time so overnight drivers map to their start day.
            query = """
                SELECT
                    driver_id,
                    DATE(route_start_time)   AS shift_date,
                    route_start_time,
                    route_finish_time
                FROM dlb_routes
                WHERE route_start_time >= DATE_SUB(CURDATE(), INTERVAL 2 DAY)
                  AND driver_id IS NOT NULL
            """
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"MySQL clock events fetch failed: {e}")
        return {"table": "driver_clock_events", "status": "error", "error": str(e)}

    if not rows:
        return {"table": "driver_clock_events", "status": "ok", "rows_upserted": 0}

    # A driver can have more than one dlb_routes record on the same shift_date
    # (multiple trips/segments in one day). The upsert conflict target is
    # (driver_id, shift_date), and Postgres can't apply ON CONFLICT DO UPDATE
    # twice for the same key within one statement — so duplicates must be
    # collapsed here first. Clock-in = earliest start, clock-out = latest
    # finish across all of that driver's segments that day.
    merged: dict[tuple, dict] = {}
    for r in rows:
        driver_id = r.get("driver_id")
        shift_date = r.get("shift_date")
        if not driver_id or not shift_date:
            continue
        route_start = r.get("route_start_time")
        route_finish = r.get("route_finish_time")
        key = (int(driver_id), shift_date)
        entry = merged.setdefault(key, {"start": route_start, "finish": route_finish})
        if route_start and (entry["start"] is None or route_start < entry["start"]):
            entry["start"] = route_start
        if route_finish and (entry["finish"] is None or route_finish > entry["finish"]):
            entry["finish"] = route_finish

    records = []
    for (driver_id, shift_date), entry in merged.items():
        route_start = entry["start"]
        route_finish = entry["finish"]
        records.append({
            "driver_id": driver_id,
            "shift_date": shift_date.isoformat() if hasattr(shift_date, "isoformat") else str(shift_date),
            "route_start_time": route_start.isoformat() if hasattr(route_start, "isoformat") else (str(route_start) if route_start else None),
            "route_finish_time": route_finish.isoformat() if hasattr(route_finish, "isoformat") else (str(route_finish) if route_finish else None),
            "synced_at": datetime.now().isoformat(),
        })

    if not records:
        return {"table": "driver_clock_events", "status": "ok", "rows_upserted": 0}

    try:
        for i in range(0, len(records), 500):
            chunk = records[i:i + 500]
            client.table("driver_clock_events").upsert(
                chunk, on_conflict="driver_id,shift_date"
            ).execute()

        duration_ms = int((time.time() - start) * 1000)
        log.info(f"✓ driver_clock_events: {len(records)} rows upserted in {duration_ms}ms")
        return {"table": "driver_clock_events", "status": "ok", "rows_upserted": len(records)}

    except Exception as e:
        log.error(f"Upsert failed for driver_clock_events: {e}")
        return {"table": "driver_clock_events", "status": "error", "error": str(e)}


def sync_table(client: Client, table: str, filename: str) -> dict:
    start = time.time()
    filepath = EXCEL_DIR / filename

    if not filepath.exists():
        log.warning(f"File not found: {filepath}")
        return {"table": table, "status": "skipped", "reason": "file not found"}

    # Some Excel files are kept open by ODBC connections (permission denied on direct read).
    # Copy to a temp file first so we can always read the latest refreshed data.
    tmp_path = None
    read_path = filepath
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", prefix=f"sync_{table}_")
        os.close(tmp_fd)
        shutil.copy2(filepath, tmp_path)
        read_path = Path(tmp_path)
        log.debug(f"Copied {filename} → temp file for reading")
    except Exception as e:
        log.warning(f"Could not copy {filename} to temp ({e}); reading directly")
        read_path = filepath
        tmp_path = None

    try:
        sheet = SHEET_NAMES.get(table, 0)  # default 0 = first sheet
        df = pd.read_excel(read_path, sheet_name=sheet)
        log.info(f"Read {len(df)} rows from {filename}" + (f" (sheet: {sheet!r})" if sheet != 0 else ""))

    except Exception as e:
        log.error(f"Failed to read {filename}: {e}")
        return {"table": table, "status": "error", "error": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

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

    # Tables with composite unique keys need explicit conflict targets
    ON_CONFLICT = {
        "driver_terminal_cards": "driver_id,terminal_id",
        "load_details": "ce_id,product_name",
    }

    # Preserve dispatcher-set attendance_expected — don't let the Excel overwrite manual toggles.
    # New rows (first appearance for a shift_date) get the Excel value; existing rows keep Supabase value.
    if table == "driver_schedules":
        try:
            existing = (
                client.table("driver_schedules")
                .select("record_id,attendance_expected")
                .execute()
                .data
            )
            existing_att = {
                r["record_id"]: r["attendance_expected"]
                for r in existing
                if r.get("record_id") is not None
            }
            for r in records:
                rid = r.get("record_id")
                if rid in existing_att and existing_att[rid] is not None:
                    r["attendance_expected"] = existing_att[rid]
        except Exception as e:
            log.warning(f"driver_schedules: could not fetch existing attendance values — sync will overwrite: {e}")

    try:
        # Upsert in chunks to avoid request size limits
        chunk_size = 500
        total_upserted = 0
        conflict_col = ON_CONFLICT.get(table)
        for i in range(0, len(records), chunk_size):
            chunk = records[i : i + chunk_size]
            q = client.table(table).upsert(chunk, on_conflict=conflict_col) if conflict_col else client.table(table).upsert(chunk)
            q.execute()
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
    # load_details comes from CE Connect MySQL, not Excel
    results.append(sync_load_details_from_mysql(client))
    # Exceptions override attendance — must run after driver_schedules Excel sync
    results.append(sync_driver_exceptions_from_mysql(client))
    # Driver clock-in/out times from vw_driver_details_feed
    results.append(sync_driver_clock_events_from_mysql(client))
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
