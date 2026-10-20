"""Current weather and the times of sunrise and sunset for the background of the design "Himmel".

Open-Meteo needs no key. Coordinates are rounded to about a kilometre and cached for ten minutes, so the exact
position of a device never leaves Lares and Open-Meteo is asked rarely.
"""

import asyncio
import os
import time

import aiohttp

OPEN_METEO = os.environ.get("LARES_OPEN_METEO", "https://api.open-meteo.com/v1/forecast")
CACHE_SECONDS = 600
CURRENT = "temperature_2m,weather_code,cloud_cover,precipitation,rain,snowfall,wind_speed_10m,wind_direction_10m,is_day"


class WeatherError(RuntimeError):
    pass


class Weather:
    def __init__(self, http: aiohttp.ClientSession):
        self.http = http
        self.cache: dict[tuple[float, float], tuple[float, dict]] = {}

    async def at(self, lat: float, lon: float) -> dict:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise WeatherError("Ort ungültig")
        key = (round(lat, 2), round(lon, 2))
        cached = self.cache.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        params = {"latitude": key[0], "longitude": key[1], "current": CURRENT, "daily": "sunrise,sunset",
                  "timezone": "auto", "forecast_days": 2, "timeformat": "unixtime"}
        try:
            async with self.http.get(OPEN_METEO, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 300:
                    raise WeatherError(f"Wetterdienst meldet {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise WeatherError("Wetterdienst nicht erreichbar") from None
        current, daily = data.get("current") or {}, data.get("daily") or {}
        out = {
            "ort": {"lat": key[0], "lon": key[1]},
            "temperatur": current.get("temperature_2m"),
            "code": int(current.get("weather_code") or 0),
            "wolken": int(current.get("cloud_cover") or 0),
            "niederschlag": float(current.get("precipitation") or 0),
            "regen": float(current.get("rain") or 0),
            "schnee": float(current.get("snowfall") or 0),
            "wind": float(current.get("wind_speed_10m") or 0),
            "windrichtung": float(current.get("wind_direction_10m") or 0),
            "aufgang": daily.get("sunrise") or [],
            "untergang": daily.get("sunset") or [],
            "stand": int(time.time()),
        }
        if len(self.cache) > 500:
            self.cache.clear()
        self.cache[key] = (time.time() + CACHE_SECONDS, out)
        return out
