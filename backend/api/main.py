"""
AutoRouting FastAPI Backend
"""

import logging
import os
from datetime import date, datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

from engine.data_loader import (
    load_yards, load_terminals, load_sites,
    load_drivers_for_date, load_loads_for_date,
)
from engine.routing_engine import RoutingEngine

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

app = FastAPI(title="AutoRouting API", version="1.0.0")

# IMPORTANT: middleware ordering matters.
# Starlette builds the stack as: ServerErrorMiddleware → [user middlewares reversed] → ExceptionMiddleware → Routes
# add_middleware() prepends, so the LAST add_middleware call ends up OUTERMOST of user middlewares.
#
# We need CORSMiddleware to be OUTERMOST so it wraps the error-catching middleware.
# Therefore: add error catcher first (via @app.middleware), then add CORSMiddleware.
#
# Resulting stack: Server → CORS → catch_exceptions → ExceptionMiddleware → Routes
# Errors from routes are caught inside CORS, so responses get CORS headers. ✓

@app.middleware("http")
async def catch_unhandled_exceptions(request: Request, call_next):
    """Catch unhandled exceptions inside CORSMiddleware so 500s include CORS headers."""
    try:
        return await call_next(request)
    except Exception as exc:
        log.exception(f"Unhandled exception on {request.method} {request.url}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

# Add CORS last so it is outermost (wraps everything above)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ---- Auth middleware ----
async def verify_token(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = authorization.split(" ")[1]
    try:
        anon_key = os.getenv("SUPABASE_ANON_KEY") or SUPABASE_SERVICE_KEY
        client = create_client(SUPABASE_URL, anon_key)
        user = client.auth.get_user(token)
        return user.user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---- Pydantic schemas ----
class DispatchRequest(BaseModel):
    dispatch_date: str  # YYYY-MM-DD
    reroute: bool = False


class AddTerminalAccessRequest(BaseModel):
    driver_id: int
    terminal_id: str  # ODBC string, e.g. "T-01-TX-0001"


class RemoveTerminalAccessRequest(BaseModel):
    driver_id: int
    terminal_id: str  # ODBC string, e.g. "T-01-TX-0001"


class AddRestrictionRequest(BaseModel):
    driver_id: int
    restriction_type: str  # 'site' or 'customer'
    site_id: Optional[int] = None
    customer_group_name: Optional[str] = None
    notes: Optional[str] = None


class AddLoadRequest(BaseModel):
    ce_id: int
    delivery_date: str
    customer_name: str
    site_id: int
    terminal_id: int
    terminal_name: str
    product_name: str
    gross_gallons: float
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    order_number: Optional[str] = None


# ---- Health ----
@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ---- Dispatch ----
@app.post("/api/dispatch/run")
async def run_dispatch(
    req: DispatchRequest,
    user=Depends(verify_token),
):
    """Run the routing engine for a given date."""
    try:
        dispatch_date = date.fromisoformat(req.dispatch_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        client = get_supabase()

        # Load all reference data
        yards = load_yards(client)
        log.info(f"run_dispatch: loaded {len(yards)} yards")
        terminals = load_terminals(client)
        log.info(f"run_dispatch: loaded {len(terminals)} terminals")
        sites = load_sites(client)
        log.info(f"run_dispatch: loaded {len(sites)} sites")
        drivers = load_drivers_for_date(client, dispatch_date, yards)
        log.info(f"run_dispatch: loaded {len(drivers)} drivers")
        loads = load_loads_for_date(client, dispatch_date)
        log.info(f"run_dispatch: loaded {len(loads)} loads")
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"run_dispatch data loading error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not drivers:
        raise HTTPException(status_code=422, detail="No active drivers found for this date.")

    engine = RoutingEngine(
        drivers=drivers,
        loads=loads,
        sites=sites,
        terminals=terminals,
        yards=yards,
        dispatch_date=dispatch_date,
        reroute=req.reroute,
    )

    # Run engine in a thread pool so asyncio's event loop is NOT running inside
    # the engine — this allows get_travel_mins_sync to call the Google Maps API
    # via loop.run_until_complete() rather than always falling back to haversine.
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, engine.run)
    except Exception as e:
        log.exception(f"run_dispatch engine error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Await persist so results are in Supabase before the frontend fetches the board
    await persist_dispatch_result(client, result, user)

    return {
        "run_id": result.run_id,
        "dispatch_date": result.dispatch_date,
        "run_type": result.run_type,
        "total_loads": result.total_loads,
        "assigned_loads": result.assigned_loads,
        "unassigned_loads": result.unassigned_loads,
        "run_duration_ms": result.run_duration_ms,
        "routes": _serialize_routes(result),
        "unassigned": _serialize_unassigned(result),
    }


def _serialize_routes(result):
    out = []
    for route in result.driver_routes:
        d = route.driver
        for stop in route.stops:
            out.append({
                "driver_id": d.driver_id,
                "driver_name": f"{d.first_name} {d.last_name}",
                "board_location": d.board_location,
                "sequence": stop.sequence,
                "ce_id": stop.ce_id,
                "terminal_name": stop.terminal.terminal_name if stop.terminal else "",
                "terminal_abbreviation": stop.terminal.abbreviation if stop.terminal else "",
                "site_name": stop.site.site_name if stop.site else "",
                "site_city": stop.site.city if stop.site else "",
                "arrive_terminal": stop.arrive_terminal.isoformat() if stop.arrive_terminal else None,
                "depart_terminal": stop.depart_terminal.isoformat() if stop.depart_terminal else None,
                "arrive_site": stop.arrive_site.isoformat() if stop.arrive_site else None,
                "depart_site": stop.depart_site.isoformat() if stop.depart_site else None,
                "loaded_miles": round(stop.loaded_miles, 2),
                "empty_miles": round(stop.empty_miles, 2),
                "total_loaded_miles": round(route.total_loaded_miles, 2),
                "total_empty_miles": round(route.total_empty_miles, 2),
            })
    return out


def _serialize_unassigned(result):
    return [
        {
            "ce_id": load.ce_id,
            "site_name": load.site_name,
            "customer_name": load.customer_name,
            "reason": reason,
            "category": category,
        }
        for load, reason, category in result.unassigned
    ]


async def persist_dispatch_result(client: Client, result, user):
    """Save dispatch results to Supabase."""
    import uuid
    run_id = result.run_id
    dispatch_date = result.dispatch_date

    try:
        # Deactivate prior runs for this date
        client.table("dispatch_runs") \
            .update({"is_active": False}) \
            .eq("dispatch_date", dispatch_date) \
            .execute()

        # Insert run record
        client.table("dispatch_runs").insert({
            "run_id": run_id,
            "dispatch_date": dispatch_date,
            "run_type": result.run_type,
            "total_loads": result.total_loads,
            "assigned_loads": result.assigned_loads,
            "unassigned_loads": result.unassigned_loads,
            "run_duration_ms": result.run_duration_ms,
            "run_by": user.id if user else None,
            "is_active": True,
        }).execute()

        # Delete old results for this date
        client.table("dispatch_results").delete().eq("dispatch_date", dispatch_date).execute()
        client.table("unassigned_loads").delete().eq("dispatch_date", dispatch_date).execute()

        # Insert new dispatch results
        dispatch_rows = []
        for route in result.driver_routes:
            d = route.driver
            for stop in route.stops:
                dispatch_rows.append({
                    "dispatch_date": dispatch_date,
                    "run_id": run_id,
                    "board_location": d.board_location,
                    "driver_id": d.driver_id,
                    "driver_name": f"{d.first_name} {d.last_name}",
                    "route_sequence": stop.sequence,
                    "ce_id": stop.ce_id,
                    "site_name": stop.site.site_name if stop.site else "",
                    "site_city": stop.site.city if stop.site else "",
                    "terminal_name": stop.terminal.terminal_name if stop.terminal else "",
                    "eta": stop.arrive_site.isoformat() if stop.arrive_site else None,
                    "drive_to_terminal_mins": stop.drive_to_terminal_mins,
                    "drive_to_site_mins": stop.drive_to_site_mins,
                    "total_loaded_miles": route.total_loaded_miles,
                    "total_empty_miles": route.total_empty_miles,
                })
        if dispatch_rows:
            for i in range(0, len(dispatch_rows), 100):
                client.table("dispatch_results").insert(dispatch_rows[i:i+100]).execute()

        # Insert unassigned
        unassigned_rows = [
            {
                "dispatch_date": dispatch_date,
                "run_id": run_id,
                "ce_id": load.ce_id,
                "site_name": load.site_name,
                "reason": reason,
                "reason_category": category,
            }
            for load, reason, category in result.unassigned
        ]
        if unassigned_rows:
            client.table("unassigned_loads").insert(unassigned_rows).execute()

        log.info(f"Persisted dispatch run {run_id}")
    except Exception as e:
        log.error(f"Failed to persist dispatch: {e}")
        raise


# ---- Loads ----
@app.get("/api/loads")
def get_loads(dispatch_date: str, user=Depends(verify_token)):
    client = get_supabase()
    try:
        rows = client.table("load_details").select("*").eq("delivery_date", dispatch_date).execute().data
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/loads")
def add_load(req: AddLoadRequest, user=Depends(verify_token)):
    client = get_supabase()
    try:
        client.table("load_details").upsert(req.dict()).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/loads/{ce_id}")
def delete_load(ce_id: int, user=Depends(verify_token)):
    client = get_supabase()
    client.table("load_details").delete().eq("ce_id", ce_id).execute()
    return {"status": "ok"}


# ---- Drivers ----
@app.get("/api/drivers")
def get_drivers(dispatch_date: str, user=Depends(verify_token)):
    client = get_supabase()
    rows = (
        client.table("driver_schedules")
        .select("*")
        .eq("shift_date", dispatch_date)
        .not_.is_("board_location", "null")
        .neq("board_location", "")
        .not_.is_("yard", "null")
        .neq("yard", "")
        .execute()
        .data
    )
    try:
        inactive = client.table("driver_inactive").select("driver_id").execute().data
        inactive_ids = {r["driver_id"] for r in inactive}
        if inactive_ids:
            rows = [r for r in rows if r.get("driver_id") not in inactive_ids]
    except Exception:
        pass
    return rows


@app.get("/api/drivers/inactive")
def get_inactive_drivers(user=Depends(verify_token)):
    client = get_supabase()
    rows = client.table("driver_inactive").select("*").order("last_name").execute().data
    return rows


@app.post("/api/drivers/{driver_id}/deactivate")
def deactivate_driver(driver_id: int, body: dict, user=Depends(verify_token)):
    client = get_supabase()
    client.table("driver_inactive").upsert({
        "driver_id": driver_id,
        "first_name": body.get("first_name", ""),
        "last_name": body.get("last_name", ""),
    }).execute()
    return {"status": "ok"}


@app.delete("/api/drivers/{driver_id}/deactivate")
def reactivate_driver(driver_id: int, user=Depends(verify_token)):
    client = get_supabase()
    client.table("driver_inactive").delete().eq("driver_id", driver_id).execute()
    return {"status": "ok"}


@app.patch("/api/drivers/{driver_id}/attendance")
def update_attendance(driver_id: int, body: dict, user=Depends(verify_token)):
    client = get_supabase()
    dispatch_date = body.get("shift_date")
    attendance = body.get("attendance_expected")
    client.table("driver_schedules") \
        .update({"attendance_expected": attendance}) \
        .eq("driver_id", driver_id) \
        .eq("shift_date", dispatch_date) \
        .execute()
    return {"status": "ok"}


# ---- Terminal Access ----
@app.get("/api/terminal-access")
def get_terminal_access(user=Depends(verify_token)):
    client = get_supabase()

    cards = client.table("driver_terminal_cards").select("driver_id,terminal_id").execute().data

    # Build terminal_id (ODBC string) → terminal info map
    term_rows = client.table("terminal_locations").select("terminal_id,terminal_name,terminal_abbreviation,terminal_abreviation").execute().data
    term_map = {
        str(t["terminal_id"]).strip(): t
        for t in term_rows if t.get("terminal_id")
    }

    # Build driver_id → name map from the most recent driver schedules available
    from datetime import date, timedelta
    driver_map: dict[int, dict] = {}
    for delta in range(0, 30):
        d = (date.today() - timedelta(days=delta)).isoformat()
        rows = client.table("driver_schedules").select("driver_id,first_name,last_name").eq("shift_date", d).execute().data
        for r in rows:
            did = r.get("driver_id")
            if did and int(did) not in driver_map:
                driver_map[int(did)] = r
        if len(driver_map) >= 30:  # enough drivers found
            break

    enriched = []
    for card in cards:
        tid = str(card.get("terminal_id") or "").strip()
        did = int(card.get("driver_id") or 0)
        term = term_map.get(tid, {})
        driver = driver_map.get(did, {})
        # Use terminal_abbreviation if set, fall back to terminal_abreviation (legacy typo column)
        abbr = term.get("terminal_abbreviation") or term.get("terminal_abreviation") or ""
        enriched.append({
            "driver_id": did,
            "terminal_id": tid,
            "terminal_name": term.get("terminal_name") or f"Terminal {tid}",
            "terminal_abbreviation": abbr,
            "first_name": driver.get("first_name") or "",
            "last_name": driver.get("last_name") or "",
        })
    return enriched


@app.post("/api/terminal-access")
def add_terminal_access(req: AddTerminalAccessRequest, user=Depends(verify_token)):
    client = get_supabase()
    try:
        client.table("driver_terminal_cards").upsert({
            "driver_id": req.driver_id,
            "terminal_id": req.terminal_id,
        }).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/terminal-access")
def remove_terminal_access(req: RemoveTerminalAccessRequest, user=Depends(verify_token)):
    client = get_supabase()
    client.table("driver_terminal_cards") \
        .delete() \
        .eq("driver_id", req.driver_id) \
        .eq("terminal_id", req.terminal_id) \
        .execute()
    return {"status": "ok"}


# ---- Restrictions ----
@app.get("/api/restrictions")
def get_restrictions(user=Depends(verify_token)):
    client = get_supabase()
    return client.table("driver_restrictions").select("*").execute().data


@app.post("/api/restrictions")
def add_restriction(req: AddRestrictionRequest, user=Depends(verify_token)):
    client = get_supabase()
    try:
        client.table("driver_restrictions").insert({
            "driver_id": req.driver_id,
            "restriction_type": req.restriction_type,
            "site_id": req.site_id,
            "customer_group_name": req.customer_group_name,
            "notes": req.notes,
            "created_by": None,
        }).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/restrictions/{restriction_id}")
def remove_restriction(restriction_id: int, user=Depends(verify_token)):
    client = get_supabase()
    client.table("driver_restrictions").delete().eq("id", restriction_id).execute()
    return {"status": "ok"}


# ---- Dispatch Alerts ----
@app.get("/api/dispatch/alerts")
def get_dispatch_alerts(dispatch_date: str, user=Depends(verify_token)):
    """Return active driver alerts for the given dispatch date.
    Only generates alerts when viewing today's board."""
    from datetime import datetime as dt
    client = get_supabase()

    today = date.today().isoformat()
    if dispatch_date != today:
        return {"alerts": [], "as_of": dt.now().isoformat()}

    now = dt.now()

    driver_rows = (
        client.table("driver_schedules")
        .select("driver_id,first_name,last_name,driver_start_time")
        .eq("shift_date", dispatch_date)
        .eq("attendance_expected", 1)
        .execute()
        .data
    )

    try:
        inactive_rows = client.table("driver_inactive").select("driver_id").execute().data
        inactive_ids = {r["driver_id"] for r in inactive_rows}
        driver_rows = [r for r in driver_rows if r.get("driver_id") not in inactive_ids]
    except Exception:
        pass

    clock_rows = (
        client.table("driver_clock_events")
        .select("driver_id,route_start_time,route_finish_time")
        .eq("shift_date", dispatch_date)
        .execute()
        .data
    )

    # Guard: if no clock events exist for this date at all, the sync hasn't
    # populated the table yet.  Suppress all alerts rather than falsely flagging
    # every driver as "not started".
    if not clock_rows:
        return {"alerts": [], "as_of": now.isoformat()}

    clock_map = {r["driver_id"]: r for r in clock_rows}

    alerts = []
    for driver in driver_rows:
        did = driver.get("driver_id")
        name = f"{driver.get('first_name') or ''} {driver.get('last_name') or ''}".strip()
        start_str = driver.get("driver_start_time") or ""
        if not start_str or ":" not in start_str:
            continue
        try:
            parts = start_str.split(":")
            scheduled_start = dt(now.year, now.month, now.day, int(parts[0]), int(parts[1]))
        except Exception:
            continue

        mins_since_scheduled = (now - scheduled_start).total_seconds() / 60
        if mins_since_scheduled < 0:
            continue  # shift hasn't started yet

        clk = clock_map.get(did, {})
        route_start_raw = clk.get("route_start_time")

        if route_start_raw:
            # Driver has clocked in — check for delayed start (≥45 min late)
            try:
                actual_start = dt.fromisoformat(str(route_start_raw).replace("Z", ""))
                delay_mins = (actual_start - scheduled_start).total_seconds() / 60
                if delay_mins >= 45:
                    alerts.append({
                        "driver_id": did,
                        "driver_name": name,
                        "type": "delayed_start",
                        "message": f"{name} has a delayed start. Reroute may be necessary.",
                        "delay_mins": int(delay_mins),
                    })
            except Exception:
                pass
        else:
            # Driver has not clocked in — alert if ≥46 min past scheduled start
            if mins_since_scheduled >= 46:
                alerts.append({
                    "driver_id": did,
                    "driver_name": name,
                    "type": "not_started",
                    "message": f"{name} has not started their shift. Confirm attendance and start time.",
                    "mins_overdue": int(mins_since_scheduled),
                })

    return {"alerts": alerts, "as_of": now.isoformat()}


# ---- Dispatch Board ----
@app.get("/api/dispatch")
def get_dispatch_board(dispatch_date: str, user=Depends(verify_token)):
    try:
     return _get_dispatch_board_inner(dispatch_date, get_supabase())
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"get_dispatch_board error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _get_dispatch_board_inner(dispatch_date: str, client):
    from datetime import date as _date, timedelta as _td

    dispatch_dt = _date.fromisoformat(dispatch_date)
    prev_date = (dispatch_dt - _td(days=1)).isoformat()

    # ---- helpers ----
    def _is_overnight_start(start_time) -> bool:
        """Driver starts at 20:00 or later → displays on next calendar day's AM board."""
        if not start_time or ':' not in str(start_time):
            return False
        try:
            return int(str(start_time).split(':')[0]) >= 20
        except Exception:
            return False

    def _to_am_board(loc: str) -> str:
        """Remap any board_location to its AM equivalent (TX-PM → TX-AM)."""
        if not loc:
            return loc
        parts = loc.split('-')
        return f"{parts[0]}-AM" if len(parts) == 2 else loc

    # ---- terminal abbreviation lookup ----
    _term_rows = client.table("terminal_locations").select(
        "terminal_name,terminal_abbreviation,terminal_abreviation"
    ).execute().data
    _term_abbr: dict[str, str] = {
        r["terminal_name"].lower().strip(): (
            r.get("terminal_abbreviation") or r.get("terminal_abreviation") or ""
        )
        for r in _term_rows if r.get("terminal_name")
    }

    # ---- dispatch results ----
    # Current date → same-day drivers; previous date → overnight drivers
    results      = client.table("dispatch_results").select("*").eq("dispatch_date", dispatch_date).execute().data
    prev_results = client.table("dispatch_results").select("*").eq("dispatch_date", prev_date).execute().data
    unassigned   = client.table("unassigned_loads").select("*").eq("dispatch_date", dispatch_date).execute().data

    for row in results + prev_results:
        tname = (row.get("terminal_name") or "").lower().strip()
        row["terminal_abbreviation"] = _term_abbr.get(tname, "")

    # ---- load details (paginated) ----
    def _fetch_loads(d: str) -> list:
        rows, offset, page = [], 0, 1000
        while True:
            batch = (
                client.table("load_details").select("*").eq("delivery_date", d)
                .range(offset, offset + page - 1).execute().data
            )
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page
        return rows

    loads      = _fetch_loads(dispatch_date)
    prev_loads = _fetch_loads(prev_date)

    # ---- driver schedules ----
    _driver_cols = (
        "driver_id,first_name,last_name,board_location,attendance_expected,"
        "driver_schedule,driver_start_time,yard,pump_trained,max_shift_hours"
    )

    def _fetch_drivers(d: str) -> list:
        return (
            client.table("driver_schedules").select(_driver_cols)
            .eq("shift_date", d).eq("attendance_expected", 1)
            .not_.is_("board_location", "null").neq("board_location", "")
            .not_.is_("yard", "null").neq("yard", "")
            .execute().data
        )

    # Same-day drivers whose shift starts before 20:00
    current_drivers = [
        d for d in _fetch_drivers(dispatch_date)
        if not _is_overnight_start(d.get("driver_start_time"))
    ]

    # Previous-day drivers whose shift starts at 20:00+ → shown on this (next) day's AM board
    overnight_drivers = [
        d for d in _fetch_drivers(prev_date)
        if _is_overnight_start(d.get("driver_start_time"))
    ]
    for d in overnight_drivers:
        d["board_location"] = _to_am_board(d.get("board_location") or "")

    driver_rows = current_drivers + overnight_drivers

    # Remove permanently inactive drivers
    try:
        inactive_ids = {
            r["driver_id"]
            for r in client.table("driver_inactive").select("driver_id").execute().data
        }
        if inactive_ids:
            driver_rows = [r for r in driver_rows if r.get("driver_id") not in inactive_ids]
    except Exception:
        pass

    scheduled_driver_ids = {r["driver_id"] for r in driver_rows}
    overnight_driver_ids = {
        d["driver_id"] for d in overnight_drivers
        if d["driver_id"] in scheduled_driver_ids
    }

    # Filter dispatch results to scheduled drivers, keeping results on the
    # correct date per driver type (overnight drivers → previous date results).
    cur_results = [r for r in results      if r.get("driver_id") in scheduled_driver_ids - overnight_driver_ids]
    ov_results  = [r for r in prev_results if r.get("driver_id") in overnight_driver_ids]
    all_results = cur_results + ov_results

    driver_name_map: dict[str, dict] = {
        f"{d['first_name']} {d['last_name']}".strip().lower(): d
        for d in driver_rows
        if d.get("first_name") and d.get("last_name")
    }

    # ---- pre-assigned loads (status > 1 with driver name) ----
    def _fetch_pre_assigned(d: str) -> list:
        cols = (
            "ce_id,first_name,last_name,load_status,site_name,site_address,city,"
            "terminal_name,delivery_eta,window_start,window_end,product_name,"
            "gross_gallons,customer_name,order_number,completed_delivery_time"
        )
        rows, offset, page = [], 0, 1000
        while True:
            batch = (
                client.table("load_details").select(cols)
                .eq("delivery_date", d).gt("load_status", 1)
                .not_.is_("first_name", "null")
                .range(offset, offset + page - 1).execute().data
            )
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page
        return rows

    pre_assigned_loads = _fetch_pre_assigned(dispatch_date)
    # Tag each row so we can filter by origin date later in the building loop.
    for r in pre_assigned_loads:
        r["_board_date"] = dispatch_date

    if overnight_driver_ids:
        # Include prev-date active loads for overnight drivers, but drop DELIVERED
        # (status 26) — completed work from the previous calendar day doesn't belong
        # on the next day's board and only clutters the dispatcher's view.
        prev_pa = _fetch_pre_assigned(prev_date)
        for r in prev_pa:
            r["_board_date"] = prev_date
        pre_assigned_loads += [
            r for r in prev_pa
            if int(r.get("load_status") or 0) != 26
        ]

    dispatched_ce_ids = {r["ce_id"] for r in all_results}
    seen: set[int] = set()
    pre_assigned: list[dict] = []

    def _pa_sort_key(r: dict):
        status = int(r.get("load_status") or 0)
        if status == 26:
            return (0, r.get("completed_delivery_time") or "9999")
        if status in (22, 24):
            return (1, r.get("delivery_eta") or "9999")
        if status == 10:
            return (2, r.get("delivery_eta") or "9999")
        return (3, r.get("delivery_eta") or "9999")

    for load in sorted(pre_assigned_loads, key=_pa_sort_key):
        ce_id = load.get("ce_id")
        if ce_id is None or ce_id in dispatched_ce_ids or ce_id in seen:
            continue
        seen.add(ce_id)
        fname = (load.get("first_name") or "").strip()
        lname = (load.get("last_name") or "").strip()
        driver = driver_name_map.get(f"{fname} {lname}".lower())
        if not driver:
            continue
        # Prev-date loads must only appear on overnight driver columns.
        # A regular driver (e.g. Juan, starts 04:30) who is also scheduled the
        # following day must NOT inherit his prev-day CE loads on that next board.
        if load.get("_board_date") == prev_date and driver["driver_id"] not in overnight_driver_ids:
            continue
        status = int(load.get("load_status") or 0)
        # For overnight drivers, skip dispatch_date loads that are still status=2
        # (CE-scheduled but not yet dispatched).  Status-2 on the dispatch_date
        # means those loads belong to the driver's NEXT shift starting that evening
        # — e.g. a driver whose current shift ends ~10:00 will have new status-2
        # loads for 22:00 that night; those should appear on the *next* board, not
        # the current one.  Active current-shift loads will already be status > 2
        # (dispatched / en-route / delivered) by the time the board is viewed.
        if (load.get("_board_date") == dispatch_date
                and driver["driver_id"] in overnight_driver_ids
                and status == 2):
            continue
        display_eta = (
            load.get("completed_delivery_time") or load.get("delivery_eta")
            if status == 26 else load.get("delivery_eta")
        )
        _tname = (load.get("terminal_name") or "").lower().strip()
        pre_assigned.append({
            "dispatch_date": dispatch_date,
            "driver_id": driver["driver_id"],
            "driver_name": f"{driver['first_name']} {driver['last_name']}",
            "board_location": driver.get("board_location"),
            "ce_id": ce_id,
            "route_sequence": None,
            "terminal_name": load.get("terminal_name") or "",
            "terminal_abbreviation": _term_abbr.get(_tname, ""),
            "site_name": load.get("site_name") or "",
            "site_city": load.get("city") or "",
            "eta": display_eta,
            "load_status": status,
            "completed_delivery_time": load.get("completed_delivery_time"),
            "pre_assigned": True,
        })

    # ---- clock events ----
    # Current-day drivers → shift_date = dispatch_date
    # Overnight drivers   → shift_date = prev_date
    try:
        clock_rows = (
            client.table("driver_clock_events")
            .select("driver_id,route_start_time,route_finish_time")
            .eq("shift_date", dispatch_date).execute().data
        )
        if overnight_driver_ids:
            prev_clock = (
                client.table("driver_clock_events")
                .select("driver_id,route_start_time,route_finish_time")
                .eq("shift_date", prev_date).execute().data
            )
            clock_rows += [r for r in prev_clock if r.get("driver_id") in overnight_driver_ids]

        clock_map = {r["driver_id"]: r for r in clock_rows}
        for dr in driver_rows:
            clk = clock_map.get(dr.get("driver_id"), {})
            dr["route_start_time"] = clk.get("route_start_time")
            dr["route_finish_time"] = clk.get("route_finish_time")
    except Exception:
        for dr in driver_rows:
            dr["route_start_time"] = None
            dr["route_finish_time"] = None

    return {
        "dispatch_results": all_results,
        "pre_assigned": pre_assigned,
        "unassigned": unassigned,
        "loads": loads + prev_loads,
        "driver_schedules": driver_rows,
    }


@app.patch("/api/dispatch/{ce_id}/resequence")
def resequence_load(ce_id: int, body: dict, user=Depends(verify_token)):
    """Move a load up/down or to a different driver."""
    client = get_supabase()
    new_driver_id = body.get("driver_id")
    new_sequence = body.get("sequence")
    dispatch_date = body.get("dispatch_date")
    client.table("dispatch_results") \
        .update({"driver_id": new_driver_id, "route_sequence": new_sequence}) \
        .eq("ce_id", ce_id) \
        .eq("dispatch_date", dispatch_date) \
        .execute()
    return {"status": "ok"}


# ---- Terminals ----
@app.get("/api/terminals")
def get_terminals(user=Depends(verify_token)):
    client = get_supabase()
    return client.table("terminal_locations").select("*").execute().data


@app.post("/api/terminals")
def add_terminal(body: dict, user=Depends(verify_token)):
    client = get_supabase()
    client.table("terminal_locations").upsert(body).execute()
    return {"status": "ok"}


@app.delete("/api/terminals/{terminal_id}")
def delete_terminal(terminal_id: int, user=Depends(verify_token)):
    client = get_supabase()
    client.table("terminal_locations").delete().eq("terminal_id", terminal_id).execute()
    return {"status": "ok"}


# ---- Sites ----
@app.get("/api/sites")
def get_sites(user=Depends(verify_token)):
    client = get_supabase()
    return client.table("site_details").select("*").execute().data


# ---- Sync status ----
@app.get("/api/sync/status")
def get_sync_status(user=Depends(verify_token)):
    try:
        client = get_supabase()
        rows = client.table("sync_log").select("*").order("synced_at", desc=True).limit(20).execute().data
        return rows
    except Exception as e:
        log.exception(f"get_sync_status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run("backend.api.main:app", host="0.0.0.0", port=8000, reload=True)
