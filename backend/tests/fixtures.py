"""
Test fixtures for the AutoRouting engine.

Scenarios covered:
  1. Basic assignment          — straightforward load → closest driver wins
  2. Terminal access control   — driver without card must be skipped
  3. Pump certification        — only certified driver can take pump site load
  4. Delivery window           — timed load must beat the window; anytime load fills gaps
  5. Diesel-wet sequencing     — gasoline load cannot precede diesel at a wet terminal
  6. Shift time limit          — driver runs out of hours; second driver must absorb the load
  7. Reroute / locked loads    — in-progress loads stay on their driver
  8. Multi-stop route          — one driver gets 2 loads; best insert position is tested
  9. Unassigned (no driver)    — load with terminal nobody has access to → unassigned
 10. Constrained load first    — hardest load (1 eligible driver) sorts before easy loads

Each fixture function returns the exact dict/list types that RoutingEngine.__init__ expects:
  drivers  : list[Driver]
  loads    : list[Load]
  sites    : dict[int, Site]
  terminals: dict[int, Terminal]
  yards    : dict[str, Yard]
"""

from datetime import date, datetime, time
from models.models import (
    Driver, Load, Site, Terminal, Yard, LoadProduct,
)

DISPATCH_DATE = date(2026, 6, 1)
DATE_STR = DISPATCH_DATE.isoformat()


# ---------------------------------------------------------------------------
# Shared geography — real Texas coordinates so Google Maps gives meaningful times
# ---------------------------------------------------------------------------

YARDS = {
    "Dallas":   Yard(yard="Dallas",   latitude=32.7767, longitude=-96.7970,  city="Dallas",   state="TX"),
    "FtWorth":  Yard(yard="FtWorth",  latitude=32.7555, longitude=-97.3308,  city="Ft Worth", state="TX"),
    "Houston":  Yard(yard="Houston",  latitude=29.7604, longitude=-95.3698,  city="Houston",  state="TX"),
}

TERMINALS = {
    # Standard terminal (not diesel-wet)
    1: Terminal(terminal_id=1, terminal_name="Dallas Rack",
                latitude=32.8205, longitude=-96.8716,
                city="Dallas", state="TX", is_diesel_wet=0),
    # Another standard terminal, further south
    2: Terminal(terminal_id=2, terminal_name="Houston Rack",
                latitude=29.8168, longitude=-95.4057,
                city="Houston", state="TX", is_diesel_wet=0),
    # Diesel-wet terminal (sequencing rules apply)
    3: Terminal(terminal_id=3, terminal_name="FtWorth Wet Rack",
                latitude=32.7355, longitude=-97.4008,
                city="Ft Worth", state="TX", is_diesel_wet=1),
}

SITES = {
    # Normal site near Dallas
    101: Site(site_id=101, site_name="Shell #101",
              latitude=32.9483, longitude=-96.7302,
              customer_group_name="Shell", city="Plano", state="TX", pump_certified=0),
    # Normal site further from Dallas
    102: Site(site_id=102, site_name="Chevron #102",
              latitude=32.6010, longitude=-97.1208,
              customer_group_name="Chevron", city="Mansfield", state="TX", pump_certified=0),
    # Pump-certified site
    103: Site(site_id=103, site_name="Loves #103 (Pump)",
              latitude=32.3513, longitude=-97.3860,
              customer_group_name="Loves", city="Cleburne", state="TX", pump_certified=1),
    # Site near Houston
    104: Site(site_id=104, site_name="ExxonMobil #104",
              latitude=29.6516, longitude=-95.2837,
              customer_group_name="ExxonMobil", city="Pearland", state="TX", pump_certified=0),
    # Site near Ft Worth for diesel-wet test
    105: Site(site_id=105, site_name="Casey's #105",
              latitude=32.8254, longitude=-97.3809,
              customer_group_name="Caseys", city="Keller", state="TX", pump_certified=0),
}


def _dt(hour: int, minute: int = 0) -> datetime:
    """Helper: build a naive datetime on DISPATCH_DATE."""
    return datetime(DISPATCH_DATE.year, DISPATCH_DATE.month, DISPATCH_DATE.day, hour, minute)


def _load(
    ce_id: int,
    site_id: int,
    terminal_id: int,
    products: list,
    window_start_h: int = None,
    window_end_h: int = None,
    load_status: int = 1,
    assigned_driver_id: int = None,
) -> Load:
    """Helper: build a Load with sensible defaults."""
    ws = _dt(window_start_h) if window_start_h is not None else None
    we = _dt(window_end_h)   if window_end_h   is not None else None
    return Load(
        ce_id=ce_id,
        delivery_date=DATE_STR,
        customer_name=SITES[site_id].customer_group_name,
        order_number=f"ORD-{ce_id}",
        site_id=site_id,
        terminal_id=terminal_id,
        terminal_name=TERMINALS[terminal_id].terminal_name,
        products=[LoadProduct(p, 8000) for p in products],
        load_status=load_status,
        site_name=SITES[site_id].site_name,
        city=SITES[site_id].city,
        state=SITES[site_id].state,
        window_start=ws,
        window_end=we,
        assigned_driver_id=assigned_driver_id,
    )


def _driver(
    driver_id: int,
    name: str,
    yard: str,
    terminal_ids: set,
    start_hour: int = 6,
    pump_trained: int = 0,
    max_shift_hours: float = 12.0,
    board_location: str = "TX-AM",
    restricted_site_ids: set = None,
    restricted_customer_groups: set = None,
) -> Driver:
    first, last = name.split()
    d = Driver(
        driver_id=driver_id,
        first_name=first,
        last_name=last,
        yard=yard,
        board_location=board_location,
        start_time=time(start_hour, 0),
        pump_trained=pump_trained,
        max_shift_hours=max_shift_hours,
        yard_location=YARDS[yard],
        terminal_ids=terminal_ids,
    )
    d.restricted_site_ids = restricted_site_ids or set()
    d.restricted_customer_groups = restricted_customer_groups or set()
    return d


# ===========================================================================
# SCENARIO 1 — Basic assignment
# Two drivers, one load. Closer driver (Dallas) should win over Houston driver.
# Validates: engine assigns load, closer yard wins on score.
# ===========================================================================
def scenario_basic():
    drivers = [
        _driver(1, "Alice Close",  yard="Dallas",  terminal_ids={1}),  # near terminal 1
        _driver(2, "Bob Far",      yard="Houston", terminal_ids={1}),  # far from terminal 1
    ]
    loads = [
        _load(1001, site_id=101, terminal_id=1, products=["Regular"]),
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 2 — Terminal access control
# Only driver 2 has the card for terminal 2. Driver 1 must be skipped.
# Validates: terminal access check works, correct failure reason logged.
# ===========================================================================
def scenario_terminal_access():
    drivers = [
        _driver(1, "Alice NoCard", yard="Dallas",  terminal_ids={1}),       # no card for terminal 2
        _driver(2, "Bob HasCard",  yard="Dallas",  terminal_ids={1, 2}),    # has card
    ]
    loads = [
        _load(1002, site_id=104, terminal_id=2, products=["Regular"]),      # requires terminal 2
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 3 — Pump certification
# Site 103 requires pump_certified. Only driver 2 is pump-trained.
# Validates: pump check blocks driver 1, driver 2 gets the load.
# ===========================================================================
def scenario_pump_cert():
    drivers = [
        _driver(1, "Alice NoPump",  yard="FtWorth", terminal_ids={1, 3}, pump_trained=0),
        _driver(2, "Bob Pumper",    yard="FtWorth", terminal_ids={1, 3}, pump_trained=1),
    ]
    loads = [
        _load(1003, site_id=103, terminal_id=1, products=["Regular"]),
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 4 — Delivery window
# Load A: tight window 08:00–09:00 (timed, must go first)
# Load B: anytime
# One driver. Validates: timed load sorts first, window check passes/fails correctly.
# ===========================================================================
def scenario_delivery_window():
    drivers = [
        _driver(1, "Alice Window", yard="Dallas", terminal_ids={1}),
    ]
    loads = [
        # Anytime load — intentionally created first to confirm sort puts it last
        _load(1005, site_id=102, terminal_id=1, products=["Regular"],
              window_start_h=None, window_end_h=None),
        # Tight timed load — must sort first
        _load(1004, site_id=101, terminal_id=1, products=["Regular"],
              window_start_h=8, window_end_h=9),
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 5 — Diesel-wet sequencing
# Terminal 3 is diesel-wet. Load A is gasoline (Regular). Load B is diesel.
# Rule: diesel-wet terminal requires prior load to be diesel-only.
# Driver has 2 stops: if gasoline comes first, diesel-wet stop at pos=1 must fail.
# Validates: diesel-wet check blocks bad sequence.
# ===========================================================================
def scenario_diesel_wet():
    drivers = [
        _driver(1, "Alice DieselWet", yard="FtWorth", terminal_ids={1, 3}),
    ]
    loads = [
        # Gasoline load at standard terminal — assigned first
        _load(1006, site_id=101, terminal_id=1, products=["Regular"]),
        # Diesel-wet load — cannot follow a gasoline load
        _load(1007, site_id=105, terminal_id=3, products=["Diesel"]),
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 6 — Shift time exceeded
# Driver 1 has a very short shift (4 hours) — not enough for 2 loads.
# Driver 2 has a normal 12-hour shift.
# Validates: overflow load goes to driver 2, not left unassigned.
# ===========================================================================
def scenario_shift_overflow():
    drivers = [
        # 2-hour shift: enough for 1 short local stop but not 2
        _driver(1, "Alice Short",  yard="Dallas", terminal_ids={1}, max_shift_hours=2.0),
        _driver(2, "Bob Long",     yard="Dallas", terminal_ids={1}, max_shift_hours=12.0),
    ]
    loads = [
        _load(1008, site_id=101, terminal_id=1, products=["Regular"]),
        _load(1009, site_id=102, terminal_id=1, products=["Regular"]),
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 7 — Reroute with locked loads
# Load 1010 is STATUS_EN_ROUTE_RACK (12) assigned to driver 1.
# Load 1011 is unscheduled (1).
# In reroute mode: load 1010 must stay on driver 1; load 1011 gets assigned normally.
# Validates: _seed_locked_loads preserves in-progress assignment.
# ===========================================================================
def scenario_reroute():
    drivers = [
        _driver(1, "Alice EnRoute", yard="Dallas", terminal_ids={1}),
        _driver(2, "Bob Waiting",   yard="Dallas", terminal_ids={1}),
    ]
    loads = [
        _load(1010, site_id=101, terminal_id=1, products=["Regular"],
              load_status=12, assigned_driver_id=1),   # locked — en route to rack
        _load(1011, site_id=102, terminal_id=1, products=["Regular"],
              load_status=1),                           # unscheduled — free to assign
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE, reroute=True)


# ===========================================================================
# SCENARIO 8 — Multi-stop route (best insert position)
# Driver already has load 1012 assigned. Load 1013 can insert at pos 0 or pos 1.
# Validates: engine picks the insert position with the better (lower) score.
# ===========================================================================
def scenario_multi_stop():
    drivers = [
        _driver(1, "Alice Multi", yard="Dallas", terminal_ids={1}),
    ]
    loads = [
        _load(1012, site_id=101, terminal_id=1, products=["Regular"]),
        _load(1013, site_id=102, terminal_id=1, products=["Regular"]),
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 9 — No eligible driver (unassigned)
# Load requires terminal 2, but no driver has access to it.
# Validates: load ends up in unassigned list with correct reason.
# ===========================================================================
def scenario_unassigned():
    drivers = [
        _driver(1, "Alice Wrong", yard="Dallas", terminal_ids={1}),  # only terminal 1
        _driver(2, "Bob Wrong",   yard="Dallas", terminal_ids={1}),  # only terminal 1
    ]
    loads = [
        _load(1014, site_id=104, terminal_id=2, products=["Regular"]),  # needs terminal 2
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# SCENARIO 10 — Constrained load first (hardest load has only 1 eligible driver)
# Load A: only driver 2 can take it (terminal 2 access + pump cert required).
# Load B: any driver can take it.
# Validates: sort-by-difficulty puts load A first so it's not crowded out.
# ===========================================================================
def scenario_constrained_first():
    drivers = [
        _driver(1, "Alice Easy",       yard="Dallas",  terminal_ids={1},    pump_trained=0),
        _driver(2, "Bob Specialized",  yard="FtWorth", terminal_ids={1, 2}, pump_trained=1),
    ]
    loads = [
        # Easy load — both drivers eligible
        _load(1015, site_id=101, terminal_id=1, products=["Regular"]),
        # Hard load — only driver 2 (needs terminal 2 + pump cert)
        _load(1016, site_id=103, terminal_id=2, products=["Regular"]),
    ]
    return dict(drivers=drivers, loads=loads, sites=SITES, terminals=TERMINALS,
                yards=YARDS, dispatch_date=DISPATCH_DATE)


# ===========================================================================
# ALL SCENARIOS — convenience dict for the benchmark runner
# ===========================================================================
ALL_SCENARIOS = {
    "basic":               scenario_basic,
    "terminal_access":     scenario_terminal_access,
    "pump_cert":           scenario_pump_cert,
    "delivery_window":     scenario_delivery_window,
    "diesel_wet":          scenario_diesel_wet,
    "shift_overflow":      scenario_shift_overflow,
    "reroute":             scenario_reroute,
    "multi_stop":          scenario_multi_stop,
    "unassigned":          scenario_unassigned,
    "constrained_first":   scenario_constrained_first,
}
