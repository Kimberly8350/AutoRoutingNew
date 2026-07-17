"""
Data loader: fetches from Supabase, builds typed model objects for the routing engine.
"""

import logging
from datetime import date, datetime, time
from typing import Optional

from supabase import Client

from models.models import (
    Driver, Load, Site, Terminal, Yard, LoadProduct,
)

log = logging.getLogger(__name__)


def _parse_time(val) -> Optional[time]:
    if not val:
        return None
    if isinstance(val, time):
        return val
    try:
        parts = str(val).strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except Exception:
        return None


def _parse_dt(val) -> Optional[datetime]:
    """Parse a datetime value and always return a timezone-naive datetime.
    The routing engine builds shift times as naive datetimes, so all window
    timestamps must be naive to avoid offset-naive vs offset-aware comparisons.
    """
    if not val:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None)
    try:
        dt = datetime.fromisoformat(str(val))
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def load_yards(client: Client) -> dict[str, Yard]:
    rows = client.table("yard_locations").select("*").execute().data
    yards = {}
    for r in rows:
        if r.get("yard"):
            yards[r["yard"]] = Yard(
                yard=r["yard"],
                latitude=float(r.get("latitude") or 0),
                longitude=float(r.get("longitude") or 0),
                address=r.get("yard_address") or "",
                city=r.get("city") or "",
                state=r.get("state") or "",
                zip=str(r.get("zip") or ""),
            )
    log.info(f"Loaded {len(yards)} yards")
    return yards


def load_terminals(client: Client) -> dict[str, Terminal]:
    rows = client.table("terminal_locations").select("*").execute().data
    terminals = {}
    for r in rows:
        tid = str(r.get("terminal_id") or "").strip()
        if tid:
            terminals[tid] = Terminal(
                terminal_id=tid,
                terminal_name=r.get("terminal_name") or "",
                latitude=float(r.get("latitude") or 0),
                longitude=float(r.get("longitude") or 0),
                abbreviation=r.get("terminal_abbreviation") or r.get("terminal_abreviation") or "",
                address=r.get("terminal_address") or "",
                city=r.get("city") or "",
                state=r.get("state") or "",
                is_diesel_wet=int(r.get("is_diesel_wet") or 0),
            )
    log.info(f"Loaded {len(terminals)} terminals")
    return terminals


def load_sites(client: Client) -> dict[int, Site]:
    # Supabase default page limit is 1000 — paginate to fetch all sites.
    rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = client.table("site_details").select("*").range(offset, offset + page_size - 1).execute().data
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    sites = {}
    for r in rows:
        sid = r.get("site_id")
        if sid:
            sites[int(sid)] = Site(
                site_id=int(sid),
                site_name=r.get("site_name") or "",
                latitude=float(r.get("latitude") or 0),
                longitude=float(r.get("longitude") or 0),
                customer_group_name=r.get("customer_group_name") or "",
                address=r.get("site_address") or "",
                city=r.get("city") or "",
                state=r.get("state") or "",
                pump_certified=int(r.get("pump_certified") or 0),
                alternate_terminal_ids=r.get("alternate_terminal_ids") or [],
            )
    log.info(f"Loaded {len(sites)} sites")
    return sites


def load_driver_terminal_access(client: Client) -> dict[int, set]:
    """Returns {driver_id: {terminal_id, ...}}"""
    rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = (
            client.table("driver_terminal_cards")
            .select("driver_id, terminal_id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    access = {}
    for r in rows:
        did = r.get("driver_id")
        tid = r.get("terminal_id")
        if did and tid:
            access.setdefault(int(did), set()).add(str(tid).strip())
    return access


def load_driver_restrictions(client: Client) -> dict[int, dict]:
    """Returns {driver_id: {site_ids: set, customer_groups: set}}"""
    rows = client.table("driver_restrictions").select("*").execute().data
    restrictions = {}
    for r in rows:
        did = r.get("driver_id")
        if not did:
            continue
        did = int(did)
        if did not in restrictions:
            restrictions[did] = {"site_ids": set(), "customer_groups": set()}
        if r.get("restriction_type") == "site" and r.get("site_id"):
            restrictions[did]["site_ids"].add(int(r["site_id"]))
        if r.get("restriction_type") == "customer" and r.get("customer_group_name"):
            restrictions[did]["customer_groups"].add(r["customer_group_name"])
    return restrictions


def load_pre_assigned_counts(client: Client, dispatch_date: date) -> dict[str, int]:
    """Count CE-committed loads (status > 1) per driver name for capacity budgeting.

    Covers dispatch_date + next day so overnight drivers' next-day CE assignments
    are included in their capacity budget.  Keyed by lowercase 'first last' name.

    Deduplicated by ce_id (multi-product loads share a ce_id across several rows —
    one physical load, not several). A linked split pair committed to the same
    driver (split=1, split_with_ce_id pointing at another ce_id also committed to
    that driver) counts as 1 toward the total, since it's a single terminal visit.
    """
    from datetime import timedelta
    dates = [dispatch_date.isoformat(), (dispatch_date + timedelta(days=1)).isoformat()]
    # driver_key -> {ce_id: split_with_ce_id or None}
    driver_loads: dict[str, dict[int, Optional[int]]] = {}
    for d in dates:
        try:
            rows = (
                client.table("load_details")
                .select("ce_id,first_name,last_name,split,split_with_ce_id")
                .eq("delivery_date", d)
                .gt("load_status", 1)
                .not_.is_("first_name", "null")
                .execute()
                .data
            )
            for r in rows:
                fn = (r.get("first_name") or "").strip()
                ln = (r.get("last_name") or "").strip()
                ce_id = r.get("ce_id")
                if (fn or ln) and ce_id is not None:
                    key = f"{fn} {ln}".strip().lower()
                    partner = r.get("split_with_ce_id") if r.get("split") else None
                    driver_loads.setdefault(key, {})[int(ce_id)] = int(partner) if partner else None
        except Exception as e:
            log.warning(f"Could not load pre-assigned counts for {d}: {e}")

    counts: dict[str, int] = {}
    for key, ce_map in driver_loads.items():
        seen: set[int] = set()
        total = 0
        for ce_id, partner in ce_map.items():
            if ce_id in seen:
                continue
            seen.add(ce_id)
            if partner and partner in ce_map:
                seen.add(partner)  # linked split pair, both committed to this driver — count once
            total += 1
        counts[key] = total
    return counts


def load_driver_clock_events(client: Client, dispatch_date: date) -> dict[int, dict]:
    """Returns {driver_id: {route_start_time, route_finish_time}} for the given shift date."""
    try:
        rows = (
            client.table("driver_clock_events")
            .select("driver_id,route_start_time,route_finish_time")
            .eq("shift_date", dispatch_date.isoformat())
            .execute()
            .data
        )
        return {
            int(r["driver_id"]): {
                "route_start_time": _parse_dt(r.get("route_start_time")),
                "route_finish_time": _parse_dt(r.get("route_finish_time")),
            }
            for r in rows if r.get("driver_id")
        }
    except Exception as e:
        log.warning(f"Could not load driver clock events: {e}")
        return {}


def load_drivers_for_date(
    client: Client,
    dispatch_date: date,
    yards: dict[str, Yard],
    force_include: Optional[set[str]] = None,
) -> list[Driver]:
    """force_include: lowercase 'first last' names to include regardless of the
    yard/board_location/driver_inactive/attendance_expected checks below — for
    historical backtests where a driver's *current* inactive/attendance status
    doesn't reflect whether they actually worked on this past date."""
    force_include = force_include or set()
    date_str = dispatch_date.isoformat()
    rows = (
        client.table("driver_schedules")
        .select("*")
        .eq("shift_date", date_str)
        .execute()
        .data
    )

    # Exclude permanently inactive drivers (no longer with company)
    try:
        inactive_rows = client.table("driver_inactive").select("driver_id").execute().data
        inactive_ids = {r["driver_id"] for r in inactive_rows}
    except Exception:
        inactive_ids = set()

    terminal_access = load_driver_terminal_access(client)
    restrictions = load_driver_restrictions(client)
    clock_events = load_driver_clock_events(client, dispatch_date)
    pre_assigned_counts = load_pre_assigned_counts(client, dispatch_date)

    drivers = []
    seen = set()
    for r in rows:
        name_key = f"{(r.get('first_name') or '').strip()} {(r.get('last_name') or '').strip()}".strip().lower()
        forced = name_key in force_include

        did = r.get("driver_id")
        if not did or did in seen:
            continue

        if not forced:
            # Drivers without a board_location or yard, or with yard explicitly marked
            # "N/A" (termination convention), are considered inactive (no longer with QW)
            yard_val = (r.get("yard") or "").strip()
            if not r.get("board_location") or not yard_val or yard_val.upper() == "N/A":
                continue

            # Skip permanently deactivated drivers
            if int(did) in inactive_ids:
                continue

            # attendance_expected overrides driver_schedule
            is_working = int(r.get("attendance_expected") or 0)
            if not is_working:
                continue

        seen.add(int(did))

        start_t = _parse_time(r.get("driver_start_time")) or time(6, 0)
        yard_name = r.get("yard") or ""
        yard_loc = yards.get(yard_name)

        driver = Driver(
            driver_id=int(did),
            first_name=r.get("first_name") or "",
            last_name=r.get("last_name") or "",
            yard=yard_name,
            board_location=r.get("board_location") or "",
            start_time=start_t,
            pump_trained=int(r.get("pump_trained") or 0),
            max_shift_hours=float(r.get("max_shift_hours") or 12.0),
            yard_location=yard_loc,
            terminal_ids=terminal_access.get(int(did), set()),
        )
        rid = restrictions.get(int(did), {})
        driver.restricted_site_ids = rid.get("site_ids", set())
        driver.restricted_customer_groups = rid.get("customer_groups", set())

        clk = clock_events.get(int(did), {})
        driver.route_start_time = clk.get("route_start_time")
        driver.route_finish_time = clk.get("route_finish_time")

        # Count CE-committed loads so the engine knows how many slots are already taken
        name_key = f"{driver.first_name} {driver.last_name}".strip().lower()
        driver.pre_assigned_count = pre_assigned_counts.get(name_key, 0)

        drivers.append(driver)

    log.info(f"Loaded {len(drivers)} active drivers for {date_str}")
    return drivers


def load_loads_for_date(
    client: Client,
    dispatch_date: date,
) -> list[Load]:
    """Load all loads for dispatch_date and +1 day (tomorrow's orders)."""
    from datetime import timedelta
    dates = [
        dispatch_date.isoformat(),
        (dispatch_date + timedelta(days=1)).isoformat(),
    ]

    all_rows = []
    for d in dates:
        rows = (
            client.table("load_details")
            .select("*")
            .eq("delivery_date", d)
            .execute()
            .data
        )
        all_rows.extend(rows)

    # Build terminal name → terminal_id lookup (ODBC string IDs).
    # "Not Determined" (terminal_id T-00-00-0000) is CE Connect's placeholder for
    # loads that haven't been assigned a real terminal yet — excluded here so
    # name-based lookup doesn't "successfully" resolve to it (no driver has
    # card access to a placeholder terminal), letting the raw-ID / historical
    # fallback tiers below actually run instead.
    term_rows = client.table("terminal_locations").select("terminal_id, terminal_name").execute().data
    terminal_name_map: dict[str, str] = {
        r["terminal_name"].lower().strip(): str(r["terminal_id"]).strip()
        for r in term_rows
        if r.get("terminal_name") and r.get("terminal_id")
        and r["terminal_name"].lower().strip() != "not determined"
    }
    # Real, resolvable terminal IDs — used to validate the raw stored terminal_id
    # fallback below. CE Connect's raw terminal_id can be a bare numeric string
    # (e.g. "1.0") that doesn't match our ODBC-format IDs (e.g. "T-75-TX-2665"),
    # in which case it should NOT block the historical-inference tier that follows.
    valid_terminal_ids = {
        str(r["terminal_id"]).strip() for r in term_rows
        if r.get("terminal_id") and (r.get("terminal_name") or "").lower().strip() != "not determined"
    }

    # Group by ce_id
    ce_map: dict[int, dict] = {}
    for r in all_rows:
        ce = r.get("ce_id")
        if not ce:
            continue
        ce = int(ce)
        if ce not in ce_map:
            ce_map[ce] = {**r, "products": []}
        product = r.get("product_name")
        gallons = float(r.get("gross_gallons") or 0)
        if product:
            ce_map[ce]["products"].append(LoadProduct(product_name=product, gross_gallons=gallons))

    # Build site_id → terminal_id fallback map from historical load_details.
    # Loads from the new feed often have no terminal assigned yet (future orders);
    # we infer the most-recently-used terminal for that site as a best guess.
    site_terminal_fallback: dict[int, str] = {}
    try:
        # 1000 recent rows is sufficient to map site → most-used terminal.
        # Avoid paginating the full table — that scan causes request timeouts.
        hist_rows = (
            client.table("load_details")
            .select("site_id,terminal_name")
            .not_.is_("terminal_name", "null")
            .neq("terminal_name", "")
            .limit(1000)
            .execute()
            .data
        )
        from collections import Counter
        site_term_counts: dict[int, Counter] = {}
        for hr in hist_rows:
            sid = hr.get("site_id")
            tname = (hr.get("terminal_name") or "").lower().strip()
            tid = terminal_name_map.get(tname, "")
            if sid and tid:
                site_term_counts.setdefault(int(sid), Counter())[tid] += 1
        site_terminal_fallback = {
            sid: cnt.most_common(1)[0][0]
            for sid, cnt in site_term_counts.items()
        }
        log.info(f"Built site→terminal fallback map for {len(site_terminal_fallback)} sites")
    except Exception as e:
        log.warning(f"Could not build site→terminal fallback: {e}")

    loads = []
    for ce, r in ce_map.items():
        # Resolve terminal_id by name first (name-based lookup is authoritative).
        terminal_name = r.get("terminal_name") or ""
        terminal_id = terminal_name_map.get(terminal_name.lower().strip(), "")
        # Fall back to the raw stored terminal_id (already an ODBC string) — but
        # only if it's actually a real, resolvable terminal. CE Connect sometimes
        # stores a bare numeric placeholder (e.g. "1.0") here that isn't a real
        # ODBC ID; accepting it as-is would block the historical-inference tier
        # below and later fail to resolve to any Terminal object anyway.
        if not terminal_id:
            raw_tid = str(r.get("terminal_id") or "").strip()
            terminal_id = raw_tid if raw_tid and raw_tid.lower() != "none" and raw_tid in valid_terminal_ids else ""
        # Last resort: infer terminal from historical deliveries to this site
        if not terminal_id:
            site_id = int(r.get("site_id") or 0)
            terminal_id = site_terminal_fallback.get(site_id, "")
            if terminal_id:
                log.debug(f"ce_id={ce}: inferred terminal_id={terminal_id} from site history")

        load = Load(
            ce_id=ce,
            delivery_date=str(r.get("delivery_date") or "")[:10],
            customer_name=r.get("customer_name") or "",
            order_number=r.get("order_number"),
            site_id=int(r.get("site_id") or 0),
            terminal_id=terminal_id,
            terminal_name=terminal_name,
            products=r["products"],
            load_status=int(r.get("load_status") or 0),
            city=r.get("city") or "",
            state=r.get("state") or "",
            site_name=r.get("site_name") or "",
            site_address=r.get("site_address") or "",
            window_start=_parse_dt(r.get("window_start")),
            window_end=_parse_dt(r.get("window_end")),
            delivery_eta=_parse_dt(r.get("delivery_eta")),
            completed_delivery_time=_parse_dt(r.get("completed_delivery_time")),
            assigned_driver_id=None,  # resolved from first_name/last_name if needed
            assigned_driver_first=r.get("first_name"),
            assigned_driver_last=r.get("last_name"),
            split=int(r.get("split") or 0),
            split_with_ce_id=int(r["split_with_ce_id"]) if r.get("split_with_ce_id") else None,
        )
        loads.append(load)

    log.info(f"Loaded {len(loads)} unique loads (ce_ids) for {dispatch_date}")
    return loads
