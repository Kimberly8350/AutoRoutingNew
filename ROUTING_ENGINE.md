# AutoRouting Engine — Algorithm Reference

## Overview

The routing engine is a **greedy insertion algorithm** that assigns fuel delivery loads to drivers one load at a time, in priority order. For each load, it tries every eligible driver and every possible insertion position in that driver's existing route, then picks the combination that maximizes loaded driving efficiency. It does not backtrack — once a load is assigned it stays assigned unless a Reroute is triggered.

---

## Inputs

| Input | Source | Description |
|---|---|---|
| Drivers | `driver_schedules` (Supabase) | Filtered: `attendance_expected=1`, valid `board_location`, valid `yard` |
| Loads | `load_details` (Supabase, synced from CE Connect) | All loads for the dispatch date |
| Sites | `site_details` (Supabase) | Delivery locations with lat/lon, pump cert flag |
| Terminals | `terminal_locations` (Supabase) | Rack/terminal locations with lat/lon, diesel-wet flag |
| Yards | `yard_locations` (Supabase) | Driver home yards with lat/lon |

---

## Execution Order

### Step 1 — Resolve & Filter Loads

- Site and terminal objects are attached to each load by ID
- Any load with a driver name but no numeric driver ID is resolved via name lookup
- **Eligible loads**: delivery date is today or tomorrow (+1 day), `load_status = 1` (Unscheduled only)
  - Status 0 = deleted (excluded at sync)
  - Status > 1 = already in motion — goes to pre-assigned panel, not the routing queue
- Loads missing a site, terminal, or coordinates are moved to **Unassigned** immediately

### Step 2 — Sort Loads by Priority

Loads are processed in this order — a load higher on this list is attempted before one lower:

| Priority | Criterion | Rationale |
|---|---|---|
| 1st | Timed loads before any-time loads | Deliveries with a specific window must be placed first while time slots are open |
| 2nd | Earliest delivery date | Older/sooner loads take precedence |
| 3rd | Earliest window start time | Within the same date, earlier windows go first |
| 4th | Minimize unloaded miles | Minimize the amount of time a truck is not loaded with fuel |

> **Any-time load**: a load whose window is 00:00–23:xx, or has no window at all. These are flexible and scheduled last.
> **Note**: Any-time loads should be treated as unscheduled if there are time constraints with loads that have time windows.

### Step 3 — Seed Pre-Assigned & In-Progress Loads

Before routing any unscheduled loads, the engine pins loads already in progress onto their drivers. This accounts for real capacity before new assignments are made.

**Locked statuses** (pinned to their driver):
- `10` — Assigned
- `12` — En Route to Rack
- `20` — At Rack
- `22` — En Route to Site
- `24` — At Site
- `26` — Delivered

Within each driver, locked loads are sorted: Delivered (by completed time) → In-progress (by ETA) → Assigned (by ETA).

### Step 4 — Greedy Assignment Loop

For each unscheduled load (in priority order from Step 2):

1. **Sort drivers** — try drivers in this order:
   - In Reroute mode: the load's originally-assigned driver is tried first
   - Fewest existing stops (spread work before stacking)
   - Earliest last-stop departure time (most-available driver first)
   - Driver ID as tiebreaker

2. **Hard eligibility checks** (instant disqualify, no simulation):
   - Pump certification: site requires it, driver doesn't have it → skip
   - Terminal access: driver's approved terminal list must include this load's terminal → skip if not
   - Site restriction: driver-specific delivery site blocks → skip
   - Customer group restriction: driver-specific customer group blocks → skip
   - Yard location: driver must have a resolved yard with coordinates → skip

3. **Capacity check**: drivers are capped at **5 stops per day**. If already at 5, skip.

4. **Insertion position loop**: try inserting the load at every position in the driver's current sequence (before stop 1, between 1 and 2, after last stop, etc.)
   - At each position: check **diesel-wet sequencing rule** (see below)
   - Run a full **route simulation** for the candidate sequence

5. **Score the simulation**: `score = total empty miles − total loaded miles`
   - Lower score = better (minimizes deadhead, maximizes productive loaded driving)
   - Track the best-scoring driver + position across all candidates

6. **Assign** the load to the best driver/position. If no valid assignment exists, move the load to **Unassigned** with the highest-priority failure reason.

---

## Route Simulation

Every candidate assignment is fully time-simulated before being accepted. The simulation traces the driver's complete day:

```
Yard (shift start)
  → Drive empty to Terminal  [Google Maps / haversine]
  → Load at Terminal         [45 min fixed]
  → Drive loaded to Site     [Google Maps / haversine]
  → Wait if arriving early   [up to window_start − 2 hours]
  → Deliver at Site          [45 min fixed]
  → (repeat for each stop)
  → Drive empty back to Yard
```

**Hard timing constraints** — simulation returns failure if any of these are violated:
- Driver arrives at terminal after shift end
- Driver arrives at site more than **4 hours past** window end
- Driver arrives at site after shift end
- Driver returns to yard after shift end

**Window tolerance**:
- Up to **2 hours early**: driver waits at or near the site — allowed
- Up to **4 hours late**: acceptable, load is still assigned
- More than **4 hours late**: delivery window missed — simulation fails

---

## Travel Time

| Method | When Used |
|---|---|
| Google Maps Routes API (traffic-aware, hazmat) | Primary — called with expected departure epoch for real-time traffic |
| Haversine at 50 mph constant | Fallback if Maps API is unavailable or errors |

All routing uses hazmat vehicle settings (avoids ferries, traffic-aware, no tunnel restrictions currently set).

---

## Diesel-Wet Sequencing Rule

Some terminals are flagged `is_diesel_wet = 1`. When a load is being inserted after an existing stop at a diesel-wet terminal, the preceding load must satisfy all three conditions:

| Condition | Required |
|---|---|
| Has diesel product | ✓ Yes |
| Has gasoline product (Regular, MidGrade, Super, Gas-Other) | ✗ No |
| Has dyed diesel product | ✗ No |

If any condition fails, that insertion position is skipped. This prevents product contamination from an incompatible prior load.

---

## Scoring Function

```
score = total_empty_miles − total_loaded_miles
```

The engine picks the **lowest score** (most negative = most loaded relative to empty). This prioritizes:
- Drivers/positions that require less deadhead driving to reach the terminal
- Routes where the terminal is close to the driver's current position
- Efficiency of the overall route shape

---

## Capacity & Shift Limits

| Parameter | Value |
|---|---|
| Max stops per driver per day | 5 |
| Max shift hours | Per driver (default 12 hrs, stored in `driver_schedules`) |
| Load service time (terminal) | 45 minutes |
| Unload service time (site) | 45 minutes |

---

## Unassigned Load Reasons (Priority Order)

When a load cannot be assigned, the highest-priority failure reason is reported:

| # | Reason |
|---|---|
| 1 | No eligible driver: No active working driver |
| 2 | Driver unavailable |
| 3 | No eligible terminal: Driver restricted from this terminal |
| 4 | No eligible terminal: Driver has no terminal access |
| 5 | No eligible terminal: Terminal location unavailable |
| 6 | No feasible assignment: Pump certification required |
| 7 | No feasible assignment: Diesel-wet sequencing conflict |
| 8 | Delivery window missed |
| 9 | Shift time exceeded |
| 10 | No feasible assignment: Driver restricted from this site |
| 11 | No feasible assignment: Site location unavailable |
| 12 | Invalid input data |
| 13 | No feasible assignment (catch-all) |

---

## Reroute Mode

When **Reroute** is triggered instead of a fresh dispatch:
- Locked loads (statuses 10–26) remain pinned to their drivers and are not re-assigned
- Only `status=1` (Unscheduled) loads are re-routed
- The originally-assigned driver for each load is tried first before other candidates
- Everything else (scoring, simulation, constraints) behaves identically to a fresh dispatch

---

## What the Engine Does NOT Do

- **No backtracking**: once a load is assigned it is not reconsidered for a better global solution
- **No multi-trip optimization**: each route is optimized locally per driver, not globally across all drivers
- **No load splitting**: a load is always assigned to exactly one driver
- **No vehicle capacity constraint**: gallons per load are not checked against trailer capacity (assumed handled operationally)
- **No multi-day planning**: only loads for today and tomorrow are considered
