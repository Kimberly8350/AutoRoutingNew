"""
Out-of-Time Backtest: Compare v1 vs v2 routing engine against actuals.

Runs both engine versions on historical data from Supabase for a given date,
then compares the planned assignments against actual deliveries from the CSV.

Usage:
    python scripts/backtest.py --date 2026-06-23
    python scripts/backtest.py --date 2026-06-23 --no-api
    python scripts/backtest.py --all-dates --no-api

Requires: supabase, python-dotenv
Run from: backend/ directory
"""

import sys
import os
import csv
import logging
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta, time
from collections import defaultdict
from copy import deepcopy

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from supabase import create_client
from models.models import (
    Driver, Load, Site, Terminal, Yard, DriverRoute, RouteStop,
    DispatchResult, LoadProduct,
)
from engine.data_loader import (
    load_yards, load_terminals, load_sites,
    load_drivers_for_date, load_loads_for_date,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ===========================================================================
# Load actuals from CSV
# ===========================================================================

def load_actuals_csv(csv_path: str, target_date: date) -> list[dict]:
    """Load actual deliveries from the CSV for a specific date."""
    actuals = []
    target_str = f"{target_date.month}/{target_date.day}/{target_date.year}"

    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            schedule_date = row["ScheduleDate"].split(" ")[0]
            if schedule_date == target_str:
                actuals.append(row)
    return actuals


def parse_actual_driver_name(driver_str: str) -> str:
    """Convert 'Last, First' to 'First Last' (lowercase)."""
    if "," in driver_str:
        parts = driver_str.split(",", 1)
        return f"{parts[1].strip()} {parts[0].strip()}".lower()
    return driver_str.strip().lower()


# ===========================================================================
# Engine runners
# ===========================================================================

def run_engine_v1(drivers, loads, sites, terminals, yards, dispatch_date):
    """Run v1 routing engine (committed version in git)."""
    from engine.routing_engine import RoutingEngine
    from engine import geo

    # Save v2 values and temporarily set v1 values
    orig_load_service = geo.LOAD_SERVICE_MINS
    orig_unload_service = geo.UNLOAD_SERVICE_MINS
    orig_multiplier = getattr(geo, 'TANKER_TRAVEL_MULTIPLIER', 1.0)

    # v1 uses: LOAD_SERVICE_MINS=45, UNLOAD_SERVICE_MINS=45, no tanker multiplier
    geo.LOAD_SERVICE_MINS = 45
    geo.UNLOAD_SERVICE_MINS = 45
    if hasattr(geo, 'TANKER_TRAVEL_MULTIPLIER'):
        geo.TANKER_TRAVEL_MULTIPLIER = 1.0

    try:
        engine = RoutingEngine(
            drivers=deepcopy(drivers),
            loads=deepcopy(loads),
            sites=sites,
            terminals=terminals,
            yards=yards,
            dispatch_date=dispatch_date,
        )
        result = engine.run()
    finally:
        # Restore
        geo.LOAD_SERVICE_MINS = orig_load_service
        geo.UNLOAD_SERVICE_MINS = orig_unload_service
        if hasattr(geo, 'TANKER_TRAVEL_MULTIPLIER'):
            geo.TANKER_TRAVEL_MULTIPLIER = orig_multiplier

    return result


def run_engine_v2(drivers, loads, sites, terminals, yards, dispatch_date):
    """Run v2 routing engine."""
    from engine.routing_engine_v2 import RoutingEngine as RoutingEngineV2
    from engine import geo

    # v2 uses: LOAD_SERVICE_MINS=30, UNLOAD_SERVICE_MINS=45, TANKER_TRAVEL_MULTIPLIER=1.20
    orig_load_service = geo.LOAD_SERVICE_MINS
    orig_unload_service = geo.UNLOAD_SERVICE_MINS
    orig_multiplier = getattr(geo, 'TANKER_TRAVEL_MULTIPLIER', 1.0)

    geo.LOAD_SERVICE_MINS = 30
    geo.UNLOAD_SERVICE_MINS = 45
    if hasattr(geo, 'TANKER_TRAVEL_MULTIPLIER'):
        geo.TANKER_TRAVEL_MULTIPLIER = 1.20

    try:
        engine = RoutingEngineV2(
            drivers=deepcopy(drivers),
            loads=deepcopy(loads),
            sites=sites,
            terminals=terminals,
            yards=yards,
            dispatch_date=dispatch_date,
        )
        result = engine.run()
    finally:
        geo.LOAD_SERVICE_MINS = orig_load_service
        geo.UNLOAD_SERVICE_MINS = orig_unload_service
        if hasattr(geo, 'TANKER_TRAVEL_MULTIPLIER'):
            geo.TANKER_TRAVEL_MULTIPLIER = orig_multiplier

    return result


# ===========================================================================
# Simulate actuals using same travel estimates as v2
# ===========================================================================

def simulate_actuals(actuals: list[dict], sites: dict, terminals: dict, yards: dict, drivers: list, dispatch_date: date):
    """
    Simulate the actual deliveries using the same geo calculations as v2.
    Computes empty and loaded miles using haversine/Google API, not the CSV values.
    """
    from engine.geo import haversine_miles, get_travel_mins_sync

    actual_driver_loads, actual_records = extract_actual_assignments(actuals)

    # Build driver lookup by name for yard info
    driver_by_name = {}
    for d in drivers:
        name = f"{d.first_name} {d.last_name}".strip().lower()
        driver_by_name[name] = d

    # Build terminal lookup by name
    terminal_by_name = {}
    for t in terminals.values():
        terminal_by_name[t.terminal_name.lower().strip()] = t

    # Build site lookup by name
    site_by_name = {}
    for s in sites.values():
        site_by_name[s.site_name.lower().strip()] = s

    total_empty_miles = 0.0
    total_loaded_miles = 0.0
    total_drivers = 0
    missed = 0

    for driver_name, deliveries in actual_driver_loads.items():
        driver = driver_by_name.get(driver_name)
        yard = driver.yard_location if driver else None

        # Start from yard
        if yard and yard.latitude and yard.longitude:
            current_lat, current_lon = yard.latitude, yard.longitude
        else:
            current_lat, current_lon = None, None

        driver_empty = 0.0
        driver_loaded = 0.0
        total_drivers += 1

        for rec in deliveries:
            terminal_name = (rec.get("terminal") or "").lower().strip()
            site_name = (rec.get("site") or "").lower().strip()

            terminal = terminal_by_name.get(terminal_name)
            site = site_by_name.get(site_name)

            if not terminal or not terminal.latitude or not terminal.longitude:
                missed += 1
                current_lat, current_lon = None, None
                continue
            if not site or not site.latitude or not site.longitude:
                missed += 1
                current_lat, current_lon = None, None
                continue

            # Empty miles: current position → terminal
            if current_lat and current_lon:
                empty = haversine_miles(current_lat, current_lon, terminal.latitude, terminal.longitude)
                driver_empty += empty

            # Loaded miles: terminal → site
            loaded = haversine_miles(terminal.latitude, terminal.longitude, site.latitude, site.longitude)
            driver_loaded += loaded

            # Update position to site
            current_lat, current_lon = site.latitude, site.longitude

        # Return to yard
        if current_lat and current_lon and yard and yard.latitude and yard.longitude:
            driver_empty += haversine_miles(current_lat, current_lon, yard.latitude, yard.longitude)

        total_empty_miles += driver_empty
        total_loaded_miles += driver_loaded

    total_miles = total_empty_miles + total_loaded_miles
    ratio = total_loaded_miles / total_miles if total_miles > 0 else 0

    return {
        "total_empty_miles": total_empty_miles,
        "total_loaded_miles": total_loaded_miles,
        "total_miles": total_miles,
        "loaded_mile_ratio": ratio,
        "drivers_used": total_drivers,
        "loads": len(actual_records),
        "missed_lookups": missed,
    }


# ===========================================================================
# Comparison metrics
# ===========================================================================

def extract_plan_assignments(result: DispatchResult) -> dict:
    """Extract assignments from a dispatch result.
    Returns {ce_id: {driver_name, sequence, empty_miles, loaded_miles, ...}}
    """
    assignments = {}
    for route in result.driver_routes:
        driver_name = f"{route.driver.first_name} {route.driver.last_name}".strip().lower()
        for stop in route.stops:
            assignments[stop.ce_id] = {
                "driver": driver_name,
                "sequence": stop.sequence,
                "empty_miles": stop.empty_miles,
                "loaded_miles": stop.loaded_miles,
                "arrive_terminal": stop.arrive_terminal,
                "depart_terminal": stop.depart_terminal,
                "arrive_site": stop.arrive_site,
                "depart_site": stop.depart_site,
                "drive_to_terminal_mins": stop.drive_to_terminal_mins,
                "drive_to_site_mins": stop.drive_to_site_mins,
            }
    return assignments


def extract_plan_driver_stats(result: DispatchResult) -> dict:
    """Per-driver stats from the plan."""
    stats = {}
    for route in result.driver_routes:
        driver_name = f"{route.driver.first_name} {route.driver.last_name}".strip().lower()
        stats[driver_name] = {
            "loads": len(route.stops),
            "total_loaded_miles": route.total_loaded_miles,
            "total_empty_miles": route.total_empty_miles,
            "total_shift_mins": route.total_shift_mins,
        }
    return stats


def extract_actual_assignments(actuals: list[dict]) -> dict:
    """Extract actual assignments from CSV.
    Returns {bol_id: {driver, miles, ...}} — keyed by BillOfLadingId since
    the actuals don't have ce_id directly.
    Also returns by-driver stats.
    """
    # Group by driver
    driver_loads = defaultdict(list)
    all_records = []

    for row in actuals:
        if row.get("BOLStatusDescription") not in ("Billing Exported", "Dispatch Confirmed"):
            continue

        driver_name = parse_actual_driver_name(row.get("driver", ""))
        bol_id = row.get("BillOfLadingId", "")
        miles = float(row.get("Miles") or 0)
        gallons = float(row.get("DeliveredGallons") or 0)
        rack_time = float(row.get("RackTime") or 0)
        travel_time = float(row.get("traveltime") or 0)
        drop_time = float(row.get("droploadtime") or 0)

        record = {
            "driver": driver_name,
            "bol_id": bol_id,
            "miles": miles,
            "gallons": gallons,
            "rack_time_mins": rack_time,
            "travel_time_mins": travel_time,
            "drop_time_mins": drop_time,
            "terminal": row.get("TerminalName", ""),
            "site": row.get("Dealer", ""),
            "city": row.get("City", ""),
            "slot": row.get("SlotDescription", ""),
            "shift": row.get("Shift", ""),
        }
        driver_loads[driver_name].append(record)
        all_records.append(record)

    return driver_loads, all_records


def compute_metrics(plan_result: DispatchResult, actuals: list[dict], label: str):
    """Compute comparison metrics between a plan and actuals."""
    plan_assignments = extract_plan_assignments(plan_result)
    plan_driver_stats = extract_plan_driver_stats(plan_result)
    actual_driver_loads, actual_records = extract_actual_assignments(actuals)

    # Summary metrics
    total_planned_loads = sum(s["loads"] for s in plan_driver_stats.values())
    total_actual_loads = len(actual_records)
    total_unassigned = len(plan_result.unassigned)

    total_planned_empty_miles = sum(s["total_empty_miles"] for s in plan_driver_stats.values())
    total_planned_loaded_miles = sum(s["total_loaded_miles"] for s in plan_driver_stats.values())
    total_actual_miles = sum(r["miles"] for r in actual_records)

    planned_drivers_used = len(plan_driver_stats)
    actual_drivers_used = len(actual_driver_loads)

    # Per-driver load count comparison
    all_drivers = set(list(plan_driver_stats.keys()) + list(actual_driver_loads.keys()))

    load_count_diffs = []
    for driver in all_drivers:
        planned = plan_driver_stats.get(driver, {}).get("loads", 0)
        actual = len(actual_driver_loads.get(driver, []))
        load_count_diffs.append({
            "driver": driver,
            "planned": planned,
            "actual": actual,
            "diff": planned - actual,
        })

    # Avg loads per driver
    avg_planned_per_driver = total_planned_loads / max(planned_drivers_used, 1)
    avg_actual_per_driver = total_actual_loads / max(actual_drivers_used, 1)

    return {
        "label": label,
        "total_planned_loads": total_planned_loads,
        "total_actual_loads": total_actual_loads,
        "total_unassigned": total_unassigned,
        "planned_drivers_used": planned_drivers_used,
        "actual_drivers_used": actual_drivers_used,
        "avg_planned_per_driver": avg_planned_per_driver,
        "avg_actual_per_driver": avg_actual_per_driver,
        "total_planned_empty_miles": total_planned_empty_miles,
        "total_planned_loaded_miles": total_planned_loaded_miles,
        "total_actual_miles": total_actual_miles,
        "load_count_diffs": sorted(load_count_diffs, key=lambda x: x["driver"]),
    }


# ===========================================================================
# Display results
# ===========================================================================

def print_comparison(v1_metrics: dict, v2_metrics: dict, actuals_sim: dict, dispatch_date: date):
    """Print side-by-side comparison."""
    print(f"\n{'='*80}")
    print(f"  BACKTEST RESULTS — {dispatch_date.isoformat()}")
    print(f"{'='*80}\n")

    print(f"{'Metric':<35} {'V1 (current)':<18} {'V2 (improved)':<18} {'Actuals':<18}")
    print(f"{'-'*35} {'-'*18} {'-'*18} {'-'*18}")

    actual_loads = actuals_sim["loads"]

    print(f"{'Loads assigned':<35} {v1_metrics['total_planned_loads']:<18} {v2_metrics['total_planned_loads']:<18} {actual_loads:<18}")
    print(f"{'Loads unassigned':<35} {v1_metrics['total_unassigned']:<18} {v2_metrics['total_unassigned']:<18} {'—':<18}")
    print(f"{'Drivers used':<35} {v1_metrics['planned_drivers_used']:<18} {v2_metrics['planned_drivers_used']:<18} {actuals_sim['drivers_used']:<18}")
    print(f"{'Avg loads/driver':<35} {v1_metrics['avg_planned_per_driver']:<18.1f} {v2_metrics['avg_planned_per_driver']:<18.1f} {actual_loads/max(actuals_sim['drivers_used'],1):<18.1f}")
    print(f"{'Total empty miles':<35} {v1_metrics['total_planned_empty_miles']:<18.0f} {v2_metrics['total_planned_empty_miles']:<18.0f} {actuals_sim['total_empty_miles']:<18.0f}")
    print(f"{'Total loaded miles':<35} {v1_metrics['total_planned_loaded_miles']:<18.0f} {v2_metrics['total_planned_loaded_miles']:<18.0f} {actuals_sim['total_loaded_miles']:<18.0f}")

    # Efficiency ratio
    v1_total = v1_metrics['total_planned_empty_miles'] + v1_metrics['total_planned_loaded_miles']
    v2_total = v2_metrics['total_planned_empty_miles'] + v2_metrics['total_planned_loaded_miles']
    v1_ratio = v1_metrics['total_planned_loaded_miles'] / max(v1_total, 1)
    v2_ratio = v2_metrics['total_planned_loaded_miles'] / max(v2_total, 1)
    print(f"{'Loaded mile ratio':<35} {v1_ratio:<18.1%} {v2_ratio:<18.1%} {actuals_sim['loaded_mile_ratio']:<18.1%}")

    # Per-driver comparison
    print(f"\n{'='*80}")
    print(f"  PER-DRIVER LOAD COUNT: V1 vs V2 vs Actuals")
    print(f"{'='*80}\n")
    print(f"{'Driver':<25} {'V1':<6} {'V2':<6} {'Actual':<8} {'V1 diff':<9} {'V2 diff':<9}")
    print(f"{'-'*25} {'-'*6} {'-'*6} {'-'*8} {'-'*9} {'-'*9}")

    # Merge driver lists
    all_drivers = set()
    for d in v1_metrics["load_count_diffs"]:
        all_drivers.add(d["driver"])
    for d in v2_metrics["load_count_diffs"]:
        all_drivers.add(d["driver"])

    v1_by_driver = {d["driver"]: d for d in v1_metrics["load_count_diffs"]}
    v2_by_driver = {d["driver"]: d for d in v2_metrics["load_count_diffs"]}

    for driver in sorted(all_drivers):
        v1d = v1_by_driver.get(driver, {"planned": 0, "actual": 0})
        v2d = v2_by_driver.get(driver, {"planned": 0, "actual": 0})
        actual = v1d["actual"]
        if v1d["planned"] == 0 and v2d["planned"] == 0 and actual == 0:
            continue
        v1_diff = v1d["planned"] - actual
        v2_diff = v2d["planned"] - actual
        v1_diff_str = f"+{v1_diff}" if v1_diff > 0 else str(v1_diff)
        v2_diff_str = f"+{v2_diff}" if v2_diff > 0 else str(v2_diff)
        print(f"{driver:<25} {v1d['planned']:<6} {v2d['planned']:<6} {actual:<8} {v1_diff_str:<9} {v2_diff_str:<9}")

    # Unassigned reasons summary
    print(f"\n{'='*80}")
    print(f"  UNASSIGNED LOAD REASONS")
    print(f"{'='*80}\n")

    print(f"{'Reason':<55} {'V1':<6} {'V2':<6}")
    print(f"{'-'*55} {'-'*6} {'-'*6}")

    v1_reasons = defaultdict(int)
    v2_reasons = defaultdict(int)
    for _, reason, _ in v1_metrics.get("_unassigned_raw", []):
        v1_reasons[reason] += 1
    for _, reason, _ in v2_metrics.get("_unassigned_raw", []):
        v2_reasons[reason] += 1

    all_reasons = set(list(v1_reasons.keys()) + list(v2_reasons.keys()))
    for reason in sorted(all_reasons):
        print(f"{reason[:54]:<55} {v1_reasons.get(reason, 0):<6} {v2_reasons.get(reason, 0):<6}")


# ===========================================================================
# Main
# ===========================================================================

def run_backtest(dispatch_date: date, csv_path: str):
    """Run full backtest for a single date."""
    print(f"\n--- Loading data for {dispatch_date.isoformat()} ---")

    # Connect to Supabase
    client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY"),
    )

    # Load all inputs (same as the real engine does)
    yards = load_yards(client)
    terminals = load_terminals(client)
    sites = load_sites(client)
    drivers = load_drivers_for_date(client, dispatch_date, yards)
    loads = load_loads_for_date(client, dispatch_date)

    # BACKTEST OVERRIDE: Reset all load statuses to unscheduled (1)
    # so the engine treats them as available for dispatch.
    # In production, loads for this date are already completed (status 70),
    # but for backtesting we simulate a fresh dispatch day.
    for load in loads:
        load.load_status = 1
        load.assigned_driver_id = None
        load.assigned_driver_first = None
        load.assigned_driver_last = None

    print(f"  Inputs: {len(drivers)} drivers, {len(loads)} loads, {len(terminals)} terminals, {len(sites)} sites")

    # Load actuals
    actuals = load_actuals_csv(csv_path, dispatch_date)
    print(f"  Actuals: {len(actuals)} deliveries from CSV")

    if not actuals:
        print("  ERROR: No actuals found for this date in CSV. Skipping.")
        return None, None

    # Run V1
    print(f"\n  Running V1 engine...")
    v1_result = run_engine_v1(drivers, loads, sites, terminals, yards, dispatch_date)
    v1_assigned = sum(len(r.stops) for r in v1_result.driver_routes)
    print(f"  V1: {v1_assigned} assigned, {len(v1_result.unassigned)} unassigned")

    # Run V2
    print(f"  Running V2 engine...")
    v2_result = run_engine_v2(drivers, loads, sites, terminals, yards, dispatch_date)
    v2_assigned = sum(len(r.stops) for r in v2_result.driver_routes)
    print(f"  V2: {v2_assigned} assigned, {len(v2_result.unassigned)} unassigned")

    # Compute metrics
    v1_metrics = compute_metrics(v1_result, actuals, "V1")
    v1_metrics["_unassigned_raw"] = v1_result.unassigned
    v2_metrics = compute_metrics(v2_result, actuals, "V2")
    v2_metrics["_unassigned_raw"] = v2_result.unassigned

    # Simulate actuals using same geo estimates as v2
    print(f"  Simulating actuals with v2 geo estimates...")
    actuals_sim = simulate_actuals(actuals, sites, terminals, yards, drivers, dispatch_date)
    print(f"  Actuals sim: {actuals_sim['loads']} loads, {actuals_sim['drivers_used']} drivers, "
          f"ratio={actuals_sim['loaded_mile_ratio']:.1%} (missed={actuals_sim['missed_lookups']})")

    # Print comparison
    print_comparison(v1_metrics, v2_metrics, actuals_sim, dispatch_date)

    return v1_metrics, v2_metrics


def main():
    parser = argparse.ArgumentParser(description="Backtest routing engine v1 vs v2")
    parser.add_argument("--date", help="Dispatch date (YYYY-MM-DD)")
    parser.add_argument("--csv", default=None,
                        help="Path to actuals CSV")
    parser.add_argument("--all-dates", action="store_true",
                        help="Run backtest for all dates in the CSV")
    parser.add_argument("--no-api", action="store_true",
                        help="Disable Google Maps API, use haversine only (fast)")
    args = parser.parse_args()

    # Resolve CSV path
    if args.csv:
        csv_path = args.csv
    else:
        csv_path = str(Path(__file__).resolve().parent.parent.parent / "Data" / "Delivery Data 6.22 to 6.26.csv")

    if not Path(csv_path).exists():
        print(f"ERROR: CSV not found at {csv_path}")
        sys.exit(1)

    # Disable Google Maps API if requested
    if args.no_api:
        os.environ["GOOGLE_MAPS_API_KEY"] = ""
        # Also patch it in the already-imported geo module
        from engine import geo
        geo.GOOGLE_MAPS_API_KEY = ""
        print("  [--no-api] Google Maps disabled, using haversine fallback")

    if args.all_dates:
        dates = [
            date(2026, 6, 22),
            date(2026, 6, 23),
            date(2026, 6, 24),
            date(2026, 6, 25),
            date(2026, 6, 26),
        ]
    elif args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        print("ERROR: Provide --date YYYY-MM-DD or --all-dates")
        sys.exit(1)

    for d in dates:
        run_backtest(d, csv_path)

    # Save persistent travel cache if used
    try:
        from engine.travel_cache import save_cache
        save_cache()
    except Exception:
        pass


if __name__ == "__main__":
    main()
