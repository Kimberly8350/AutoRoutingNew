"""
Generate an interactive HTML map showing terminals, yards, and regions.
"""
import os
import json
import urllib.request
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent / ".env"
env_vars = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            env_vars[key.strip()] = val.strip()

url = env_vars.get("SUPABASE_URL")
key = env_vars.get("SUPABASE_SERVICE_KEY")
headers = {"apikey": key, "Authorization": f"Bearer {key}"}

# Fetch terminals
req = urllib.request.Request(f"{url}/rest/v1/terminal_locations?select=terminal_id,terminal_name,city,state,latitude,longitude", headers=headers)
with urllib.request.urlopen(req, timeout=10) as resp:
    terminals = json.loads(resp.read())

# Fetch yards
req = urllib.request.Request(f"{url}/rest/v1/yard_locations?select=yard,city,state,latitude,longitude", headers=headers)
with urllib.request.urlopen(req, timeout=10) as resp:
    yards = json.loads(resp.read())

# Terminal → region mapping
TERMINAL_REGIONS = {
    "tyler delek": "ET-AM",
    "global hearne": "ET-AM",
    "sunoco caddo llc": "TX-AM",
    "us oil melissa": "TX-AM",
    "dallas magellan": "TX-AM / TX-PM",
    "dallas motiva": "TX-AM / TX-PM",
    "motiva enterprises llc": "Shared (All)",
    "irving exxon": "Shared (TX/FW)",
    "global dallas": "Shared (TX/FW)",
    "euless flint hills": "Shared (TX/FW)",
    "ft worth motiva": "FW-AM / FW-PM",
    "ft worth chevron": "FW-AM / FW-PM",
    "southlake nustar": "Shared (TX/FW)",
    "musket": "FW-AM",
    "cresson": "FW-AM",
    "euless kinder morgan": "FW-AM",
    "waco flint hills": "FW-AM",
    "waco motiva": "FW-AM",
    "aledo magellan": "FW-AM / FW-PM",
    "direct fuels llc": "FW-AM / TX-PM",
    "ft worth cargill": "FW-AM / FW-PM",
    "sunoco center": "ET-AM",
    "delek center": "ET-AM",
    "delek mt pleasant": "ET-AM",
}

# Region colors
REGION_COLORS = {
    "ET-AM": "#e74c3c",       # red
    "TX-AM": "#3498db",       # blue
    "TX-AM / TX-PM": "#2980b9",
    "TX-PM": "#9b59b6",       # purple
    "FW-AM": "#27ae60",       # green
    "FW-AM / FW-PM": "#2ecc71",
    "FW-PM": "#f39c12",       # orange
    "Shared (All)": "#7f8c8d",
    "Shared (TX/FW)": "#95a5a6",
    "FW-AM / TX-PM": "#1abc9c",
    "Unknown": "#bdc3c7",
}

# Yard region mapping (by city)
YARD_REGIONS = {
    "Dallas": "TX-AM / TX-PM",
    "Ft Worth": "FW-AM / FW-PM",
    "Ft Worth - Lemming": "FW-AM / FW-PM",
    "Caddo Mills": "TX-AM",
    "Tyler - Reg": "ET-AM",
    "Tyler - Zee": "ET-AM",
    "Waco": "FW-AM",
    "Melissa": "TX-AM",
}

# Build HTML with Leaflet.js
terminal_markers = []
for t in terminals:
    lat = t.get("latitude")
    lon = t.get("longitude")
    if not lat or not lon or (float(lat) == 0 and float(lon) == 0):
        continue
    name = t.get("terminal_name", "")
    region = TERMINAL_REGIONS.get(name.lower().strip(), "Unknown")
    color = REGION_COLORS.get(region, "#bdc3c7")
    terminal_markers.append({
        "lat": float(lat),
        "lon": float(lon),
        "name": name,
        "city": t.get("city", ""),
        "state": t.get("state", ""),
        "region": region,
        "color": color,
        "type": "terminal",
    })

yard_markers = []
for y in yards:
    lat = y.get("latitude")
    lon = y.get("longitude")
    if not lat or not lon or (float(lat) == 0 and float(lon) == 0):
        continue
    name = y.get("yard", "")
    region = YARD_REGIONS.get(name, "Unknown")
    color = REGION_COLORS.get(region, "#bdc3c7")
    yard_markers.append({
        "lat": float(lat),
        "lon": float(lon),
        "name": name,
        "city": y.get("city", ""),
        "state": y.get("state", ""),
        "region": region,
        "color": color,
        "type": "yard",
    })

all_markers = terminal_markers + yard_markers

html = f"""<!DOCTYPE html>
<html>
<head>
    <title>QW Transport — Terminals, Yards & Regions</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
        #map {{ height: 100vh; width: 100%; }}
        .legend {{
            background: white; padding: 12px 16px; border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.2); line-height: 1.8;
            font-size: 13px;
        }}
        .legend h4 {{ margin: 0 0 8px 0; font-size: 14px; }}
        .legend-item {{ display: flex; align-items: center; gap: 8px; }}
        .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; display: inline-block; }}
        .legend-square {{ width: 12px; height: 12px; display: inline-block; }}
    </style>
</head>
<body>
    <div id="map"></div>
    <script>
        var map = L.map('map').setView([32.8, -96.8], 8);

        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap contributors',
            maxZoom: 18,
        }}).addTo(map);

        var markers = {json.dumps(all_markers)};

        markers.forEach(function(m) {{
            var icon;
            if (m.type === 'yard') {{
                icon = L.divIcon({{
                    html: '<div style="background:' + m.color + '; width:16px; height:16px; border:2px solid white; box-shadow:0 1px 4px rgba(0,0,0,0.4);"></div>',
                    className: '',
                    iconSize: [16, 16],
                    iconAnchor: [8, 8],
                }});
            }} else {{
                icon = L.divIcon({{
                    html: '<div style="background:' + m.color + '; width:12px; height:12px; border-radius:50%; border:2px solid white; box-shadow:0 1px 4px rgba(0,0,0,0.4);"></div>',
                    className: '',
                    iconSize: [12, 12],
                    iconAnchor: [6, 6],
                }});
            }}

            L.marker([m.lat, m.lon], {{icon: icon}})
                .addTo(map)
                .bindPopup('<strong>' + m.name + '</strong><br>' +
                           m.city + ', ' + m.state + '<br>' +
                           '<em>Type: ' + m.type + '</em><br>' +
                           '<em>Region: ' + m.region + '</em>');
        }});

        // Legend
        var legend = L.control({{position: 'bottomright'}});
        legend.onAdd = function(map) {{
            var div = L.DomUtil.create('div', 'legend');
            div.innerHTML = `
                <h4>Regions</h4>
                <div class="legend-item"><span class="legend-dot" style="background:#e74c3c"></span> ET-AM (East Texas)</div>
                <div class="legend-item"><span class="legend-dot" style="background:#3498db"></span> TX-AM (Dallas AM)</div>
                <div class="legend-item"><span class="legend-dot" style="background:#9b59b6"></span> TX-PM (Dallas PM)</div>
                <div class="legend-item"><span class="legend-dot" style="background:#27ae60"></span> FW-AM (Fort Worth AM)</div>
                <div class="legend-item"><span class="legend-dot" style="background:#f39c12"></span> FW-PM (Fort Worth PM)</div>
                <div class="legend-item"><span class="legend-dot" style="background:#95a5a6"></span> Shared</div>
                <h4 style="margin-top:10px">Markers</h4>
                <div class="legend-item"><span class="legend-dot" style="background:#555"></span> Terminal (circle)</div>
                <div class="legend-item"><span class="legend-square" style="background:#555"></span> Yard (square)</div>
            `;
            return div;
        }};
        legend.addTo(map);
    </script>
</body>
</html>"""

output_path = Path(__file__).resolve().parent.parent.parent / "Data" / "regions_map.html"
with open(output_path, "w") as f:
    f.write(html)

print(f"Map generated: {output_path}")
print(f"  Terminals plotted: {len(terminal_markers)}")
print(f"  Yards plotted: {len(yard_markers)}")
print(f"  Open in browser: file://{output_path}")
