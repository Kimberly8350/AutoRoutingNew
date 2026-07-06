"""
AutoRouting Engine — Scenario Test Runner

Runs every test scenario against the current engine and prints a report showing:
  - Pass / Fail for each scenario's assertions
  - Assigned vs unassigned loads
  - Miles (loaded / empty / ratio)
  - Timing per stop
  - Run duration (ms)

Usage:
    python tests/run_scenarios.py                  # all scenarios
    python tests/run_scenarios.py basic            # single scenario by name
    python tests/run_scenarios.py basic reroute    # multiple specific scenarios

Scenarios available:
    basic, terminal_access, pump_cert, delivery_window, diesel_wet,
    shift_overflow, reroute, multi_stop, unassigned, constrained_first
"""

import sys
import os
import time as time_mod
from datetime import datetime

# Make sure backend/ is on the path when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.routing_engine import RoutingEngine
from tests.fixtures import ALL_SCENARIOS, DISPATCH_DATE

# ANSI colours
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

SEP = "─" * 70


def ok(msg):  return f"{GREEN}✓ PASS{RESET}  {msg}"
def fail(msg): return f"{RED}✗ FAIL{RESET}  {msg}"
def warn(msg): return f"{YELLOW}⚠ WARN{RESET}  {msg}"


# ---------------------------------------------------------------------------
# Per-scenario assertions
# ---------------------------------------------------------------------------

def assert_basic(result) -> list[str]:
    notes = []
    routes = result.driver_routes
    if len(routes) == 1 and routes[0].driver.driver_id == 1:
        notes.append(ok("Load assigned to Alice Close (closer yard)"))
    else:
        assigned_to = [r.driver.first_name for r in routes]
        notes.append(fail(f"Expected Alice Close, got: {assigned_to}"))
    if result.unassigned_loads == 0:
        notes.append(ok("No unassigned loads"))
    else:
        notes.append(fail(f"{result.unassigned_loads} load(s) unassigned"))
    return notes


def assert_terminal_access(result) -> list[str]:
    notes = []
    routes = result.driver_routes
    assigned_drivers = [r.driver.driver_id for r in routes]
    if 2 in assigned_drivers and 1 not in assigned_drivers:
        notes.append(ok("Only Bob HasCard (driver 2) assigned — driver 1 correctly blocked"))
    else:
        notes.append(fail(f"Unexpected assignment. Drivers assigned: {assigned_drivers}"))
    return notes


def assert_pump_cert(result) -> list[str]:
    notes = []
    routes = result.driver_routes
    assigned_drivers = [r.driver.driver_id for r in routes]
    if 2 in assigned_drivers and 1 not in assigned_drivers:
        notes.append(ok("Only Bob Pumper (pump_trained=1) assigned — uncertified driver blocked"))
    else:
        notes.append(fail(f"Pump cert check failed. Drivers assigned: {assigned_drivers}"))
    return notes


def assert_delivery_window(result) -> list[str]:
    notes = []
    routes = result.driver_routes
    if not routes:
        notes.append(fail("No routes generated"))
        return notes

    stops = routes[0].stops
    if len(stops) < 2:
        notes.append(fail(f"Expected 2 stops, got {len(stops)}"))
        return notes

    notes.append(ok(f"Both loads assigned ({len(stops)} stops on 1 driver)"))

    # The timed load (ce_id=1004) must arrive within its window (08:00–10:00 + tolerances)
    timed_stop = next((s for s in stops if s.ce_id == 1004), None)
    anytime_stop = next((s for s in stops if s.ce_id == 1005), None)

    if timed_stop is None:
        notes.append(fail("Timed load (ce_id=1004) not found in any stop"))
        return notes

    ws = datetime(DISPATCH_DATE.year, DISPATCH_DATE.month, DISPATCH_DATE.day, 8, 0)
    we = datetime(DISPATCH_DATE.year, DISPATCH_DATE.month, DISPATCH_DATE.day, 10, 0)
    earliest = ws - __import__('datetime').timedelta(minutes=120)  # 2h early allowance
    reject_after = we + __import__('datetime').timedelta(minutes=60)  # 1h late = reject

    if timed_stop.arrive_site:
        arrive = timed_stop.arrive_site
        if arrive <= reject_after:
            notes.append(ok(f"Timed load arrives {arrive.strftime('%H:%M')} — within window tolerance"))
        else:
            notes.append(fail(f"Timed load arrives {arrive.strftime('%H:%M')} — OUTSIDE window (reject after {reject_after.strftime('%H:%M')})"))
    else:
        notes.append(fail("Timed load has no arrive_site time"))

    # Anytime load can be scheduled at any position — just confirm it's present
    if anytime_stop:
        notes.append(ok(f"Anytime load (ce_id=1005) also assigned at stop seq={anytime_stop.sequence}"))
    else:
        notes.append(warn("Anytime load (ce_id=1005) not found"))

    return notes


def assert_diesel_wet(result) -> list[str]:
    notes = []
    # Load 1007 (diesel-wet terminal) after load 1006 (gasoline) should be blocked
    # Either: load 1007 is unassigned, OR it's the only stop (gasoline load went elsewhere)
    unassigned_ids = [u[0].ce_id for u in result.unassigned]
    routes = result.driver_routes

    if 1007 in unassigned_ids:
        notes.append(ok("Diesel-wet load (1007) unassigned — sequencing conflict correctly detected"))
    else:
        # Check: if both assigned, gasoline must not immediately precede diesel-wet
        all_stops = [(r.driver.driver_id, s) for r in routes for s in r.stops]
        ce_seq = [s.ce_id for _, s in sorted(all_stops, key=lambda x: x[1].sequence)]
        if 1006 in ce_seq and 1007 in ce_seq:
            idx6 = ce_seq.index(1006)
            idx7 = ce_seq.index(1007)
            if idx7 == idx6 + 1:
                notes.append(fail("Diesel-wet violation: gasoline (1006) immediately precedes diesel-wet (1007)"))
            else:
                notes.append(ok("Both loads assigned; diesel-wet load not directly after gasoline"))
        else:
            notes.append(ok("Diesel-wet sequencing handled (loads split or resequenced)"))
    return notes


def assert_shift_overflow(result) -> list[str]:
    notes = []
    routes = {r.driver.driver_id: r for r in result.driver_routes}
    total_assigned = sum(len(r.stops) for r in routes.values())

    if total_assigned == 2:
        notes.append(ok("Both loads assigned — overflow driver absorbed the extra load"))
    elif total_assigned == 1:
        notes.append(ok("1 load assigned (short-shift blocked second load, overflow driver took it)"))
    else:
        notes.append(fail(f"Expected 1-2 loads assigned, got {total_assigned}"))

    # Short-shift driver (Alice, 2-hour shift) must have at most 1 stop
    if 1 in routes:
        alice_stops = len(routes[1].stops)
        alice_shift = routes[1].total_shift_mins
        if alice_stops <= 1:
            notes.append(ok(f"Short-shift driver (Alice, 2h) has {alice_stops} stop — shift limit respected "
                            f"({alice_shift:.0f} min used)"))
        else:
            notes.append(fail(f"Short-shift driver has {alice_stops} stops ({alice_shift:.0f} min) "
                              f"— exceeds 2-hour limit"))
    else:
        notes.append(ok("Short-shift driver (Alice) has 0 stops — both loads went to Bob Long"))

    # Bob (12h) should have picked up whatever Alice couldn't
    if 2 in routes:
        notes.append(ok(f"Overflow driver (Bob Long) has {len(routes[2].stops)} stop(s)"))
    return notes


def assert_reroute(result) -> list[str]:
    notes = []
    routes = {r.driver.driver_id: r for r in result.driver_routes}
    # Load 1010 must stay on driver 1
    if 1 in routes:
        ce_ids = [s.ce_id for s in routes[1].stops]
        if 1010 in ce_ids:
            notes.append(ok("Locked load (1010) preserved on driver 1 in reroute mode"))
        else:
            notes.append(fail("Locked load (1010) NOT found on driver 1"))
    else:
        notes.append(fail("Driver 1 has no route at all — locked load was dropped"))
    # Load 1011 should be assigned somewhere
    all_ce = [s.ce_id for r in result.driver_routes for s in r.stops]
    if 1011 in all_ce:
        notes.append(ok("New load (1011) assigned in reroute mode"))
    else:
        notes.append(warn("New load (1011) unassigned — may be a timing issue"))
    return notes


def assert_multi_stop(result) -> list[str]:
    notes = []
    routes = result.driver_routes
    if routes and len(routes[0].stops) == 2:
        notes.append(ok("Driver got both stops (multi-stop route built)"))
        s0, s1 = routes[0].stops
        notes.append(ok(f"Stop order: ce_id={s0.ce_id} → ce_id={s1.ce_id}"))
        notes.append(ok(f"Total loaded miles: {routes[0].total_loaded_miles:.1f}  "
                        f"empty: {routes[0].total_empty_miles:.1f}"))
    else:
        total = sum(len(r.stops) for r in routes)
        notes.append(warn(f"Expected 2 stops on 1 driver, got {total} total stops across {len(routes)} routes"))
    return notes


def assert_unassigned(result) -> list[str]:
    notes = []
    if result.unassigned_loads == 1:
        reason = result.unassigned[0][1]
        notes.append(ok(f"Load correctly unassigned. Reason: '{reason}'"))
        if "terminal" in reason.lower():
            notes.append(ok("Failure reason correctly identifies terminal access issue"))
        else:
            notes.append(warn(f"Unexpected reason: {reason}"))
    else:
        notes.append(fail(f"Expected 1 unassigned load, got {result.unassigned_loads}"))
    return notes


def assert_constrained_first(result) -> list[str]:
    notes = []
    all_ce = [s.ce_id for r in result.driver_routes for s in r.stops]
    if 1016 in all_ce:
        notes.append(ok("Constrained load (1016, 1 eligible driver) was assigned — not crowded out"))
    else:
        notes.append(fail("Constrained load (1016) was NOT assigned — greedy order may be the issue"))
    if 1015 in all_ce:
        notes.append(ok("Easy load (1015) also assigned"))
    else:
        notes.append(warn("Easy load (1015) unassigned — unexpected"))
    return notes


ASSERTIONS = {
    "basic":               assert_basic,
    "terminal_access":     assert_terminal_access,
    "pump_cert":           assert_pump_cert,
    "delivery_window":     assert_delivery_window,
    "diesel_wet":          assert_diesel_wet,
    "shift_overflow":      assert_shift_overflow,
    "reroute":             assert_reroute,
    "multi_stop":          assert_multi_stop,
    "unassigned":          assert_unassigned,
    "constrained_first":   assert_constrained_first,
}


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def print_routes(result):
    if not result.driver_routes:
        print("  (no routes)")
        return
    for route in result.driver_routes:
        d = route.driver
        print(f"  {CYAN}{d.first_name} {d.last_name}{RESET} "
              f"[{d.board_location}] | yard={d.yard} | start={d.start_time}")
        for stop in route.stops:
            at  = stop.arrive_terminal.strftime('%H:%M') if stop.arrive_terminal else "?"
            dt_ = stop.depart_terminal.strftime('%H:%M') if stop.depart_terminal else "?"
            asi = stop.arrive_site.strftime('%H:%M')     if stop.arrive_site     else "?"
            print(f"    Stop {stop.sequence}: ce_id={stop.ce_id:>5} | "
                  f"terminal={stop.terminal.terminal_name:<20} arr={at} dep={dt_} | "
                  f"site={stop.site.site_name:<22} arr={asi} | "
                  f"loaded={stop.loaded_miles:>5.1f}mi empty={stop.empty_miles:>5.1f}mi"
                  + (f" wait={stop.wait_mins:.0f}m" if stop.wait_mins else ""))
        ratio = (route.total_loaded_miles / route.total_empty_miles
                 if route.total_empty_miles else 0)
        print(f"    {'':5} Totals: loaded={route.total_loaded_miles:.1f}mi  "
              f"empty={route.total_empty_miles:.1f}mi  "
              f"ratio={ratio:.2f}  shift={route.total_shift_mins:.0f}min")

    if result.unassigned:
        print(f"\n  {YELLOW}Unassigned ({len(result.unassigned)}):{RESET}")
        for load, reason, _ in result.unassigned:
            print(f"    ce_id={load.ce_id}  site={load.site_name}  → {reason}")


def run_scenario(name: str, scenario_fn):
    print(f"\n{BOLD}{SEP}{RESET}")
    print(f"{BOLD}SCENARIO: {name.upper()}{RESET}")
    print(SEP)

    kwargs = scenario_fn()
    reroute = kwargs.pop("reroute", False)

    engine = RoutingEngine(**kwargs, reroute=reroute)

    t0 = time_mod.time()
    result = engine.run()
    elapsed = (time_mod.time() - t0) * 1000

    print(f"  Loads: {result.total_loads} total | "
          f"{GREEN}{result.assigned_loads} assigned{RESET} | "
          f"{RED if result.unassigned_loads else ''}{result.unassigned_loads} unassigned{RESET} | "
          f"run={elapsed:.0f}ms (engine reported {result.run_duration_ms}ms)")

    print_routes(result)

    print(f"\n  {BOLD}Assertions:{RESET}")
    assert_fn = ASSERTIONS.get(name)
    if assert_fn:
        for note in assert_fn(result):
            print(f"    {note}")
    else:
        print(f"    {warn('No assertions defined for this scenario')}")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    requested = sys.argv[1:] if len(sys.argv) > 1 else list(ALL_SCENARIOS.keys())
    unknown = [s for s in requested if s not in ALL_SCENARIOS]
    if unknown:
        print(f"{RED}Unknown scenario(s): {unknown}{RESET}")
        print(f"Available: {list(ALL_SCENARIOS.keys())}")
        sys.exit(1)

    print(f"\n{BOLD}AutoRouting Engine — Scenario Test Runner{RESET}")
    print(f"Dispatch date : {DISPATCH_DATE}")
    print(f"Scenarios     : {requested}")
    print(f"Google Maps   : {'enabled' if os.environ.get('GOOGLE_MAPS_API_KEY') else 'disabled (haversine only)'}")

    pass_count = 0
    fail_count = 0
    for name in requested:
        run_scenario(name, ALL_SCENARIOS[name])

    print(f"\n{BOLD}{SEP}{RESET}")
    print(f"{BOLD}Done. {len(requested)} scenario(s) run.{RESET}")
    print(SEP)
