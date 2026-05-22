"""
AutoRouting FastAPI Backend
"""

import logging
import os
from datetime import date, datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
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

# The frontend authenticates via Bearer token in the Authorization header,
# not via cookies, so allow_credentials is not needed. This lets us use
# allow_origins=["*"] which avoids all origin-matching complexity.
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
    client = create_client(SUPABASE_URL, os.getenv("SUPABASE_ANON_KEY"))
    try:
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
    terminal_id: int


class RemoveTerminalAccessRequest(BaseModel):
    driver_id: int
    terminal_id: int


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
    background_tasks: BackgroundTasks,
    user=Depends(verify_token),
):
    """Run the routing engine for a given date."""
    try:
        dispatch_date = date.fromisoformat(req.dispatch_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    client = get_supabase()

    # Load all reference data
    yards = load_yards(client)
    terminals = load_terminals(client)
    sites = load_sites(client)
    drivers = load_drivers_for_date(client, dispatch_date, yards)
    loads = load_loads_for_date(client, dispatch_date)

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
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, engine.run)

    # Persist results to Supabase in background
    background_tasks.add_task(persist_dispatch_result, client, result, user)

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
                    "terminal_id": stop.terminal.terminal_id if stop.terminal else None,
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
    rows = client.table("driver_schedules").select("*").eq("shift_date", dispatch_date).execute().data
    return rows


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
    return client.table("driver_terminal_cards").select("*").execute().data


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


# ---- Dispatch Board ----
@app.get("/api/dispatch")
def get_dispatch_board(dispatch_date: str, user=Depends(verify_token)):
    client = get_supabase()
    results = client.table("dispatch_results").select("*").eq("dispatch_date", dispatch_date).execute().data
    unassigned = client.table("unassigned_loads").select("*").eq("dispatch_date", dispatch_date).execute().data

    # Paginate load_details — Supabase default page limit is 1000 rows
    loads = []
    _page_size = 1000
    _offset = 0
    while True:
        _batch = (
            client.table("load_details")
            .select("*")
            .eq("delivery_date", dispatch_date)
            .range(_offset, _offset + _page_size - 1)
            .execute()
            .data
        )
        loads.extend(_batch)
        if len(_batch) < _page_size:
            break
        _offset += _page_size

    # Build pre-assigned rows: loads already in progress or delivered that
    # have a driver name in the source data. These show on the board
    # regardless of whether the routing engine has been run.
    pre_assigned_loads = []
    _pa_offset = 0
    while True:
        _pa_batch = (
            client.table("load_details")
            .select("ce_id,first_name,last_name,load_status,site_name,site_address,city,terminal_name,delivery_eta,window_start,window_end,product_name,gross_gallons,customer_name,order_number,completed_delivery_time")
            .eq("delivery_date", dispatch_date)
            .gt("load_status", 1)
            .not_.is_("first_name", "null")
            .range(_pa_offset, _pa_offset + 999)
            .execute()
            .data
        )
        pre_assigned_loads.extend(_pa_batch)
        if len(_pa_batch) < 1000:
            break
        _pa_offset += 1000

    # Load ALL driver_schedules for the date (all attendance states)
    # so the board can render every driver column and flag exceptions.
    driver_rows = (
        client.table("driver_schedules")
        .select("driver_id,first_name,last_name,board_location,attendance_expected,driver_schedule,driver_start_time,yard,pump_trained,max_shift_hours")
        .eq("shift_date", dispatch_date)
        .execute()
        .data
    )
    driver_name_map: dict[str, dict] = {
        f"{d['first_name']} {d['last_name']}".strip().lower(): d
        for d in driver_rows
        if d.get("first_name") and d.get("last_name")
    }

    # Deduplicate by ce_id (one row per load, not per product),
    # skip any ce_id already covered by dispatch_results
    dispatched_ce_ids = {r["ce_id"] for r in results}
    seen: set[int] = set()
    pre_assigned: list[dict] = []

    def _pa_sort_key(r: dict):
        """
        Sort order for pre-assigned loads within a driver's column:
          1. Delivered (26): sort by completed_delivery_time asc
          2. In-progress (22, 24): sort by delivery_eta asc
          3. Assigned-not-started (10): sort by delivery_eta asc
          4. Everything else: push to end
        """
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
        status = int(load.get("load_status") or 0)
        # Use status-appropriate time as the display ETA
        if status == 26:
            display_eta = load.get("completed_delivery_time") or load.get("delivery_eta")
        else:
            display_eta = load.get("delivery_eta")
        pre_assigned.append({
            "dispatch_date": dispatch_date,
            "driver_id": driver["driver_id"],
            "driver_name": f"{driver['first_name']} {driver['last_name']}",
            "board_location": driver.get("board_location"),
            "ce_id": ce_id,
            "route_sequence": None,
            "terminal_name": load.get("terminal_name") or "",
            "site_name": load.get("site_name") or "",
            "site_city": load.get("city") or "",
            "eta": display_eta,
            "load_status": status,
            "completed_delivery_time": load.get("completed_delivery_time"),
            "pre_assigned": True,
        })

    return {
        "dispatch_results": results,
        "pre_assigned": pre_assigned,
        "unassigned": unassigned,
        "loads": loads,
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
    client = get_supabase()
    rows = client.table("sync_log").select("*").order("synced_at", desc=True).limit(20).execute().data
    return rows


if __name__ == "__main__":
    uvicorn.run("backend.api.main:app", host="0.0.0.0", port=8000, reload=True)
