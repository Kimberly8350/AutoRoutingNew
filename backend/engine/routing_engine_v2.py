"""
AutoRouting Engine
Greedy assignment engine with 2-opt / local-search improvement pass.

Dispatch flow
-------------
1. Greedy pass  — assigns each load (priority-sorted) to the best available
   driver/position found at the moment of assignment.
2. 2-opt improvement loop — after the greedy pass, repeatedly tries:
     a. SWAP  : exchange one load between two drivers
     b. MOVE  : relocate a single load from one driver to another
   Each candidate is accepted only if the *combined* route score improves.
   The loop runs until no improving move is found or MAX_OPT_ITERS is reached.

Score function (lower = better): total_empty_miles - total_loaded_miles
This maximises the loaded-vs-deadhead ratio across the fleet.
"""

import uuid
import logging
import time as time_mod
from copy import deepcopy, copy
from datetime import datetime, timedelta, date
from typing import Optional

from models.models import (
    Driver, Load, Site, Terminal, Yard, DriverRoute, RouteStop,
    DispatchResult, LoadProduct,
)
from engine.geo import (
    get_travel_mins_sync, haversine_miles,
    LOAD_SERVICE_MINS, UNLOAD_SERVICE_MINS,
)

log = logging.getLogger(__name__)

# Load status constants
STATUS_UNSCHEDULED = 1
STATUS_PLANNED = 2
STATUS_ASSIGNED = 10
STATUS_EN_ROUTE_RACK = 12
STATUS_AT_RACK = 20
STATUS_EN_ROUTE_SITE = 22
STATUS_AT_SITE = 24
STATUS_DELIVERED = 26

LOCKED_STATUSES = {STATUS_ASSIGNED, STATUS_EN_ROUTE_RACK, STATUS_AT_RACK, STATUS_EN_ROUTE_SITE, STATUS_AT_SITE, STATUS_DELIVERED}
SAME_TERMINAL_SWAP_STATUS = {STATUS_EN_ROUTE_RACK}

# 2-opt local search cap — prevents runaway loops on large dispatch days
MAX_OPT_ITERS = 200

# Delivery window tolerance
EARLY_ALLOWANCE_MINS = 120  # 2 hours early
LATE_ALLOWANCE_MINS = 60    # 1 hour late
REJECT_LATE_MINS = 240      # reject if more than 4 hours late

# Priority for unassigned reason display
REASON_PRIORITY = [
    "No eligible driver: No active working driver.",
    "Driver unavailable.",
    "No eligible terminal: Driver restricted from this terminal.",
    "No eligible terminal: Driver has no terminal access.",
    "No eligible terminal: Terminal location unavailable.",
    "No feasible assignment: Pump certification required.",
    "No feasible assignment: Diesel-wet sequencing conflict.",
    "Delivery window missed.",
    "Shift time exceeded.",
    "No feasible assignment: Driver restricted from this site.",
    "No feasible assignment: Site location unavailable.",
    "Invalid input data.",
    "No feasible assignment.",
]

GASOLINE_PRODUCTS = {"Regular", "MidGrade", "Super", "Gas-Other"}


def reason_priority(r: str) -> int:
    try:
        return REASON_PRIORITY.index(r)
    except ValueError:
        return len(REASON_PRIORITY)


class RoutingEngine:

    def __init__(
        self,
        drivers: list[Driver],
        loads: list[Load],
        sites: dict[int, Site],
        terminals: dict[int, Terminal],
        yards: dict[str, Yard],
        dispatch_date: date,
        reroute: bool = False,
    ):
        self.drivers = drivers
        self.loads = loads
        self.sites = sites
        self.terminals = terminals
        self.yards = yards
        self.dispatch_date = dispatch_date
        self.reroute = reroute
        self.routes: dict[int, DriverRoute] = {}
        self.unassigned: list[tuple] = []  # (load, reason, category)

    def _shift_start(self, driver: Driver) -> datetime:
        t = driver.start_time
        return datetime(
            self.dispatch_date.year,
            self.dispatch_date.month,
            self.dispatch_date.day,
            t.hour, t.minute, t.second,
        )

    def _shift_end(self, driver: Driver) -> datetime:
        return self._shift_start(driver) + timedelta(hours=driver.max_shift_hours)

    # ---- eligibility checks ----

    # Terminal → eligible regions mapping.
    # Derived from historical actuals: which board_locations load from each terminal.
    # Terminals shared across regions list all eligible regions.
    TERMINAL_REGIONS = {
        "tyler delek": {"ET-AM"},
        "global hearne": {"ET-AM"},
        "sunoco caddo llc": {"TX-AM"},
        "us oil melissa": {"TX-AM", "TX-PM"},
        "dallas magellan": {"TX-AM", "TX-PM"},
        "dallas motiva": {"TX-AM", "TX-PM"},
        "motiva enterprises llc": {"TX-AM", "TX-PM", "FW-AM", "FW-PM", "ET-AM"},
        "irving exxon": {"TX-AM", "TX-PM", "FW-AM", "FW-PM"},
        "global dallas": {"TX-AM", "TX-PM", "FW-AM", "FW-PM"},
        "euless flint hills": {"FW-AM", "FW-PM", "TX-AM", "TX-PM"},
        "ft worth motiva": {"FW-AM", "FW-PM", "TX-PM"},
        "ft worth chevron": {"FW-AM", "FW-PM"},
        "southlake nustar": {"FW-AM", "FW-PM", "TX-AM", "TX-PM"},
        "musket": {"FW-AM"},
        "cresson": {"FW-AM"},
        "euless kinder morgan": {"FW-AM"},
        "waco flint hills": {"FW-AM"},
        "waco motiva": {"FW-AM"},
        "aledo magellan": {"FW-AM", "FW-PM"},
        "direct fuels llc": {"FW-AM", "TX-PM"},
    }

    def _get_load_region(self, load: Load) -> Optional[set]:
        """Return the set of eligible regions for a load based on its terminal.
        Returns None if terminal is unknown (no constraint applied).
        """
        if not load.terminal_name:
            return None
        return self.TERMINAL_REGIONS.get(load.terminal_name.lower().strip())

    def _get_viable_terminal(self, driver: Driver, load: Load) -> Optional["Terminal"]:
        """Return the terminal to use for this driver/load pair.
        Tries the primary terminal first, then any alternates defined on the site.
        Returns None if no accessible, resolved terminal is found.
        """
        # Primary terminal — driver has access and terminal object is resolved
        if load.terminal_id and load.terminal_id in driver.terminal_ids and load.terminal:
            return load.terminal
        # Alternate terminals from site (e.g. GPM interchangeable rack group)
        if load.site and load.site.alternate_terminal_ids:
            for alt_tid in load.site.alternate_terminal_ids:
                if alt_tid == load.terminal_id:
                    continue  # already tried above
                if alt_tid in driver.terminal_ids:
                    alt_terminal = self.terminals.get(alt_tid)
                    if alt_terminal:
                        return alt_terminal
        return None

    def _check_driver_eligible(self, driver: Driver, load: Load) -> Optional[str]:
        """Return failure reason string or None if eligible.
        Terminal access is NOT checked here — handled separately via _get_viable_terminal
        so that alternate terminals are considered before rejecting the driver.
        """
        site = load.site
        # Region constraint: driver's board_location must match the load's region
        if driver.board_location and load.terminal_name:
            load_region = self._get_load_region(load)
            if load_region and driver.board_location not in load_region:
                return "No feasible assignment: Load outside driver region."
        # Pump certification
        if site and site.pump_certified and not driver.pump_trained:
            return "No feasible assignment: Pump certification required."
        # Site restriction
        if site and site.site_id in driver.restricted_site_ids:
            return "No feasible assignment: Driver restricted from this site."
        # Customer group restriction — compare the site's group name, not the raw customer name
        if site and site.customer_group_name and site.customer_group_name in driver.restricted_customer_groups:
            return "No feasible assignment: Driver restricted from this site."
        return None

    def _check_diesel_wet_sequence(self, driver: Driver, load: Load, insert_pos: int) -> Optional[str]:
        """Check diesel-wet sequencing rule for a proposed insertion."""
        terminal = load.terminal
        if not terminal or not terminal.is_diesel_wet:
            return None

        route = self.routes.get(driver.driver_id)
        if not route or insert_pos == 0:
            return None

        prev_stop = route.stops[insert_pos - 1]
        prev_load_products = prev_stop.site  # we need to check product names

        # We need to retrieve product info - stored in the load
        # Find the previous load by ce_id
        prev_load = self._find_load_by_ce(route.stops[insert_pos - 1].ce_id)
        if prev_load is None:
            return None

        # Prior load must: have diesel, no gasoline, no dyed (bio is ok)
        if not prev_load.has_diesel:
            return "No feasible assignment: Diesel-wet sequencing conflict."
        if prev_load.has_gasoline:
            return "No feasible assignment: Diesel-wet sequencing conflict."
        if prev_load.has_dyed:
            return "No feasible assignment: Diesel-wet sequencing conflict."
        return None

    def _find_load_by_ce(self, ce_id: int) -> Optional[Load]:
        for load in self.loads:
            if load.ce_id == ce_id:
                return load
        return None

    # ---- route simulation ----

    def _simulate_route(
        self,
        driver: Driver,
        stops: list[tuple],  # list of (load, insert_position)
    ) -> Optional[DriverRoute]:
        """
        Simulate full route for driver with ordered load stops.
        Returns DriverRoute or None if timing fails.
        """
        route = DriverRoute(driver=driver)
        shift_start = self._shift_start(driver)
        shift_end = self._shift_end(driver)
        yard = driver.yard_location

        current_lat = yard.latitude if yard else 0
        current_lon = yard.longitude if yard else 0
        current_time = shift_start
        total_loaded_miles = 0.0
        total_empty_miles = 0.0

        for seq, (load, _) in enumerate(stops):
            terminal = load.terminal
            site = load.site
            if not terminal or not site:
                return None

            # Drive from current pos to terminal (empty)
            drive_to_terminal_mins = get_travel_mins_sync(
                current_lat, current_lon,
                terminal.latitude, terminal.longitude,
                int(current_time.timestamp()),
            )
            empty_miles = haversine_miles(current_lat, current_lon, terminal.latitude, terminal.longitude)
            arrive_terminal = current_time + timedelta(minutes=drive_to_terminal_mins)

            if arrive_terminal > shift_end:
                return None

            depart_terminal = arrive_terminal + timedelta(minutes=LOAD_SERVICE_MINS)

            # Drive from terminal to site (loaded)
            drive_to_site_mins = get_travel_mins_sync(
                terminal.latitude, terminal.longitude,
                site.latitude, site.longitude,
                int(depart_terminal.timestamp()),
            )
            loaded_miles = haversine_miles(terminal.latitude, terminal.longitude, site.latitude, site.longitude)
            arrive_site_raw = depart_terminal + timedelta(minutes=drive_to_site_mins)

            # Delivery window logic.
            # Overdue loads (window_end before dispatch day) have no enforceable window —
            # treat as anytime so the engine can still dispatch them.
            dispatch_day_start = datetime(
                self.dispatch_date.year, self.dispatch_date.month, self.dispatch_date.day
            )
            is_overdue = (
                not load.is_anytime
                and load.window_end is not None
                and load.window_end < dispatch_day_start
            )
            wait_mins = 0.0
            if not load.is_anytime and not is_overdue and load.window_start:
                earliest_allowed = load.window_start - timedelta(minutes=EARLY_ALLOWANCE_MINS)
                if arrive_site_raw < earliest_allowed:
                    # Wait at site or staging
                    wait_mins = (earliest_allowed - arrive_site_raw).total_seconds() / 60
                    arrive_site_raw = earliest_allowed

                if load.window_end:
                    reject_after = load.window_end + timedelta(minutes=REJECT_LATE_MINS)
                    if arrive_site_raw > reject_after:
                        return None  # missed window

            arrive_site = arrive_site_raw
            if arrive_site > shift_end:
                return None

            depart_site = arrive_site + timedelta(minutes=UNLOAD_SERVICE_MINS)

            stop = RouteStop(
                ce_id=load.ce_id,
                sequence=seq,
                terminal=terminal,
                site=site,
                arrive_terminal=arrive_terminal,
                depart_terminal=depart_terminal,
                arrive_site=arrive_site,
                depart_site=depart_site,
                drive_to_terminal_mins=drive_to_terminal_mins,
                drive_to_site_mins=drive_to_site_mins,
                loaded_miles=loaded_miles,
                empty_miles=empty_miles,
                wait_mins=wait_mins,
            )
            route.stops.append(stop)

            total_loaded_miles += loaded_miles
            total_empty_miles += empty_miles

            current_lat = site.latitude
            current_lon = site.longitude
            current_time = depart_site

        # Last stop: return to yard
        if yard and route.stops:
            return_mins = get_travel_mins_sync(
                current_lat, current_lon,
                yard.latitude, yard.longitude,
                int(current_time.timestamp()),
            )
            return_time = current_time + timedelta(minutes=return_mins)
            if return_time > shift_end:
                return None
            route.return_to_yard_time = return_time
            total_empty_miles += haversine_miles(current_lat, current_lon, yard.latitude, yard.longitude)

        route.total_loaded_miles = total_loaded_miles
        route.total_empty_miles = total_empty_miles
        route.total_shift_mins = (current_time - shift_start).total_seconds() / 60

        return route

    # ---- load sorting ----

    def _sort_loads(self, loads: list[Load]) -> list[Load]:
        """
        Sort by priority:
        1. is_anytime=False first (timed loads first, anytime last)
        2. ASAP (status=planned, no window)
        3. Earliest delivery date
        4. Earliest window start
        5. (priority value not in data — skipped)
        """
        def sort_key(load: Load):
            anytime = 1 if load.is_anytime else 0
            date_key = load.delivery_date or "9999-99-99"
            win_start = load.window_start or datetime.max
            return (anytime, date_key, win_start)
        return sorted(loads, key=sort_key)

    # ---- driver ordering ----

    def _sort_drivers(self, drivers: list[Driver], reroute_driver_id: Optional[int] = None) -> list[Driver]:
        routes = self.routes

        def sort_key(d: Driver):
            is_current = 0 if (reroute_driver_id and d.driver_id == reroute_driver_id) else 1
            assignment_count = len(routes.get(d.driver_id, DriverRoute(driver=d)).stops)
            last_end = datetime.min
            route = routes.get(d.driver_id)
            if route and route.stops:
                last_end = route.stops[-1].depart_site or datetime.min
            return (is_current, assignment_count, last_end, d.driver_id)

        return sorted(drivers, key=sort_key)

    # ---- seed locked loads in reroute mode ----

    def _locked_load_sort_key(self, load: Load):
        """
        Sort locked loads into chronological shift order:

          1. Delivered (26)         — completed; by completed_delivery_time asc
          2. At Site (24)           — currently unloading
          3. En Route to Site (22)  — loaded, driving to delivery
          4. At Rack (20)           — loading at terminal
          5. En Route to Rack (12)  — driving to terminal
          6. Dispatched (10)        — queued, not yet started

        Within groups 2–6 sort by delivery_eta (or window_start for status 10
        loads that may not have an eta yet).  Higher status = further along =
        shown earlier because it happened (or is happening) first.
        """
        if load.load_status == STATUS_DELIVERED:
            return (0, 0, load.completed_delivery_time or datetime.max)
        # For all active/queued loads: negate status so higher status sorts first
        eta = load.delivery_eta or load.window_start or datetime.max
        return (1, -load.load_status, eta)

    def _seed_locked_loads(self, load_map: dict[int, Load]):
        """Preserve loads with locked statuses on their assigned driver.

        Always runs (not just in reroute mode) so that pre-assigned and
        in-progress loads always appear on the correct driver regardless of
        whether this is a fresh dispatch or a reroute.

        Seeding rules:
        - Same-day loads (delivery_date == dispatch_date): seed unless the load's
          window/eta predates the driver's shift start (i.e. it belongs to a
          previous shift earlier the same calendar day).
        - Next-day loads (delivery_date == dispatch_date + 1): seed ONLY for
          overnight drivers (start_time >= 20:00) whose shift spans midnight.
        - All other dates: skip.
        """
        dispatch_date_str = str(self.dispatch_date)
        next_date_str = (self.dispatch_date + timedelta(days=1)).isoformat()
        dispatch_day_start = datetime(
            self.dispatch_date.year, self.dispatch_date.month, self.dispatch_date.day
        )
        driver_lookup = {d.driver_id: d for d in self.drivers}

        # Overnight drivers whose shift spans midnight (start >= 20:00)
        overnight_driver_ids = {
            d.driver_id for d in self.drivers
            if d.start_time and d.start_time.hour >= 20
        }

        # Group locked loads by driver
        driver_locked: dict[int, list[Load]] = {}
        for load in self.loads:
            if load.load_status not in LOCKED_STATUSES:
                continue
            if not load.assigned_driver_id:
                continue

            load_date = load.delivery_date[:10] if load.delivery_date else ""

            if load_date == dispatch_date_str:
                # Same-day load: skip if window predates this driver's shift start
                # (would belong to an earlier AM shift on the same calendar day).
                driver = driver_lookup.get(load.assigned_driver_id)
                if driver and driver.start_time:
                    driver_shift_start = dispatch_day_start.replace(
                        hour=driver.start_time.hour,
                        minute=driver.start_time.minute,
                    )
                    load_time = load.window_start or load.delivery_eta
                    if load_time is not None and load_time < driver_shift_start:
                        log.debug(
                            f"Skipping seed ce_id={load.ce_id}: load time {load_time} "
                            f"predates driver {load.assigned_driver_id} shift start {driver_shift_start}"
                        )
                        continue
            elif load_date == next_date_str:
                # Next-day load: only seed for overnight drivers
                if load.assigned_driver_id not in overnight_driver_ids:
                    continue
            else:
                continue  # outside the relevant date window

            driver_locked.setdefault(load.assigned_driver_id, []).append(load)

        for driver_id, locked_loads in driver_locked.items():
            driver = next((d for d in self.drivers if d.driver_id == driver_id), None)
            if not driver:
                continue

            # Sort locked loads by status-specific time field
            locked_loads.sort(key=self._locked_load_sort_key)

            if driver.driver_id not in self.routes:
                self.routes[driver.driver_id] = DriverRoute(driver=driver)

            route = self.routes[driver.driver_id]

            for load in locked_loads:
                # All loads reaching this loop are in LOCKED_STATUSES (status 10–26).
                # They are physically committed — the driver is already dispatched,
                # en-route, or has completed the delivery.
                #
                # Do NOT apply the 5-load cap here and do NOT call _simulate_route.
                #
                # Cap problem: pre_assigned_count counts ALL status>1 loads for both
                # dispatch_date AND dispatch_date+1.  A non-overnight driver with
                # 4 in-motion loads today and 1 status=2 load pre-planned for tomorrow
                # would have pre_assigned_count=5, causing the cap to fire immediately
                # and seed ZERO loads — leaving all dispatched work as unnumbered cards.
                #
                # Simulation problem: _simulate_route returns None whenever terminal/
                # site geo data is missing or shift_end is exceeded (e.g. overnight
                # loads spanning midnight break the timing math).  For already-committed
                # loads these constraints don't apply; the load MUST appear in
                # dispatch_results regardless.
                #
                # Solution: directly append a bare RouteStop.  Timing fields are
                # populated from CE data (delivery_eta / completed_delivery_time).
                bare_stop = RouteStop(
                    ce_id=load.ce_id,
                    sequence=len(route.stops),
                    terminal=load.terminal or Terminal("", "", 0.0, 0.0),
                    site=load.site or Site(0, "", 0.0, 0.0),
                    arrive_site=load.delivery_eta,
                    depart_site=(
                        load.completed_delivery_time
                        if load.load_status == STATUS_DELIVERED
                        else load.delivery_eta
                    ),
                )
                route.stops.append(bare_stop)
                log.debug(
                    f"Seeded locked ce_id={load.ce_id} status={load.load_status} "
                    f"driver={driver_id} seq={bare_stop.sequence}"
                )

    # ---- route scoring ----

    @staticmethod
    def _route_score(route: DriverRoute) -> float:
        """Lower is better: penalises deadhead, rewards loaded miles."""
        return route.total_empty_miles - route.total_loaded_miles

    # ---- 2-opt / local-search improvement ----

    def _loads_for_driver(self, driver_id: int) -> list[Load]:
        """Return the ordered list of Load objects currently on a driver's route."""
        route = self.routes.get(driver_id)
        if not route:
            return []
        return [self._find_load_by_ce(s.ce_id) for s in route.stops]

    def _is_locked(self, load: Load) -> bool:
        """Loads with in-progress statuses must not be moved."""
        return load.load_status in LOCKED_STATUSES

    def _try_build_route(self, driver: Driver, loads: list[Load]) -> Optional[DriverRoute]:
        """Simulate a route for `driver` with exactly these loads (in order)."""
        if not loads:
            return None
        stops = [(l, i) for i, l in enumerate(loads)]
        return self._simulate_route(driver, stops)

    def _two_opt_pass(self) -> bool:
        """
        One full pass over all driver pairs.  Tries:
          - SWAP  : swap one load from driver A with one load from driver B
          - MOVE  : move one load from driver A to driver B (all insert positions)

        Returns True if at least one improving move was accepted (caller should
        keep looping), False when the solution is locally optimal.

        Only non-locked loads are touched.  Eligibility checks (terminal access,
        pump cert, restrictions) are enforced implicitly — _simulate_route
        returns None for any infeasible combination, so invalid swaps are
        naturally rejected.
        """
        driver_ids = list(self.routes.keys())
        improved = False

        for i in range(len(driver_ids)):
            for j in range(i + 1, len(driver_ids)):
                did_a = driver_ids[i]
                did_b = driver_ids[j]
                driver_a = self.routes[did_a].driver
                driver_b = self.routes[did_b].driver

                loads_a = self._loads_for_driver(did_a)
                loads_b = self._loads_for_driver(did_b)

                # Current combined score
                base_score = (
                    self._route_score(self.routes[did_a])
                    + self._route_score(self.routes[did_b])
                )

                # ---- SWAP: swap load ia from A with load jb from B ----
                for ia, load_a in enumerate(loads_a):
                    if self._is_locked(load_a):
                        continue
                    for ib, load_b in enumerate(loads_b):
                        if self._is_locked(load_b):
                            continue

                        new_a_loads = loads_a[:ia] + [load_b] + loads_a[ia + 1:]
                        new_b_loads = loads_b[:ib] + [load_a] + loads_b[ib + 1:]

                        # Eligibility: driver must have terminal access for the
                        # swapped-in load (other hard checks handled by simulate)
                        if self._check_driver_eligible(driver_a, load_b):
                            continue
                        if self._check_driver_eligible(driver_b, load_a):
                            continue

                        route_a = self._try_build_route(driver_a, new_a_loads)
                        if route_a is None:
                            continue
                        route_b = self._try_build_route(driver_b, new_b_loads)
                        if route_b is None:
                            continue

                        new_score = self._route_score(route_a) + self._route_score(route_b)
                        if new_score < base_score - 0.01:  # tiny epsilon avoids float noise
                            log.debug(
                                "2-opt SWAP: drivers %s↔%s load ce%s↔ce%s "
                                "score %.2f→%.2f",
                                did_a, did_b, load_a.ce_id, load_b.ce_id,
                                base_score, new_score,
                            )
                            self.routes[did_a] = route_a
                            self.routes[did_b] = route_b
                            # Refresh locals for the rest of this pair's inner loop
                            loads_a = self._loads_for_driver(did_a)
                            loads_b = self._loads_for_driver(did_b)
                            base_score = new_score
                            improved = True

                # ---- MOVE: relocate one load from A → B ----
                for ia, load_a in enumerate(loads_a):
                    if self._is_locked(load_a):
                        continue
                    if len(loads_a) == 1:
                        # Moving the only load would leave A empty — skip
                        # (the engine doesn't track empty routes)
                        continue
                    if len(loads_b) >= 4:
                        continue  # B is already at max capacity

                    if self._check_driver_eligible(driver_b, load_a):
                        continue

                    new_a_loads = loads_a[:ia] + loads_a[ia + 1:]
                    route_a = self._try_build_route(driver_a, new_a_loads)
                    if route_a is None:
                        continue

                    # Try every insert position in B
                    for ib in range(len(loads_b) + 1):
                        new_b_loads = loads_b[:ib] + [load_a] + loads_b[ib:]
                        route_b = self._try_build_route(driver_b, new_b_loads)
                        if route_b is None:
                            continue

                        new_score = self._route_score(route_a) + self._route_score(route_b)
                        if new_score < base_score - 0.01:
                            log.debug(
                                "2-opt MOVE: load ce%s driver %s→%s "
                                "score %.2f→%.2f",
                                load_a.ce_id, did_a, did_b, base_score, new_score,
                            )
                            self.routes[did_a] = route_a
                            self.routes[did_b] = route_b
                            loads_a = self._loads_for_driver(did_a)
                            loads_b = self._loads_for_driver(did_b)
                            base_score = new_score
                            improved = True
                            break  # restart inner ia loop with fresh loads_a

        return improved

    def _run_two_opt(self):
        """Drive the 2-opt loop until convergence or the iteration cap."""
        for iteration in range(1, MAX_OPT_ITERS + 1):
            if not self._two_opt_pass():
                log.info("2-opt converged after %d iteration(s)", iteration)
                return
        log.info("2-opt reached MAX_OPT_ITERS (%d) without full convergence", MAX_OPT_ITERS)

    def _driver_finish_time(self, driver: Driver) -> datetime:
        """Latest depart_site time on the driver's current route, or shift start if empty."""
        route = self.routes.get(driver.driver_id)
        if route and route.stops:
            return route.stops[-1].depart_site or self._shift_start(driver)
        return self._shift_start(driver)

    def _retry_shift_exceeded(self):
        """
        Retry pass for loads that failed exclusively due to timing/capacity.

        Loads are eligible for retry when every failure reason collected during
        the greedy pass was "Shift time exceeded." — meaning no hard blocker
        (terminal access, pump cert, site restriction) prevented assignment.
        After the 2-opt pass may have rebalanced routes, a slot that didn't
        exist before might now be available.

        Candidate drivers are sorted by finish time (lightest route first) so
        the load is tried against the driver most likely to absorb it before
        falling back to busier ones.
        """
        # Separate retryable from permanently unassigned
        retryable: list[tuple] = []
        permanent: list[tuple] = []
        for entry in self.unassigned:
            load, reason, category = entry
            if reason == "Shift time exceeded.":
                retryable.append(entry)
            else:
                permanent.append(entry)

        if not retryable:
            return

        log.info("Retry pass: %d load(s) failed only due to timing — retrying", len(retryable))

        # Drivers sorted by finish time ascending (earliest finish = most room)
        candidate_drivers = sorted(
            self.drivers,
            key=lambda d: (
                len(self.routes.get(d.driver_id, DriverRoute(driver=d)).stops),
                self._driver_finish_time(d),
            ),
        )

        still_unassigned: list[tuple] = []

        for load, reason, category in retryable:
            best_route = None
            best_driver = None
            best_score = float("inf")

            for driver in candidate_drivers:
                elig_fail = self._check_driver_eligible(driver, load)
                if elig_fail:
                    continue

                if not driver.yard_location:
                    continue

                current_route = self.routes.get(driver.driver_id)
                current_stops = []
                if current_route:
                    if len(current_route.stops) >= 4:
                        continue
                    current_stops = [
                        (self._find_load_by_ce(s.ce_id), s.sequence)
                        for s in current_route.stops
                    ]

                for pos in range(len(current_stops) + 1):
                    if self._check_diesel_wet_sequence(driver, load, pos):
                        continue

                    candidate_stops = current_stops[:pos] + [(load, pos)] + current_stops[pos:]
                    candidate_stops = [(l, i) for i, (l, _) in enumerate(candidate_stops)]

                    simulated = self._simulate_route(driver, candidate_stops)
                    if simulated is None:
                        continue

                    score = simulated.total_empty_miles - simulated.total_loaded_miles
                    if score < best_score:
                        best_score = score
                        best_route = simulated
                        best_driver = driver

            if best_driver and best_route:
                log.info(
                    "Retry pass: load ce%s assigned to driver %s on retry",
                    load.ce_id, best_driver.driver_id,
                )
                self.routes[best_driver.driver_id] = best_route
            else:
                still_unassigned.append((load, reason, category))

        rescued = len(retryable) - len(still_unassigned)
        if rescued:
            log.info("Retry pass: rescued %d load(s) from unassigned", rescued)

        self.unassigned = permanent + still_unassigned

    # ---- main run ----

    def run(self) -> DispatchResult:
        start_ms = time_mod.time()
        run_id = str(uuid.uuid4())

        # Resolve site/terminal on each load
        for load in self.loads:
            load.site = self.sites.get(load.site_id)
            load.terminal = self.terminals.get(load.terminal_id)

        # Build driver name → driver_id lookup so loads with no numeric
        # assigned_driver_id can be resolved from first_name/last_name fields.
        name_to_driver: dict[tuple[str, str], int] = {
            (d.first_name.strip().lower(), d.last_name.strip().lower()): d.driver_id
            for d in self.drivers
        }

        # Resolve assigned_driver_id for every load that has a driver name but no ID.
        for load in self.loads:
            if not load.assigned_driver_id:
                fn = (load.assigned_driver_first or "").strip().lower()
                ln = (load.assigned_driver_last or "").strip().lower()
                if fn or ln:
                    load.assigned_driver_id = name_to_driver.get((fn, ln))

        # Filter deliverable loads (today ± 1 day).
        # Only route loads that are unscheduled (status=1) or have no status set —
        # anything above status 1 is already in motion (dispatched, en route, delivered)
        # and belongs in the pre-assigned panel, not the routing queue.
        today = self.dispatch_date
        eligible_loads = [
            l for l in self.loads
            if l.delivery_date and 0 <= (date.fromisoformat(l.delivery_date) - today).days <= 1
            and (l.load_status == 1)  # only route unscheduled; 0=deleted, >1=in progress
        ]

        # Validate each load
        valid_loads = []
        for load in eligible_loads:
            if not load.site:
                self.unassigned.append((load, "No feasible assignment: Site location unavailable.", "site"))
                continue
            if not load.site.latitude or not load.site.longitude:
                self.unassigned.append((load, "No feasible assignment: Site location unavailable.", "site"))
                continue
            if not load.terminal:
                # Allow through if the site has alternate terminals that are resolved
                has_alternates = any(
                    self.terminals.get(t) for t in (load.site.alternate_terminal_ids or [])
                )
                if not has_alternates:
                    self.unassigned.append((load, "No eligible terminal: Terminal location unavailable.", "terminal"))
                    continue
            valid_loads.append(load)

        sorted_loads = self._sort_loads(valid_loads)

        # Always seed locked/pre-assigned loads onto their drivers first so that
        # capacity is accounted for before routing unscheduled loads.
        load_map = {l.ce_id: l for l in self.loads}
        self._seed_locked_loads(load_map)

        # Snapshot seeded stop counts per driver.
        # The greedy loop uses this to track only *newly added* loads so that
        # CE pre-assigned loads and routed loads are never double-counted when
        # enforcing the 5-load cap.
        initial_seeded: dict[int, int] = {
            did: len(r.stops) for did, r in self.routes.items()
        }

        # Lock boundary per driver: the first insertion index that is safe for
        # a new load.  Any seeded stop with load_status > 10 (en-route, at-site,
        # delivered) is already physically in motion — inserting a new load before
        # or between those stops is impossible.  New loads may only be appended
        # AFTER the last such stop.
        # Status 10 (dispatched) is NOT considered locked here because the driver
        # hasn't left the terminal yet and scheduling around it is still valid.
        lock_boundaries: dict[int, int] = {}
        for driver_id, route in self.routes.items():
            last_locked_idx = -1
            for i, stop in enumerate(route.stops):
                seeded_load = load_map.get(stop.ce_id)
                if seeded_load and seeded_load.load_status > 10:
                    last_locked_idx = i
            lock_boundaries[driver_id] = last_locked_idx + 1

        assigned_ce_ids = set()
        for route in self.routes.values():
            for stop in route.stops:
                assigned_ce_ids.add(stop.ce_id)

        remaining_loads = [l for l in sorted_loads if l.ce_id not in assigned_ce_ids]

        for load in remaining_loads:
            failure_reasons = []
            # Separate reasons from drivers who could actually access the terminal vs.
            # those who failed the terminal-access check (zero-terminal or wrong terminal).
            # This prevents "no terminal access" from masking the real failure reason
            # when some drivers DO have access but fail for another reason.
            terminal_eligible_reasons = []
            best_route = None
            best_driver = None
            best_score = float("inf")

            sorted_drivers = self._sort_drivers(
                self.drivers,
                reroute_driver_id=load.assigned_driver_id if self.reroute else None,
            )

            # Determine if this is a next-day load (delivery_date = tomorrow)
            load_delivery_date = date.fromisoformat(load.delivery_date) if load.delivery_date else self.dispatch_date
            is_next_day_load = load_delivery_date > self.dispatch_date

            for driver in sorted_drivers:
                # Route locking: in reroute mode, skip drivers who have already
                # clocked out — their shift is complete and no new loads should be added.
                if self.reroute and driver.route_finish_time:
                    continue

                # Next-day load eligibility: only overnight drivers (shift extends past
                # midnight) should receive loads with delivery_date = tomorrow.
                # This prevents same-day drivers from delivering a next-day load early.
                if is_next_day_load:
                    shift_end = self._shift_end(driver)
                    next_midnight = datetime(
                        self.dispatch_date.year,
                        self.dispatch_date.month,
                        self.dispatch_date.day,
                    ) + timedelta(days=1)
                    if shift_end <= next_midnight:
                        failure_reasons.append("Delivery window missed.")
                        terminal_eligible_reasons.append("Delivery window missed.")
                        continue

                # Resolve terminal — try primary first, then site alternates
                viable_terminal = self._get_viable_terminal(driver, load)
                if not viable_terminal:
                    if not driver.terminal_ids:
                        failure_reasons.append("No eligible terminal: Driver has no terminal access.")
                    else:
                        failure_reasons.append("No eligible terminal: Driver restricted from this terminal.")
                    continue

                # Hard static checks (terminal access already resolved above)
                elig_fail = self._check_driver_eligible(driver, load)
                if elig_fail:
                    failure_reasons.append(elig_fail)
                    terminal_eligible_reasons.append(elig_fail)
                    continue

                if not driver.yard_location:
                    failure_reasons.append("Driver unavailable.")
                    terminal_eligible_reasons.append("Driver unavailable.")
                    continue

                current_route = self.routes.get(driver.driver_id)
                current_stops = []
                current_total = len(current_route.stops) if current_route else 0
                seeds = initial_seeded.get(driver.driver_id, 0)
                # Loads added by this routing run (excludes CE locks already seeded).
                newly_added = current_total - seeds
                # Available new-load slots = 5 minus CE pre-assigned count.
                # CE locks that were seeded don't reduce this budget (they're
                # already represented in pre_assigned_count).
                available_new_slots = max(0, 5 - driver.pre_assigned_count)
                if newly_added >= available_new_slots:
                    failure_reasons.append("Shift time exceeded.")
                    terminal_eligible_reasons.append("Shift time exceeded.")
                    continue
                if current_route:
                    current_stops = [
                        (self._find_load_by_ce(s.ce_id), s.sequence)
                        for s in current_route.stops
                    ]

                # If routing via an alternate terminal, use a shallow copy of the load
                # so the simulation uses the correct terminal without mutating the original.
                if viable_terminal.terminal_id != (load.terminal_id or ""):
                    working_load = copy(load)
                    working_load.terminal = viable_terminal
                    working_load.terminal_id = viable_terminal.terminal_id
                    log.debug(
                        f"ce_id={load.ce_id}: using alternate terminal "
                        f"{viable_terminal.terminal_id} for driver {driver.driver_id}"
                    )
                else:
                    working_load = load

                # Try inserting this load at each valid position.
                # Positions before the lock boundary are off-limits — the driver
                # is already past those stops (en-route / at-site / delivered).
                lock_pos = lock_boundaries.get(driver.driver_id, 0)
                insert_positions = list(range(lock_pos, len(current_stops) + 1))
                for pos in insert_positions:
                    diesel_wet_fail = self._check_diesel_wet_sequence(driver, working_load, pos)
                    if diesel_wet_fail:
                        failure_reasons.append(diesel_wet_fail)
                        terminal_eligible_reasons.append(diesel_wet_fail)
                        continue

                    candidate_stops = current_stops[:pos] + [(working_load, pos)] + current_stops[pos:]
                    candidate_stops = [(l, i) for i, (l, _) in enumerate(candidate_stops)]

                    simulated = self._simulate_route(driver, candidate_stops)
                    if simulated is None:
                        failure_reasons.append("Shift time exceeded.")
                        terminal_eligible_reasons.append("Shift time exceeded.")
                        continue

                    # Score: lower loaded-to-empty ratio = better (maximize loaded)
                    score = simulated.total_empty_miles - simulated.total_loaded_miles
                    if score < best_score:
                        best_score = score
                        best_route = simulated
                        best_driver = driver

            if best_driver and best_route:
                self.routes[best_driver.driver_id] = best_route
            else:
                # Prefer the most-informative reasons from drivers that passed terminal checks;
                # fall back to the full list only if no terminal-eligible driver was tried.
                reason_pool = terminal_eligible_reasons if terminal_eligible_reasons else failure_reasons
                reasons = reason_pool or ["No feasible assignment."]
                best_reason = min(reasons, key=reason_priority)
                self.unassigned.append((load, best_reason, "unassigned"))

        # ---- retry pass: timing-only failures ----
        # Must run before 2-opt so the improvement loop can also optimise
        # any loads rescued here.
        self._retry_shift_exceeded()

        # ---- 2-opt improvement pass ----
        # Only worth running if at least two drivers have loads to swap/move.
        if sum(1 for r in self.routes.values() if r.stops) >= 2:
            log.info("Starting 2-opt local search over %d driver routes", len(self.routes))
            self._run_two_opt()

        duration_ms = int((time_mod.time() - start_ms) * 1000)

        return DispatchResult(
            dispatch_date=str(self.dispatch_date),
            run_type="reroute" if self.reroute else "dispatch",
            run_id=run_id,
            driver_routes=list(self.routes.values()),
            unassigned=self.unassigned,
            total_loads=len(eligible_loads),
            assigned_loads=sum(len(r.stops) for r in self.routes.values()),
            unassigned_loads=len(self.unassigned),
            run_duration_ms=duration_ms,
        )
