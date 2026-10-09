"""Synthetic OpenWeatherMap payloads for offline tests, evaluation, and demos.

Nothing in this module runs unless ``CLEARCAST_WEATHER_FIXTURES`` is set or a
test installs :class:`FixtureTransport` explicitly. Payload shapes follow the
public OpenWeatherMap responses that ``weather_api`` consumes, so the real
parsing, evidence-ID, unit, retry, and cache code paths are exercised offline.

Spec format: ``"<pattern>"`` or ``"<pattern>:<failure>"``, for example
``"baseline_mild"`` or ``"baseline_mild:owm_down"``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import httpx

BLOCK_SECONDS = 3 * 3600
FORECAST_BLOCKS = 40
DEFAULT_TZ_OFFSET_SECONDS = -4 * 3600


@dataclass(frozen=True)
class BlockWeather:
    """Weather for one three-hour forecast block (imperial units)."""

    temp: float
    feels_like: float
    humidity: int
    wind: float
    pop: float
    rain_3h: float
    main: str
    description: str


def _diurnal(hour_local: float, low: float, high: float) -> float:
    """Return a smooth daily temperature curve: coolest near 05:00, warmest near 15:00."""
    phase = math.cos((hour_local - 15) / 24 * 2 * math.pi)
    return round(low + (high - low) * (phase + 1) / 2, 2)


def _block(
    local: datetime,
    low: float,
    high: float,
    *,
    wind: float,
    pop: float,
    rain: float = 0.0,
    main: str = "Clouds",
    description: str = "scattered clouds",
    humidity: int = 60,
    feels_delta: float = -1.5,
) -> BlockWeather:
    temp = _diurnal(local.hour + 1.5, low, high)
    return BlockWeather(
        temp=temp,
        feels_like=round(temp + feels_delta, 2),
        humidity=humidity,
        wind=wind,
        pop=pop,
        rain_3h=rain,
        main=main,
        description=description,
    )


def _baseline_mild(local: datetime, day: int) -> BlockWeather:
    # Day-to-day variety keeps demo plans interesting: day 1 has afternoon showers.
    pops = [0.1, 0.6, 0.05, 0.3, 0.0, 0.1]
    pop = pops[day % len(pops)]
    if day % len(pops) == 1 and not 12 <= local.hour < 18:
        pop = 0.2
    wet = pop >= 0.5
    return _block(
        local,
        50,
        68,
        wind=8.0 + (local.hour % 6) * 0.4,
        pop=pop,
        rain=0.8 if wet else 0.0,
        main="Rain" if wet else "Clouds",
        description="light rain" if wet else "scattered clouds",
    )


def _rainy_mornings(local: datetime, day: int) -> BlockWeather:
    if 5 <= local.hour < 11:
        return _block(
            local,
            46,
            60,
            wind=9.0,
            pop=0.8,
            rain=1.2,
            main="Rain",
            description="light rain",
            humidity=88,
        )
    return _block(local, 46, 60, wind=7.0, pop=0.1, description="broken clouds")


def _clear_warm(local: datetime, day: int) -> BlockWeather:
    return _block(
        local,
        58,
        78,
        wind=6.0,
        pop=0.0,
        main="Clear",
        description="clear sky",
        humidity=45,
    )


def _heat_wave(local: datetime, day: int) -> BlockWeather:
    return _block(
        local,
        84,
        104,
        wind=5.0,
        pop=0.0,
        main="Clear",
        description="clear sky",
        humidity=35,
        feels_delta=3.0,
    )


def _cold_snap(local: datetime, day: int) -> BlockWeather:
    return _block(
        local,
        18,
        30,
        wind=10.0,
        pop=0.2,
        main="Snow",
        description="light snow",
        humidity=75,
        feels_delta=-8.0,
    )


def _windy(local: datetime, day: int) -> BlockWeather:
    return _block(local, 52, 66, wind=24.0, pop=0.1, description="overcast clouds")


def _storm_week(local: datetime, day: int) -> BlockWeather:
    return _block(
        local,
        55,
        65,
        wind=22.0,
        pop=0.95,
        rain=6.0,
        main="Rain",
        description="heavy intensity rain",
        humidity=94,
    )


PATTERNS: dict[str, Callable[[datetime, int], BlockWeather]] = {
    "baseline_mild": _baseline_mild,
    "rainy_mornings": _rainy_mornings,
    "clear_warm": _clear_warm,
    "heat_wave": _heat_wave,
    "cold_snap": _cold_snap,
    "windy": _windy,
    "storm_week": _storm_week,
    # Weather like baseline_mild, but with modified air quality or missing fields.
    "poor_air": _baseline_mild,
    "missing_fields": _clear_warm,
}

FAILURES = {
    "owm_down",
    "owm_rate_limited",
    "owm_timeout",
    "owm_invalid_json",
    "owm_auth_error",
    "geocode_empty",
    "forecast_missing_list",
    "aq_forecast_down",
}


@dataclass(frozen=True)
class FixtureSpec:
    """A weather pattern plus an optional injected provider failure."""

    pattern: str = "baseline_mild"
    failure: str | None = None
    tz_offset_seconds: int = DEFAULT_TZ_OFFSET_SECONDS

    @classmethod
    def parse(cls, value: str) -> FixtureSpec:
        pattern, _, failure = value.strip().partition(":")
        pattern = pattern or "baseline_mild"
        if pattern not in PATTERNS:
            raise ValueError(f"Unknown weather fixture pattern: {pattern!r}")
        if failure and failure not in FAILURES:
            raise ValueError(f"Unknown weather fixture failure: {failure!r}")
        return cls(pattern=pattern, failure=failure or None)


def _location(query: str) -> dict:
    """Derive a stable, plausible US location from the geocoding query."""
    digest = hashlib.sha256(query.casefold().encode()).digest()
    city, _, rest = query.partition(",")
    return {
        "name": city.strip().title() or "Fixture City",
        "lat": round(25 + digest[0] / 255 * 23, 4),
        "lon": round(-122 + digest[1] / 255 * 52, 4),
        "country": "US",
        "state": rest.strip(),
    }


def first_block_start(anchor: datetime) -> datetime:
    """Return the first three-hour block boundary strictly after ``anchor``."""
    ts = int(anchor.timestamp())
    return datetime.fromtimestamp((ts // BLOCK_SECONDS + 1) * BLOCK_SECONDS, UTC)


class FixtureTransport(httpx.MockTransport):
    """An httpx transport that serves synthetic OpenWeatherMap responses.

    ``calls`` records each requested path so tests can measure provider calls
    (for example, to demonstrate cache reuse).
    """

    def __init__(self, spec: FixtureSpec | str, clock: Callable[[], datetime] | None = None):
        self.spec = FixtureSpec.parse(spec) if isinstance(spec, str) else spec
        self.clock = clock or (lambda: datetime.now(UTC))
        self.calls: list[str] = []
        super().__init__(self._handle)

    # -- payload builders -------------------------------------------------
    def _local(self, ts: int) -> datetime:
        return datetime.fromtimestamp(ts, timezone(timedelta(seconds=self.spec.tz_offset_seconds)))

    def _weather_for(self, ts: int, start_ts: int) -> BlockWeather:
        local = self._local(ts)
        day = (ts - start_ts) // 86400
        return PATTERNS[self.spec.pattern](local, int(day))

    def _forecast(self, lat: float, lon: float) -> dict:
        start = int(first_block_start(self.clock()).timestamp())
        items = []
        for index in range(FORECAST_BLOCKS):
            ts = start + index * BLOCK_SECONDS
            w = self._weather_for(ts, start)
            item: dict = {
                "dt": ts,
                "main": {"temp": w.temp, "feels_like": w.feels_like, "humidity": w.humidity},
                "weather": [{"main": w.main, "description": w.description}],
                "wind": {"speed": w.wind},
                "pop": w.pop,
                "dt_txt": datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S"),
            }
            if w.rain_3h:
                item["rain"] = {"3h": w.rain_3h}
            if self.spec.pattern == "missing_fields" and index % 2 == 1:
                # Odd blocks lose wind and precipitation probability entirely.
                del item["wind"]
                del item["pop"]
            items.append(item)
        payload: dict = {
            "cod": "200",
            "cnt": len(items),
            "list": items,
            "city": {
                "name": "Fixture City",
                "country": "US",
                "timezone": self.spec.tz_offset_seconds,
                "coord": {"lat": lat, "lon": lon},
            },
        }
        if self.spec.failure == "forecast_missing_list":
            del payload["list"]
        return payload

    def _current(self, lat: float, lon: float) -> dict:
        now = int(self.clock().timestamp()) - 600
        w = self._weather_for(now, now)
        return {
            "coord": {"lat": lat, "lon": lon},
            "weather": [{"main": w.main, "description": w.description}],
            "main": {"temp": w.temp, "feels_like": w.feels_like, "humidity": w.humidity},
            "wind": {"speed": w.wind},
            "clouds": {"all": 40},
            "dt": now,
            "timezone": self.spec.tz_offset_seconds,
            "name": "Fixture City",
        }

    def _aqi(self) -> int:
        return 4 if self.spec.pattern == "poor_air" else 2

    def _air(self, lat: float, lon: float, forecast: bool) -> dict:
        now = int(self.clock().timestamp())
        hour = now - now % 3600
        hours = range(0, 96) if forecast else range(0, 1)
        return {
            "coord": {"lat": lat, "lon": lon},
            "list": [
                {
                    "dt": hour + offset * 3600,
                    "main": {"aqi": self._aqi()},
                    "components": {"pm2_5": 8.4, "pm10": 14.2, "o3": 61.0},
                }
                for offset in hours
            ],
        }

    # -- request routing --------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        failure = self.spec.failure
        params = request.url.params
        if failure == "owm_timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
        if failure == "owm_down":
            return httpx.Response(503, json={"cod": 503, "message": "unavailable"})
        if failure == "owm_rate_limited":
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"cod": 429})
        if failure == "owm_auth_error":
            return httpx.Response(401, json={"cod": 401, "message": "Invalid API key"})

        if path == "/geo/1.0/direct":
            if failure == "geocode_empty":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[_location(params.get("q", ""))])

        lat = float(params.get("lat", 0))
        lon = float(params.get("lon", 0))
        if path == "/data/2.5/forecast":
            if failure == "owm_invalid_json":
                return httpx.Response(200, text="<html>not json</html>")
            return httpx.Response(200, json=self._forecast(lat, lon))
        if path == "/data/2.5/weather":
            return httpx.Response(200, json=self._current(lat, lon))
        if path == "/data/2.5/air_pollution":
            return httpx.Response(200, json=self._air(lat, lon, forecast=False))
        if path == "/data/2.5/air_pollution/forecast":
            if failure == "aq_forecast_down":
                return httpx.Response(503, json={"cod": 503})
            return httpx.Response(200, json=self._air(lat, lon, forecast=True))
        return httpx.Response(404, content=json.dumps({"cod": 404}).encode())
