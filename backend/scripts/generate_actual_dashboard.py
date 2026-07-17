"""
Build an HTML dashboard from what actually happened on a given historical date
(real assigned drivers + real load status from load_details), in the same
Map/Timeline format as export_dispatch_excel.py's simulated dashboard, so the
two can be compared side by side.

Since load_details doesn't store per-leg arrive/depart timestamps, arrive/depart
terminal times are back-calculated from the known site arrival time using
haversine travel estimates — same method the engine uses as its fallback.
"""

import sys
import os
import json
import logging
import argparse
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from supabase import create_client
from engine.data_loader import load_yards, load_terminals, load_sites, load_loads_for_date
from engine.geo import haversine_miles, haversine_travel_mins

logging.basicConfig(level=logging.WARNING)

parser = argparse.ArgumentParser(description="Build an 'actual dispatch' dashboard for a historical date")
parser.add_argument("--date", type=str, required=True, help="Date as YYYY-MM-DD")
args = parser.parse_args()
dispatch_date = date.fromisoformat(args.date)

LOAD_SERVICE_MINS = 30
UNLOAD_SERVICE_MINS = 45

print("Loading reference data from Supabase...")
client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
yards = load_yards(client)
terminals = load_terminals(client)
sites = load_sites(client)

# Driver name -> {yard, board_location} for this date, fetched directly
# (not through load_drivers_for_date, which filters out inactive drivers —
# for an "actual" historical view we want everyone who really drove that day,
# including drivers later marked inactive).
driver_rows = (
    client.table("driver_schedules").select("driver_id,first_name,last_name,yard,board_location")
    .eq("shift_date", dispatch_date.isoformat())
    .execute().data
)
driver_info = {}
for r in driver_rows:
    key = f"{(r.get('first_name') or '').strip()} {(r.get('last_name') or '').strip()}".strip().lower()
    if key and key not in driver_info:
        driver_info[key] = r

print("Loading actual load outcomes...")
loads = load_loads_for_date(client, dispatch_date)

# CE-touched loads only (status > 1 = actually assigned/dispatched to a driver),
# scoped to this exact delivery_date (load_loads_for_date also pulls date+1
# overflow, which we don't want for a single day's "actual" snapshot).
actual_loads = [
    l for l in loads
    if l.load_status > 1
    and l.assigned_driver_first and l.assigned_driver_last
    and l.delivery_date == dispatch_date.isoformat()
]

print(f"Found {len(actual_loads)} actually-dispatched loads for {dispatch_date}")


def sort_key(l):
    ts = l.completed_delivery_time or l.delivery_eta or l.window_start
    return (ts is None, ts)


by_driver: dict[str, list] = {}
for l in actual_loads:
    key = f"{l.assigned_driver_first.strip()} {l.assigned_driver_last.strip()}".strip().lower()
    by_driver.setdefault(key, []).append(l)

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
total_assigned = 0
total_loaded_all = 0.0
total_empty_all = 0.0

for i, (key, driver_loads) in enumerate(sorted(by_driver.items())):
    info = driver_info.get(key, {})
    color = COLORS[i % len(COLORS)]
    driver_name = key.title()
    yard_name = info.get("yard") or ""
    yard_loc = yards.get(yard_name)
    yard_lat = yard_loc.latitude if yard_loc else 0
    yard_lon = yard_loc.longitude if yard_loc else 0

    stops_data = []
    prev_lat, prev_lon = yard_lat, yard_lon
    total_loaded = 0.0
    total_empty = 0.0

    for seq, l in enumerate(sorted(driver_loads, key=sort_key)):
        term = terminals.get(l.terminal_id)
        site = sites.get(l.site_id)
        if not term or not site:
            continue

        arrive_site = l.completed_delivery_time or l.delivery_eta
        if not arrive_site:
            continue

        loaded_miles = haversine_miles(term.latitude, term.longitude, site.latitude, site.longitude)
        empty_miles = haversine_miles(prev_lat, prev_lon, term.latitude, term.longitude) if prev_lat and prev_lon else 0
        drive_to_site_mins = haversine_travel_mins(term.latitude, term.longitude, site.latitude, site.longitude)
        drive_to_terminal_mins = haversine_travel_mins(prev_lat, prev_lon, term.latitude, term.longitude) if prev_lat and prev_lon else 0

        depart_terminal = arrive_site - timedelta(minutes=drive_to_site_mins)
        arrive_terminal = depart_terminal - timedelta(minutes=LOAD_SERVICE_MINS)
        depart_site = arrive_site + timedelta(minutes=UNLOAD_SERVICE_MINS)

        stops_data.append({
            "ce_id": l.ce_id,
            "seq": seq,
            "terminal_name": term.terminal_name,
            "terminal_lat": term.latitude,
            "terminal_lon": term.longitude,
            "site_name": site.site_name,
            "site_lat": site.latitude,
            "site_lon": site.longitude,
            "site_city": l.city,
            "empty_miles": round(empty_miles, 1),
            "loaded_miles": round(loaded_miles, 1),
            "arrive_terminal": arrive_terminal.strftime("%H:%M"),
            "depart_terminal": depart_terminal.strftime("%H:%M"),
            "arrive_site": arrive_site.strftime("%H:%M"),
            "depart_site": depart_site.strftime("%H:%M"),
        })

        total_loaded += loaded_miles
        total_empty += empty_miles
        prev_lat, prev_lon = site.latitude, site.longitude
        total_assigned += 1

    if not stops_data:
        continue

    routes_data.append({
        "driver": driver_name,
        "driver_id": info.get("driver_id"),
        "board": info.get("board_location") or "",
        "yard": yard_name,
        "yard_lat": yard_lat,
        "yard_lon": yard_lon,
        "color": color,
        "stops": stops_data,
        "total_loaded_miles": round(total_loaded, 1),
        "total_empty_miles": round(total_empty, 1),
        "total_shift_mins": 0,
        "max_shift_hours": 12.0,
    })
    total_loaded_all += total_loaded
    total_empty_all += total_empty

total_drivers = len(routes_data)
ratio = total_loaded_all / (total_loaded_all + total_empty_all) if (total_loaded_all + total_empty_all) > 0 else 0

summary = {
    "date": str(dispatch_date),
    "total_assigned": total_assigned,
    "total_unassigned": 0,
    "total_drivers": total_drivers,
    "avg_loads_per_driver": round(total_assigned / max(total_drivers, 1), 1),
    "total_loaded_miles": round(total_loaded_all, 0),
    "total_empty_miles": round(total_empty_all, 0),
    "loaded_mile_ratio": round(ratio * 100, 1),
}

print(f"Actual: {total_assigned} loads across {total_drivers} drivers")

html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Actual Dispatch — {dispatch_date}</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f6fa; }}
        .header {{ background: #922b21; color: white; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; }}
        .header h1 {{ font-size: 20px; font-weight: 600; }}
        .header .date {{ font-size: 14px; opacity: 0.8; }}
        .metrics {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; padding: 16px 24px; }}
        .metric-card {{ background: white; border-radius: 8px; padding: 16px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .metric-card .value {{ font-size: 28px; font-weight: 700; color: #2c3e50; }}
        .metric-card .label {{ font-size: 12px; color: #7f8c8d; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}

        .tab-bar {{ display: flex; gap: 0; padding: 0 24px; background: white; border-bottom: 1px solid #e0e0e0; }}
        .tab {{ padding: 12px 24px; font-size: 14px; font-weight: 500; cursor: pointer; border-bottom: 2px solid transparent; color: #7f8c8d; transition: all 0.2s; }}
        .tab:hover {{ color: #2c3e50; }}
        .tab.active {{ color: #922b21; border-bottom-color: #922b21; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        .map-content {{ display: grid; grid-template-columns: 1fr 380px; gap: 0; height: calc(100vh - 230px); }}
        #map {{ height: 100%; }}
        .sidebar {{ background: white; overflow-y: auto; border-left: 1px solid #e0e0e0; }}
        .sidebar h3 {{ padding: 12px 16px; background: #ecf0f1; font-size: 14px; position: sticky; top: 0; z-index: 10; }}
        .driver-card {{ padding: 10px 16px; border-bottom: 1px solid #f0f0f0; cursor: pointer; transition: background 0.15s; }}
        .driver-card:hover {{ background: #f8f9fa; }}
        .driver-card.active {{ background: #fdedec; border-left: 3px solid #922b21; }}
        .driver-header {{ display: flex; align-items: center; gap: 8px; }}
        .driver-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
        .driver-name {{ font-weight: 600; font-size: 13px; }}
        .driver-meta {{ font-size: 11px; color: #7f8c8d; margin-top: 2px; }}
        .driver-stops {{ margin-top: 6px; font-size: 11px; }}
        .stop-item {{ display: flex; gap: 6px; padding: 2px 0; color: #555; }}
        .stop-seq {{ font-weight: 600; color: #922b21; min-width: 16px; }}

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
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>Actual Dispatch — What Really Happened</h1>
            <div class="date">{dispatch_date.strftime('%A, %B %d, %Y')}</div>
        </div>
    </div>
    <div class="metrics">
        <div class="metric-card"><div class="value">{summary['total_assigned']}</div><div class="label">Loads Delivered</div></div>
        <div class="metric-card"><div class="value">{summary['total_drivers']}</div><div class="label">Drivers Used</div></div>
        <div class="metric-card"><div class="value">{summary['avg_loads_per_driver']}</div><div class="label">Avg Loads/Driver</div></div>
        <div class="metric-card"><div class="value">{summary['loaded_mile_ratio']}%</div><div class="label">Loaded Mile Ratio*</div></div>
        <div class="metric-card"><div class="value">{int(summary['total_loaded_miles'])}</div><div class="label">Loaded Miles*</div></div>
        <div class="metric-card"><div class="value">{int(summary['total_empty_miles'])}</div><div class="label">Empty Miles*</div></div>
    </div>

    <div class="tab-bar">
        <div class="tab active" data-tab="map-tab">Map View</div>
        <div class="tab" data-tab="timeline-tab">Timeline View</div>
    </div>

    <div class="tab-content active" id="map-tab">
        <div class="map-content">
            <div id="map"></div>
            <div class="sidebar">
                <h3>Driver Routes ({total_drivers} drivers, {total_assigned} loads) <button id="reset-btn" style="float:right;padding:3px 10px;font-size:11px;border:1px solid #bbb;border-radius:4px;background:#fff;cursor:pointer;">Show All</button></h3>
                <div id="driver-list"></div>
            </div>
        </div>
    </div>

    <div class="tab-content" id="timeline-tab">
        <div class="timeline-container">
            <div class="timeline-header">
                <div class="driver-col">Driver</div>
                <div class="time-axis" id="time-axis"></div>
            </div>
            <div id="timeline-rows"></div>
        </div>
    </div>

    <p style="padding: 8px 24px; font-size: 11px; color: #999;">* Miles are straight-line (haversine) estimates between actual terminal/site coordinates — load_details doesn't store real driven mileage, so this is the closest apples-to-apples comparison against the simulated dashboard's fallback distance method. Arrive/depart terminal times are back-calculated from the real site arrival time using estimated travel time; only the site arrival (and derived departure) reflect real recorded data.</p>

    <script>
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
                if (prevLat && prevLon && stop.terminal_lat && stop.terminal_lon) {{
                    L.polyline([[prevLat, prevLon], [stop.terminal_lat, stop.terminal_lon]], {{
                        color: route.color, weight: 2, opacity: 0.5, dashArray: '5,5'
                    }}).addTo(group);
                }}

                var termIcon = L.divIcon({{
                    html: '<div style="background:' + route.color + ';width:8px;height:8px;border-radius:50%;border:1px solid white;opacity:0.7"></div>',
                    className: '', iconSize: [8,8], iconAnchor: [4,4]
                }});
                L.marker([stop.terminal_lat, stop.terminal_lon], {{icon: termIcon}})
                    .bindPopup('<b>Terminal:</b> ' + stop.terminal_name + '<br><b>Driver:</b> ' + route.driver + '<br>Arrive: ' + stop.arrive_terminal + ' | Depart: ' + stop.depart_terminal)
                    .addTo(group);

                L.polyline([[stop.terminal_lat, stop.terminal_lon], [stop.site_lat, stop.site_lon]], {{
                    color: route.color, weight: 3, opacity: 0.8
                }}).addTo(group);

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

        var driverList = document.getElementById('driver-list');
        routesData.forEach(function(route, idx) {{
            var card = document.createElement('div');
            card.className = 'driver-card';
            card.dataset.idx = idx;

            var stopsHtml = route.stops.map(function(s, si) {{
                return '<div class="stop-item"><span class="stop-seq">' + (si+1) + '</span>' + s.site_name + ' (' + s.site_city + ') — ' + s.loaded_miles + ' mi</div>';
            }}).join('');

            card.innerHTML = '<div class="driver-header"><span class="driver-dot" style="background:' + route.color + '"></span><span class="driver-name">' + route.driver + '</span></div>' +
                '<div class="driver-meta">' + route.board + ' | ' + route.stops.length + ' loads | ' + route.total_loaded_miles + ' loaded mi | ' + route.total_empty_miles + ' empty mi</div>' +
                '<div class="driver-stops">' + stopsHtml + '</div>';

            card.onclick = function() {{
                var isActive = card.classList.contains('active');
                document.querySelectorAll('.driver-card').forEach(c => c.classList.remove('active'));

                if (isActive) {{
                    routeLayers.forEach(function(layer) {{ layer.addTo(map); }});
                    if (allBounds.length) map.fitBounds(allBounds, {{padding: [30,30]}});
                }} else {{
                    card.classList.add('active');
                    routeLayers.forEach(function(layer) {{ map.removeLayer(layer); }});
                    routeLayers[idx].addTo(map);
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

        document.getElementById('reset-btn').onclick = function() {{
            document.querySelectorAll('.driver-card').forEach(c => c.classList.remove('active'));
            routeLayers.forEach(function(layer) {{ layer.addTo(map); }});
            if (allBounds.length) map.fitBounds(allBounds, {{padding: [30,30]}});
        }};

        var timelineStartHour = 0;
        var timelineEndHour = 24;
        var timelineHours = timelineEndHour - timelineStartHour;

        var timeAxis = document.getElementById('time-axis');
        for (var h = timelineStartHour; h <= timelineEndHour; h += 2) {{
            var pct = ((h - timelineStartHour) / timelineHours) * 100;
            var label = h === 0 ? '12 AM' : h === 12 ? '12 PM' : h < 12 ? h + ' AM' : (h-12) + ' PM';
            timeAxis.innerHTML += '<span class="time-mark" style="left:' + pct + '%">' + label + '</span>';
            timeAxis.innerHTML += '<div class="time-line" style="left:' + pct + '%;height:2000px"></div>';
        }}

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
                var startMins = timeToMinutes(stop.arrive_terminal);
                var endMins = timeToMinutes(stop.depart_site);

                if (startMins === null || endMins === null) return;
                if (endMins < startMins) endMins = 24 * 60 - 1;

                var leftPct = minutesToPct(startMins);
                var widthPct = minutesToPct(endMins) - leftPct;
                if (widthPct < 2) widthPct = 2;

                var bar = document.createElement('div');
                bar.className = 'trip-bar';
                bar.style.left = leftPct + '%';
                bar.style.width = widthPct + '%';
                bar.style.background = route.color;
                bar.title = stop.terminal_name + ' → ' + stop.site_name + '\\n' + stop.arrive_terminal + ' - ' + stop.depart_site + '\\n' + stop.loaded_miles + ' loaded mi (est.)';

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

output_path = Path(__file__).resolve().parent.parent.parent / "Data" / f"dispatch_dashboard_{dispatch_date}_actual.html"
with open(output_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"\nActual dashboard generated: {output_path}")
print(f"Open in browser: file://{output_path}")
