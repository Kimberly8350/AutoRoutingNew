"""
Run the V2 engine once for a given date (default: today) and export both the
HTML dashboard and an Excel breakdown from the same run, so the two always
agree with each other. Excel has four sheets: Summary, Per Driver, Assigned
Loads (with each load's requested delivery date + window), and Unassigned
Loads.
"""

import sys
import os
import json
import logging
import argparse
from pathlib import Path
from datetime import date
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import pandas as pd
from supabase import create_client
from engine.data_loader import load_yards, load_terminals, load_sites, load_drivers_for_date, load_loads_for_date
from engine import geo
from engine import geo_v2

logging.basicConfig(level=logging.WARNING)

parser = argparse.ArgumentParser(description="Run V2 engine and export dashboard + Excel breakdown")
parser.add_argument("--date", type=str, default=None, help="Dispatch date as YYYY-MM-DD (default: today)")
parser.add_argument("--exclude-drivers", type=str, default=None,
                     help="Comma-separated 'First Last' names to exclude from this run "
                          "(e.g. drivers whose termination wasn't reflected in driver_schedules "
                          "for this historical date)")
args = parser.parse_args()
exclude_names = {n.strip().lower() for n in args.exclude_drivers.split(",")} if args.exclude_drivers else set()

# Disable Google API for fast run — routing_engine_v2 reads travel functions from
# geo_v2, which has its own separate copy of these constants, so both must be patched.
os.environ["GOOGLE_MAPS_API_KEY"] = ""
geo.GOOGLE_MAPS_API_KEY = ""
geo_v2.GOOGLE_MAPS_API_KEY = ""

dispatch_date = date.fromisoformat(args.date) if args.date else date.today()

print("Loading data from Supabase...")
client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
yards = load_yards(client)
terminals = load_terminals(client)
sites = load_sites(client)
drivers = load_drivers_for_date(client, dispatch_date, yards)
loads = load_loads_for_date(client, dispatch_date)

if exclude_names:
    before = len(drivers)
    drivers = [d for d in drivers if f"{d.first_name} {d.last_name}".strip().lower() not in exclude_names]
    print(f"Excluded {before - len(drivers)} driver(s) by name: {sorted(exclude_names)}")

# ce_id -> Load lookup for enriching stops with the load's requested date/window
# (multi-product loads share a ce_id across rows; first one wins, windows match).
load_by_ce_id = {}
for load in loads:
    load_by_ce_id.setdefault(load.ce_id, load)

for load in loads:
    load.load_status = 1
    load.assigned_driver_id = None

print("Running V2 engine...")
geo.LOAD_SERVICE_MINS = 30
geo.UNLOAD_SERVICE_MINS = 45
geo_v2.LOAD_SERVICE_MINS = 30
geo_v2.UNLOAD_SERVICE_MINS = 45
if hasattr(geo, 'TANKER_TRAVEL_MULTIPLIER'):
    geo.TANKER_TRAVEL_MULTIPLIER = 1.20
if hasattr(geo_v2, 'TANKER_TRAVEL_MULTIPLIER'):
    geo_v2.TANKER_TRAVEL_MULTIPLIER = 1.20

from engine.routing_engine_v2 import RoutingEngine as RoutingEngineV2
engine = RoutingEngineV2(
    drivers=deepcopy(drivers),
    loads=deepcopy(loads),
    sites=sites,
    terminals=terminals,
    yards=yards,
    dispatch_date=dispatch_date,
)
result = engine.run()

assigned_count = sum(len(r.stops) for r in result.driver_routes)
print(f"V2 assigned: {assigned_count} loads to {len(result.driver_routes)} drivers")


def fmt_window(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


# ==================== EXCEL ====================

assigned_rows = []
per_driver_rows = []
for route in result.driver_routes:
    driver = route.driver
    driver_name = f"{driver.first_name} {driver.last_name}"

    for stop in route.stops:
        src_load = load_by_ce_id.get(stop.ce_id)
        assigned_rows.append({
            "Driver": driver_name,
            "Board": driver.board_location,
            "Yard": driver.yard,
            "Stop #": stop.sequence,
            "CE ID": stop.ce_id,
            "Terminal": stop.terminal.terminal_name,
            "Site": stop.site.site_name,
            "City": stop.site.city,
            "Loaded Miles": round(stop.loaded_miles, 1),
            "Empty Miles": round(stop.empty_miles, 1),
            "Requested Date": src_load.delivery_date if src_load else "",
            "Window Start": fmt_window(src_load.window_start) if src_load else "",
            "Window End": fmt_window(src_load.window_end) if src_load else "",
            "Arrive Terminal": fmt_window(stop.arrive_terminal),
            "Depart Terminal": fmt_window(stop.depart_terminal),
            "Arrive Site": fmt_window(stop.arrive_site),
            "Depart Site": fmt_window(stop.depart_site),
        })

    per_driver_rows.append({
        "Driver": driver_name,
        "Board": driver.board_location,
        "Yard": driver.yard,
        "Loads": len(route.stops),
        "Loaded Miles": round(route.total_loaded_miles, 1),
        "Empty Miles": round(route.total_empty_miles, 1),
        "Total Miles": round(route.total_loaded_miles + route.total_empty_miles, 1),
        "Shift Hours": round(route.total_shift_mins / 60, 1),
        "Max Shift Hours": driver.max_shift_hours,
    })

assigned_df = pd.DataFrame(assigned_rows)
per_driver_df = pd.DataFrame(per_driver_rows).sort_values("Driver") if per_driver_rows else pd.DataFrame(per_driver_rows)

unassigned_rows = []
for load, reason, category in result.unassigned:
    unassigned_rows.append({
        "CE ID": load.ce_id,
        "Customer": load.customer_name,
        "Order #": load.order_number,
        "Site": load.site_name,
        "City": load.city,
        "State": load.state,
        "Terminal": load.terminal_name,
        "Requested Date": load.delivery_date,
        "Window Start": fmt_window(load.window_start),
        "Window End": fmt_window(load.window_end),
        "Reason": reason,
        "Category": category,
    })
unassigned_df = pd.DataFrame(unassigned_rows)

total_assigned = assigned_count
total_unassigned = len(result.unassigned)
total_drivers = len(result.driver_routes)
total_loaded = assigned_df["Loaded Miles"].sum() if not assigned_df.empty else 0
total_empty = assigned_df["Empty Miles"].sum() if not assigned_df.empty else 0
ratio = total_loaded / (total_loaded + total_empty) if (total_loaded + total_empty) > 0 else 0

summary_df = pd.DataFrame([{
    "Date": str(dispatch_date),
    "Loads Assigned": total_assigned,
    "Loads Unassigned": total_unassigned,
    "Drivers Used": total_drivers,
    "Avg Loads / Driver": round(total_assigned / max(total_drivers, 1), 1),
    "Loaded Miles": round(total_loaded, 0),
    "Empty Miles": round(total_empty, 0),
    "Loaded Mile Ratio %": round(ratio * 100, 1),
}])

excel_path = Path(__file__).resolve().parent.parent.parent / "Data" / f"dispatch_breakdown_{dispatch_date}.xlsx"
with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
    summary_df.to_excel(writer, sheet_name="Summary", index=False)
    per_driver_df.to_excel(writer, sheet_name="Per Driver", index=False)
    assigned_df.to_excel(writer, sheet_name="Assigned Loads", index=False)
    unassigned_df.to_excel(writer, sheet_name="Unassigned Loads", index=False)

print(f"Excel breakdown generated: {excel_path}")

# ==================== HTML DASHBOARD ====================

COLORS = [
    "#e74c3c", "#3498db", "#2ecc71", "#9b59b6", "#f39c12",
    "#1abc9c", "#e67e22", "#2980b9", "#27ae60", "#8e44ad",
    "#d35400", "#16a085", "#c0392b", "#7f8c8d", "#f1c40f",
    "#00bcd4", "#ff5722", "#4caf50", "#673ab7", "#ff9800",
    "#009688", "#795548", "#607d8b", "#e91e63", "#3f51b5",
    "#cddc39", "#ff4081", "#00e676", "#651fff", "#ff6d00",
    "#76ff03", "#d500f9", "#00b0ff", "#ffab00", "#dd2c00",
    "#64dd17", "#aa00ff", "#0091ea", "#ffd600", "#c51162",
]

routes_data = []
for i, route in enumerate(result.driver_routes):
    driver = route.driver
    color = COLORS[i % len(COLORS)]
    driver_name = f"{driver.first_name} {driver.last_name}"

    stops_data = []
    for stop in route.stops:
        stops_data.append({
            "ce_id": stop.ce_id,
            "seq": stop.sequence,
            "terminal_name": stop.terminal.terminal_name,
            "terminal_lat": stop.terminal.latitude,
            "terminal_lon": stop.terminal.longitude,
            "site_name": stop.site.site_name,
            "site_lat": stop.site.latitude,
            "site_lon": stop.site.longitude,
            "site_city": stop.site.city,
            "empty_miles": round(stop.empty_miles, 1),
            "loaded_miles": round(stop.loaded_miles, 1),
            "arrive_terminal": stop.arrive_terminal.strftime("%H:%M") if stop.arrive_terminal else "",
            "depart_terminal": stop.depart_terminal.strftime("%H:%M") if stop.depart_terminal else "",
            "arrive_site": stop.arrive_site.strftime("%H:%M") if stop.arrive_site else "",
            "depart_site": stop.depart_site.strftime("%H:%M") if stop.depart_site else "",
        })

    yard_lat = driver.yard_location.latitude if driver.yard_location else 0
    yard_lon = driver.yard_location.longitude if driver.yard_location else 0

    routes_data.append({
        "driver": driver_name,
        "driver_id": driver.driver_id,
        "board": driver.board_location,
        "yard": driver.yard,
        "yard_lat": yard_lat,
        "yard_lon": yard_lon,
        "color": color,
        "stops": stops_data,
        "total_loaded_miles": round(route.total_loaded_miles, 1),
        "total_empty_miles": round(route.total_empty_miles, 1),
        "total_shift_mins": round(route.total_shift_mins, 1),
        "max_shift_hours": driver.max_shift_hours,
    })

summary = {
    "date": str(dispatch_date),
    "total_assigned": total_assigned,
    "total_unassigned": total_unassigned,
    "total_drivers": total_drivers,
    "avg_loads_per_driver": round(total_assigned / max(total_drivers, 1), 1),
    "total_loaded_miles": round(total_loaded, 0),
    "total_empty_miles": round(total_empty, 0),
    "loaded_mile_ratio": round(ratio * 100, 1),
}

html = f"""<!DOCTYPE html>
<html>
<head>
    <title>V2 Dispatch Dashboard — {dispatch_date}</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f6fa; }}
        .header {{ background: #2c3e50; color: white; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; }}
        .header h1 {{ font-size: 20px; font-weight: 600; }}
        .header .date {{ font-size: 14px; opacity: 0.8; }}
        .metrics {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; padding: 16px 24px; }}
        .metric-card {{ background: white; border-radius: 8px; padding: 16px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .metric-card .value {{ font-size: 28px; font-weight: 700; color: #2c3e50; }}
        .metric-card .label {{ font-size: 12px; color: #7f8c8d; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}

        /* Tabs */
        .tab-bar {{ display: flex; gap: 0; padding: 0 24px; background: white; border-bottom: 1px solid #e0e0e0; }}
        .tab {{ padding: 12px 24px; font-size: 14px; font-weight: 500; cursor: pointer; border-bottom: 2px solid transparent; color: #7f8c8d; transition: all 0.2s; }}
        .tab:hover {{ color: #2c3e50; }}
        .tab.active {{ color: #3498db; border-bottom-color: #3498db; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        /* Map tab */
        .map-content {{ display: grid; grid-template-columns: 1fr 380px; gap: 0; height: calc(100vh - 230px); }}
        #map {{ height: 100%; }}
        .sidebar {{ background: white; overflow-y: auto; border-left: 1px solid #e0e0e0; }}
        .sidebar h3 {{ padding: 12px 16px; background: #ecf0f1; font-size: 14px; position: sticky; top: 0; z-index: 10; }}
        .driver-card {{ padding: 10px 16px; border-bottom: 1px solid #f0f0f0; cursor: pointer; transition: background 0.15s; }}
        .driver-card:hover {{ background: #f8f9fa; }}
        .driver-card.active {{ background: #eef7ff; border-left: 3px solid #3498db; }}
        .driver-header {{ display: flex; align-items: center; gap: 8px; }}
        .driver-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
        .driver-name {{ font-weight: 600; font-size: 13px; }}
        .driver-meta {{ font-size: 11px; color: #7f8c8d; margin-top: 2px; }}
        .driver-stops {{ margin-top: 6px; font-size: 11px; }}
        .stop-item {{ display: flex; gap: 6px; padding: 2px 0; color: #555; }}
        .stop-seq {{ font-weight: 600; color: #3498db; min-width: 16px; }}

        /* Timeline tab */
        .timeline-container {{ height: calc(100vh - 230px); overflow-y: auto; padding: 0; background: white; }}
        .timeline-header {{ display: grid; grid-template-columns: 200px 1fr; position: sticky; top: 0; background: white; z-index: 20; border-bottom: 2px solid #e0e0e0; }}
        .timeline-header .driver-col {{ padding: 10px 16px; font-weight: 600; font-size: 13px; color: #7f8c8d; }}
        .timeline-header .time-axis {{ position: relative; height: 36px; }}
        .time-mark {{ position: absolute; top: 10px; font-size: 11px; color: #999; transform: translateX(-50%); }}
        .time-line {{ position: absolute; top: 28px; bottom: 0; width: 1px; background: #eee; }}
        .timeline-row {{ display: grid; grid-template-columns: 200px 1fr; border-bottom: 1px solid #f5f5f5; min-height: 56px; align-items: center; }}
        .timeline-row:hover {{ background: #fafbfc; }}
        .timeline-driver {{ padding: 8px 16px; }}
        .timeline-driver-name {{ font-weight: 600; font-size: 13px; color: #2c3e50; }}
        .timeline-driver-meta {{ font-size: 11px; color: #999; }}
        .timeline-bars {{ position: relative; height: 56px; }}
        .trip-bar {{ position: absolute; top: 12px; height: 32px; border-radius: 6px; display: flex; align-items: center; padding: 0 8px; font-size: 10px; color: white; font-weight: 500; overflow: hidden; white-space: nowrap; cursor: pointer; transition: opacity 0.15s; box-shadow: 0 1px 3px rgba(0,0,0,0.15); }}
        .trip-bar:hover {{ opacity: 0.85; }}
        .trip-bar .trip-label {{ overflow: hidden; text-overflow: ellipsis; }}
        .trip-bar .trip-time {{ font-size: 9px; opacity: 0.8; margin-top: 1px; }}
        .trip-bar-content {{ display: flex; flex-direction: column; overflow: hidden; }}
        .empty-bar {{ position: absolute; top: 22px; height: 12px; border-radius: 3px; opacity: 0.3; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>V2 Dispatch Engine — Route Plan</h1>
            <div class="date">{dispatch_date.strftime('%A, %B %d, %Y')}</div>
        </div>
    </div>
    <div class="metrics">
        <div class="metric-card"><div class="value">{summary['total_assigned']}</div><div class="label">Loads Assigned</div></div>
        <div class="metric-card"><div class="value">{summary['total_unassigned']}</div><div class="label">Unassigned</div></div>
        <div class="metric-card"><div class="value">{summary['total_drivers']}</div><div class="label">Drivers Used</div></div>
        <div class="metric-card"><div class="value">{summary['avg_loads_per_driver']}</div><div class="label">Avg Loads/Driver</div></div>
        <div class="metric-card"><div class="value">{summary['loaded_mile_ratio']}%</div><div class="label">Loaded Mile Ratio</div></div>
        <div class="metric-card"><div class="value">{int(summary['total_loaded_miles'])}</div><div class="label">Loaded Miles</div></div>
    </div>

    <div class="tab-bar">
        <div class="tab active" data-tab="map-tab">Map View</div>
        <div class="tab" data-tab="timeline-tab">Timeline View</div>
    </div>

    <!-- MAP TAB -->
    <div class="tab-content active" id="map-tab">
        <div class="map-content">
            <div id="map"></div>
            <div class="sidebar">
                <h3>Driver Routes ({total_drivers} drivers, {total_assigned} loads) <button id="reset-btn" style="float:right;padding:3px 10px;font-size:11px;border:1px solid #bbb;border-radius:4px;background:#fff;cursor:pointer;">Show All</button></h3>
                <div id="driver-list"></div>
            </div>
        </div>
    </div>

    <!-- TIMELINE TAB -->
    <div class="tab-content" id="timeline-tab">
        <div class="timeline-container">
            <div class="timeline-header">
                <div class="driver-col">Driver</div>
                <div class="time-axis" id="time-axis"></div>
            </div>
            <div id="timeline-rows"></div>
        </div>
    </div>

    <script>
        // Tab switching
        document.querySelectorAll('.tab').forEach(function(tab) {{
            tab.addEventListener('click', function() {{
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById(tab.dataset.tab).classList.add('active');
                if (tab.dataset.tab === 'map-tab') {{
                    setTimeout(function() {{ map.invalidateSize(); }}, 100);
                }}
            }});
        }});

        var routesData = {json.dumps(routes_data)};

        var map = L.map('map').setView([32.8, -96.8], 9);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap', maxZoom: 18
        }}).addTo(map);

        var routeLayers = [];
        var allBounds = [];

        routesData.forEach(function(route, idx) {{
            var group = L.layerGroup();

            // Yard marker
            if (route.yard_lat && route.yard_lon) {{
                var yardIcon = L.divIcon({{
                    html: '<div style="background:' + route.color + ';width:12px;height:12px;border:2px solid white;box-shadow:0 1px 3px rgba(0,0,0,0.4)"></div>',
                    className: '', iconSize: [12,12], iconAnchor: [6,6]
                }});
                L.marker([route.yard_lat, route.yard_lon], {{icon: yardIcon}})
                    .bindPopup('<b>' + route.driver + '</b><br>Yard: ' + route.yard)
                    .addTo(group);
                allBounds.push([route.yard_lat, route.yard_lon]);
            }}

            var prevLat = route.yard_lat, prevLon = route.yard_lon;

            route.stops.forEach(function(stop, si) {{
                // Line: prev → terminal (dashed = empty)
                if (prevLat && prevLon && stop.terminal_lat && stop.terminal_lon) {{
                    L.polyline([[prevLat, prevLon], [stop.terminal_lat, stop.terminal_lon]], {{
                        color: route.color, weight: 2, opacity: 0.5, dashArray: '5,5'
                    }}).addTo(group);
                }}

                // Terminal marker
                var termIcon = L.divIcon({{
                    html: '<div style="background:' + route.color + ';width:8px;height:8px;border-radius:50%;border:1px solid white;opacity:0.7"></div>',
                    className: '', iconSize: [8,8], iconAnchor: [4,4]
                }});
                L.marker([stop.terminal_lat, stop.terminal_lon], {{icon: termIcon}})
                    .bindPopup('<b>Terminal:</b> ' + stop.terminal_name + '<br><b>Driver:</b> ' + route.driver + '<br>Arrive: ' + stop.arrive_terminal + ' | Depart: ' + stop.depart_terminal)
                    .addTo(group);

                // Line: terminal → site (solid = loaded)
                L.polyline([[stop.terminal_lat, stop.terminal_lon], [stop.site_lat, stop.site_lon]], {{
                    color: route.color, weight: 3, opacity: 0.8
                }}).addTo(group);

                // Site marker
                var siteIcon = L.divIcon({{
                    html: '<div style="background:' + route.color + ';width:14px;height:14px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;color:white;font-size:9px;font-weight:bold">' + (si+1) + '</div>',
                    className: '', iconSize: [14,14], iconAnchor: [7,7]
                }});
                L.marker([stop.site_lat, stop.site_lon], {{icon: siteIcon}})
                    .bindPopup('<b>' + stop.site_name + '</b><br>' + stop.site_city + '<br><b>Driver:</b> ' + route.driver + '<br>Stop #' + (si+1) + ' | Arrive: ' + stop.arrive_site + '<br>Loaded: ' + stop.loaded_miles + ' mi | Empty: ' + stop.empty_miles + ' mi')
                    .addTo(group);

                allBounds.push([stop.site_lat, stop.site_lon]);
                allBounds.push([stop.terminal_lat, stop.terminal_lon]);
                prevLat = stop.site_lat;
                prevLon = stop.site_lon;
            }});

            group.addTo(map);
            routeLayers.push(group);
        }});

        if (allBounds.length > 0) map.fitBounds(allBounds, {{padding: [30,30]}});

        // Sidebar
        var driverList = document.getElementById('driver-list');
        routesData.forEach(function(route, idx) {{
            var card = document.createElement('div');
            card.className = 'driver-card';
            card.dataset.idx = idx;

            var stopsHtml = route.stops.map(function(s, si) {{
                return '<div class="stop-item"><span class="stop-seq">' + (si+1) + '</span>' + s.site_name + ' (' + s.site_city + ') — ' + s.loaded_miles + ' mi</div>';
            }}).join('');

            card.innerHTML = '<div class="driver-header"><span class="driver-dot" style="background:' + route.color + '"></span><span class="driver-name">' + route.driver + '</span></div>' +
                '<div class="driver-meta">' + route.board + ' | ' + route.stops.length + ' loads | ' + route.total_loaded_miles + ' loaded mi | ' + route.total_empty_miles + ' empty mi | ' + Math.round(route.total_shift_mins/60*10)/10 + 'h shift</div>' +
                '<div class="driver-stops">' + stopsHtml + '</div>';

            card.onclick = function() {{
                var isActive = card.classList.contains('active');
                document.querySelectorAll('.driver-card').forEach(c => c.classList.remove('active'));

                if (isActive) {{
                    // Deselect: show all routes
                    routeLayers.forEach(function(layer) {{ layer.addTo(map); }});
                    if (allBounds.length) map.fitBounds(allBounds, {{padding: [30,30]}});
                }} else {{
                    card.classList.add('active');
                    // Hide all routes, show only this one
                    routeLayers.forEach(function(layer) {{ map.removeLayer(layer); }});
                    routeLayers[idx].addTo(map);
                    // Zoom to this route
                    var bounds = [];
                    if (route.yard_lat) bounds.push([route.yard_lat, route.yard_lon]);
                    route.stops.forEach(function(s) {{
                        bounds.push([s.terminal_lat, s.terminal_lon]);
                        bounds.push([s.site_lat, s.site_lon]);
                    }});
                    if (bounds.length) map.fitBounds(bounds, {{padding: [50,50]}});
                }}
            }};
            driverList.appendChild(card);
        }});

        // Reset button: show all drivers
        document.getElementById('reset-btn').onclick = function() {{
            document.querySelectorAll('.driver-card').forEach(c => c.classList.remove('active'));
            routeLayers.forEach(function(layer) {{ layer.addTo(map); }});
            if (allBounds.length) map.fitBounds(allBounds, {{padding: [30,30]}});
        }};

        // =============================================
        // TIMELINE VIEW
        // =============================================
        var timelineStartHour = 0;  // midnight
        var timelineEndHour = 24;   // full day
        var timelineHours = timelineEndHour - timelineStartHour;

        // Build time axis
        var timeAxis = document.getElementById('time-axis');
        for (var h = timelineStartHour; h <= timelineEndHour; h += 2) {{
            var pct = ((h - timelineStartHour) / timelineHours) * 100;
            var label = h === 0 ? '12 AM' : h === 12 ? '12 PM' : h < 12 ? h + ' AM' : (h-12) + ' PM';
            timeAxis.innerHTML += '<span class="time-mark" style="left:' + pct + '%">' + label + '</span>';
            timeAxis.innerHTML += '<div class="time-line" style="left:' + pct + '%;height:2000px"></div>';
        }}

        // Build timeline rows
        var timelineRows = document.getElementById('timeline-rows');

        function timeToMinutes(timeStr) {{
            if (!timeStr) return null;
            var parts = timeStr.split(':');
            return parseInt(parts[0]) * 60 + parseInt(parts[1]);
        }}

        function minutesToPct(mins) {{
            return ((mins - timelineStartHour * 60) / (timelineHours * 60)) * 100;
        }}

        routesData.forEach(function(route) {{
            var row = document.createElement('div');
            row.className = 'timeline-row';

            var driverCell = document.createElement('div');
            driverCell.className = 'timeline-driver';
            driverCell.innerHTML = '<div class="timeline-driver-name"><span class="driver-dot" style="background:' + route.color + ';display:inline-block;margin-right:6px"></span>' + route.driver + '</div>' +
                '<div class="timeline-driver-meta">' + route.board + ' · ' + route.stops.length + ' loads</div>';

            var barsCell = document.createElement('div');
            barsCell.className = 'timeline-bars';

            route.stops.forEach(function(stop, si) {{
                // Trip bar: from arrive_terminal to depart_site
                var startMins = timeToMinutes(stop.arrive_terminal);
                var endMins = timeToMinutes(stop.depart_site);

                if (startMins === null || endMins === null) return;

                // Handle overnight (if depart is next day)
                if (endMins < startMins) endMins = 24 * 60 - 1;

                var leftPct = minutesToPct(startMins);
                var widthPct = minutesToPct(endMins) - leftPct;
                if (widthPct < 2) widthPct = 2; // minimum visible width

                var bar = document.createElement('div');
                bar.className = 'trip-bar';
                bar.style.left = leftPct + '%';
                bar.style.width = widthPct + '%';
                bar.style.background = route.color;
                bar.title = stop.terminal_name + ' → ' + stop.site_name + '\\n' + stop.arrive_terminal + ' - ' + stop.depart_site + '\\n' + stop.loaded_miles + ' loaded mi';

                bar.innerHTML = '<div class="trip-bar-content"><span class="trip-label">' + stop.terminal_name.substring(0,15) + ' → ' + stop.site_name.substring(0,15) + '</span><span class="trip-time">' + stop.arrive_terminal + ' - ' + stop.depart_site + '</span></div>';

                barsCell.appendChild(bar);
            }});

            row.appendChild(driverCell);
            row.appendChild(barsCell);
            timelineRows.appendChild(row);
        }});
    </script>
</body>
</html>"""

html_path = Path(__file__).resolve().parent.parent.parent / "Data" / f"dispatch_dashboard_{dispatch_date}_mine.html"
with open(html_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"Dashboard generated: {html_path}")
print(f"Open in browser: file://{html_path}")
