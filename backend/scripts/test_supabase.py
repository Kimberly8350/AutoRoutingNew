import os
from pathlib import Path
import urllib.request
import json
from collections import defaultdict

env_path = Path(".env")
env_vars = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            env_vars[key.strip()] = val.strip()

url = env_vars.get("SUPABASE_URL")
key = env_vars.get("SUPABASE_SERVICE_KEY")

# Get terminal-to-region mapping: which terminals are used by which board_location
# Look at historical load_details + driver_schedules to see which terminals
# are served by which regions
headers = {
    "apikey": key,
    "Authorization": f"Bearer {key}",
}

# Get all terminals
test_url = f"{url}/rest/v1/terminal_locations?select=terminal_id,terminal_name,city,state"
req = urllib.request.Request(test_url, headers=headers)
with urllib.request.urlopen(req, timeout=10) as resp:
    terminals = json.loads(resp.read())

print(f"Terminals ({len(terminals)}):")
for t in terminals:
    print(f"  ID: {t['terminal_id']:<6} {t['terminal_name']:<30} {t.get('city','')}, {t.get('state','')}")

# Check what board_locations the drivers loading from each terminal typically have
# by cross-referencing load_details (has terminal_name + driver first/last)
# with driver_schedules (has driver + board_location)
print("\n\nTo map terminals → regions, we need to see which drivers (by region)")
print("historically load from each terminal. Checking actuals CSV...")

import csv
terminal_region = defaultdict(lambda: defaultdict(int))
with open("../Data/Delivery Data 6.22 to 6.26.csv") as f:
    reader = csv.DictReader(f)
    for row in reader:
        terminal = row.get("TerminalName", "").strip()
        shift = row.get("Shift", "").strip()  # This IS the board_location/region
        if terminal and shift:
            terminal_region[terminal][shift] += 1

print(f"\nTerminal → Region mapping (from actuals):\n")
print(f"{'Terminal':<30} {'Regions (delivery count)'}")
print(f"{'-'*30} {'-'*50}")
for terminal in sorted(terminal_region.keys()):
    regions = terminal_region[terminal]
    region_str = ", ".join(f"{r}({c})" for r, c in sorted(regions.items(), key=lambda x: -x[1]))
    print(f"{terminal:<30} {region_str}")
