"""
Calculate loaded mile ratio from actuals by estimating empty miles.

Empty miles = deadhead from previous site (or yard) to next terminal.
We can estimate this by looking at sequential deliveries per driver
and computing distance between last site and next terminal using haversine.
"""
import csv
import math
import json
import urllib.request
from pathlib import Path
from collections import defaultdict

# Load terminal locations from Supabase
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
req = urllib.request.Request(f"{url}/rest/v1/terminal_locations?select=terminal_name,latitude,longitude", headers=headers)
with urllib.request.urlopen(req, timeout=10) as resp:
    terminal_data = json.loads(resp.read())
terminal_coords = {t["terminal_name"].lower().strip(): (float(t["latitude"]), float(t["longitude"]))
                   for t in terminal_data if t.get("latitude") and t.get("longitude")}

# Fetch sites
req = urllib.request.Request(f"{url}/rest/v1/site_details?select=site_name,latitude,longitude&limit=5000", headers=headers)
with urllib.request.urlopen(req, timeout=15) as resp:
    site_data = json.loads(resp.read())
site_coords = {s["site_name"].lower().strip(): (float(s["latitude"]), float(s["longitude"]))
               for s in site_data if s.get("latitude") and s.get("longitude")}

# Fetch yards
req = urllib.request.Request(f"{url}/rest/v1/yard_locations?select=yard,latitude,longitude", headers=headers)
with urllib.request.urlopen(req, timeout=10) as resp:
    yard_data = json.loads(resp.read())
yard_coords = {y["yard"].lower().strip(): (float(y["latitude"]), float(y["longitude"]))
               for y in yard_data if y.get("latitude") and y.get("longitude")}


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))


csv_path = Path(__file__).resolve().parent.parent.parent / "Data" / "Delivery Data 6.22 to 6.26.csv"

# Group deliveries by driver, ordered by time
driver_deliveries = defaultdict(list)
with open(csv_path) as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row["ScheduleDate"].split(" ")[0] != "6/23/2026":
            continue
        driver = row.get("driver", "").strip()
        if not driver:
            continue
        driver_deliveries[driver].append(row)

# Sort each driver's deliveries by ArrivedAtRackTime
for driver in driver_deliveries:
    driver_deliveries[driver].sort(key=lambda r: r.get("ArrivedAtRackTime", ""))

total_loaded_miles = 0
total_empty_miles = 0
missed = 0

for driver, deliveries in driver_deliveries.items():
    prev_lat, prev_lon = None, None

    for i, row in enumerate(deliveries):
        loaded_miles = float(row.get("Miles") or 0)
        total_loaded_miles += loaded_miles

        terminal_name = (row.get("TerminalName") or "").lower().strip()
        site_name = (row.get("Dealer") or "").lower().strip()

        terminal_loc = terminal_coords.get(terminal_name)
        site_loc = site_coords.get(site_name)

        if i == 0:
            # First delivery: empty miles = yard → terminal
            # We don't know the yard per driver from the CSV, so estimate ~10 miles
            total_empty_miles += 10
        else:
            # Subsequent: empty miles = previous site → this terminal
            if prev_lat and prev_lon and terminal_loc:
                empty = haversine_miles(prev_lat, prev_lon, terminal_loc[0], terminal_loc[1])
                total_empty_miles += empty
            else:
                total_empty_miles += 15  # fallback estimate
                missed += 1

        # Update prev position to this delivery's site
        if site_loc:
            prev_lat, prev_lon = site_loc
        else:
            prev_lat, prev_lon = None, None

    # After last delivery: empty miles = last site → yard (~10 miles estimate)
    total_empty_miles += 10

total_miles = total_loaded_miles + total_empty_miles
ratio = total_loaded_miles / total_miles if total_miles > 0 else 0

print(f"June 23, 2026 — Actuals Loaded Mile Ratio")
print(f"=" * 50)
print(f"  Deliveries: {sum(len(d) for d in driver_deliveries.values())}")
print(f"  Drivers: {len(driver_deliveries)}")
print(f"  Total loaded miles: {total_loaded_miles:.0f}")
print(f"  Total empty miles (estimated): {total_empty_miles:.0f}")
print(f"  Total miles: {total_miles:.0f}")
print(f"  Loaded mile ratio: {ratio:.1%}")
print(f"  (Missed geo lookups: {missed})")
