"""
Geo utilities: haversine distance, Google Maps Routes API integration.
All fuel loads use hazmat routing.

Caching strategy
----------------
Travel times between the same two points don't meaningfully change within a
single dispatch run (and barely change hour-to-hour on Texas highways).

_travel_cache stores results keyed by:
    (round(lat1,4), round(lon1,4), round(lat2,4), round(lon2,4), epoch_bucket)

where epoch_bucket = departure_epoch // 900  (15-minute buckets).
Rounding to 4 decimal places gives ~11 m precision — more than enough for
terminal/site coordinates, and ensures two drivers going to the same terminal
share the same cache entry.

The cache lives for the lifetime of the process (one dispatch run on Render).
Call clear_travel_cache() between runs if needed.

Quota impact
------------
Google Routes API free tier: 100 requests/day.
A typical dispatch (15 drivers × 4 stops × 2 legs) = ~120 unique legs.
With caching, most legs repeat (same terminal→site pairs), dropping real API
calls to ~20–30 per run — well within the free quota.
"""

import math
import os
import logging
import asyncio
import time as _time_mod
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
TRAVEL_SPEED_MPH = 50  # fallback if Maps API unavailable
LOAD_SERVICE_MINS = 45
UNLOAD_SERVICE_MINS = 45

# Tanker trailer adjustment: real-world driving times for heavy commercial
# vehicles run ~20% longer than Google Maps passenger-car estimates.
TANKER_TRAVEL_MULTIPLIER = 1.20

# ---------------------------------------------------------------------------
# In-process travel time cache
# ---------------------------------------------------------------------------
_travel_cache: dict[tuple, float] = {}


def _cache_key(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    departure_epoch: Optional[int],
) -> tuple:
    """Round coords to 4dp (~11m) and bucket departure time to 15-min slots."""
    bucket = (departure_epoch // 900) if departure_epoch else 0
    return (round(lat1, 4), round(lon1, 4), round(lat2, 4), round(lon2, 4), bucket)


def clear_travel_cache():
    """Clear the cache between dispatch runs if needed."""
    _travel_cache.clear()
    log.info("Travel time cache cleared")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def haversine_travel_mins(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Estimate travel time in minutes using haversine + constant speed fallback."""
    miles = haversine_miles(lat1, lon1, lat2, lon2)
    return (miles / TRAVEL_SPEED_MPH) * 60


async def google_maps_travel_mins(
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
    departure_time_epoch: Optional[int] = None,
) -> Optional[float]:
    """
    Call Google Maps Routes API for driving duration with traffic.
    Returns travel time in minutes, or None if API call fails.

    departure_time_epoch: Unix timestamp (seconds). Must be a future time.
    If None or in the past, omits departureTime so Google uses current time.
    """
    if not GOOGLE_MAPS_API_KEY:
        return None

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }

    body = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lon}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lon}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "routeModifiers": {
            "vehicleInfo": {"emissionType": "GASOLINE"},
            "avoidTolls": False,
            "avoidHighways": False,
            "avoidFerries": True,
        },
    }

    # Google Routes API v2 requires departureTime as an RFC3339 string,
    # and it must be a future timestamp. If the epoch is in the past (or
    # not provided), omit it — Google will use current time for traffic.
    if departure_time_epoch and departure_time_epoch > _time_mod.time() + 60:
        dt_utc = datetime.fromtimestamp(departure_time_epoch, tz=timezone.utc)
        body["departureTime"] = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            routes = data.get("routes", [])
            if routes:
                duration_str = routes[0].get("duration", "0s")
                seconds = int(duration_str.rstrip("s"))
                return seconds / 60
    except Exception as e:
        log.warning(f"Google Maps API error: {e} — falling back to haversine")
    return None


async def get_travel_mins(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    departure_epoch: Optional[int] = None,
) -> float:
    """
    Get travel time in minutes. Try Google Maps first, fall back to haversine.
    Applies TANKER_TRAVEL_MULTIPLIER to account for the additional time a
    heavy tanker trailer takes vs. the passenger-car baseline Google returns.
    """
    maps_result = await google_maps_travel_mins(lat1, lon1, lat2, lon2, departure_epoch)
    base = maps_result if maps_result is not None else haversine_travel_mins(lat1, lon1, lat2, lon2)
    return base * TANKER_TRAVEL_MULTIPLIER


# Per-process cache: (lat1, lon1, lat2, lon2) rounded to 3 dp → travel minutes.
# Keyed without departure_epoch — close-enough for routing engine simulation loops
# where the same yard→terminal or terminal→site leg is evaluated hundreds of times.
_travel_cache: dict[tuple, float] = {}


def get_travel_mins_sync(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    departure_epoch: Optional[int] = None,
) -> float:
    """Synchronous wrapper for get_travel_mins with in-process caching.

    Cache key is (lat1, lon1, lat2, lon2) rounded to 4dp + 15-min epoch bucket.
    This means repeated legs (same terminal → same site) hit the cache instead
    of consuming Google Maps quota.

    Always creates a fresh event loop so this works correctly when called from
    a thread pool executor (run_in_executor in FastAPI). Using
    asyncio.get_event_loop() from a worker thread raises RuntimeError in
    Python 3.10+ and would silently fall back to haversine.
    """
    key = _cache_key(lat1, lon1, lat2, lon2, departure_epoch)
    if key in _travel_cache:
        return _travel_cache[key]

    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                get_travel_mins(lat1, lon1, lat2, lon2, departure_epoch)
            )
        finally:
            loop.close()
    except Exception as e:
        log.warning(f"Google Maps API error: {e} — falling back to haversine")
        result = haversine_travel_mins(lat1, lon1, lat2, lon2)

    _travel_cache[key] = result
    return result
