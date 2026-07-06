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

api_key = env_vars.get("GOOGLE_MAPS_API_KEY")
print(f"Key: {api_key[:12]}...{api_key[-4:]}")

# Test: Houston to Austin
url = "https://routes.googleapis.com/directions/v2:computeRoutes"
payload = {
    "origin": {"location": {"latLng": {"latitude": 29.7604, "longitude": -95.3698}}},
    "destination": {"location": {"latLng": {"latitude": 30.2672, "longitude": -97.7431}}},
    "travelMode": "DRIVE",
    "routingPreference": "TRAFFIC_AWARE",
}
headers = {
    "Content-Type": "application/json",
    "X-Goog-Api-Key": api_key,
    "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
}

data = json.dumps(payload).encode()
req = urllib.request.Request(url, data=data, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        routes = result.get("routes", [])
        if routes:
            duration = routes[0].get("duration", "N/A")
            distance = routes[0].get("distanceMeters", 0)
            print(f"\nSUCCESS! Routes API is working.")
            print(f"  Houston → Austin:")
            print(f"  Duration: {duration}")
            print(f"  Distance: {distance} meters ({int(distance)/1609:.1f} miles)")
        else:
            print(f"\nResponse had no routes: {result}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"\nHTTP Error {e.code}:")
    print(body[:500])
except Exception as e:
    print(f"\nError: {e}")
