"""
Geo utilities: haversine distance, Google Maps Routes API integration.
All fuel loads use hazmat routing.
"""

import math
import os
import logging
import asyncio
from functools import lru_cache
from typing import Optional

import httpx

log = logging.getLogger(__name__)

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
TRAVEL_SPEED_MPH = 50  # fallback if Maps API unavailable
LOAD_SERVICE_MINS = 45
UNLOAD_SERVICE_MINS = 45


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
    Uses HAZMAT routing (avoid tunnels, comply with hazmat restrictions).
    """
    if not GOOGLE_MAPS_API_KEY:
        return None

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs",
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

    if departure_time_epoch:
        body["departureTime"] = {"seconds": departure_time_epoch}

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
    """
    maps_result = await google_maps_travel_mins(lat1, lon1, lat2, lon2, departure_epoch)
    if maps_result is not None:
        return maps_result
    return haversine_travel_mins(lat1, lon1, lat2, lon2)


# Per-process cache: (lat1, lon1, lat2, lon2) rounded to 3 dp → travel minutes.
# Keyed without departure_epoch — close-enough for routing engine simulation loops
# where the same yard→terminal or terminal→site leg is evaluated hundreds of times.
_travel_cache: dict[tuple, float] = {}


def clear_travel_cache():
    """Clear the in-process travel time cache between dispatch runs."""
    _travel_cache.clear()
    log.info("Travel time cache cleared")


def get_travel_mins_sync(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    departure_epoch: Optional[int] = None,
) -> float:
    """Synchronous wrapper for get_travel_mins, with in-process cache."""
    cache_key = (round(lat1, 3), round(lon1, 3), round(lat2, 3), round(lon2, 3))
    if cache_key in _travel_cache:
        return _travel_cache[cache_key]

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Inside async context (FastAPI handler) — use haversine only
            result = haversine_travel_mins(lat1, lon1, lat2, lon2)
        else:
            result = loop.run_until_complete(
                get_travel_mins(lat1, lon1, lat2, lon2, departure_epoch)
            )
    except Exception:
        result = haversine_travel_mins(lat1, lon1, lat2, lon2)

    _travel_cache[cache_key] = result
    return result
