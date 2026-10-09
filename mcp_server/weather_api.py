"""Raw OpenWeatherMap HTTP API layer, separated from MCP for clean architecture.

This module owns provider access: authentication, timeouts, bounded retries
with exponential backoff, HTTP 429 handling, response normalisation, units,
evidence identifiers, fetch timestamps, and a short-lived cache. Error
messages never include request URLs or API keys.
"""

from __future__ import annotations

import copy
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=False)

# httpx logs full request URLs at INFO level, and OpenWeatherMap authenticates
# with an ``appid`` query parameter, so these loggers must never emit INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BASE_URL = "https://api.openweathermap.org"
SOURCE_NAME = "openweathermap"
FIXTURE_ENV_VAR = "CLEARCAST_WEATHER_FIXTURES"
CACHE_TTL_ENV_VAR = "CLEARCAST_WEATHER_CACHE_TTL_SECONDS"

UNITS = "imperial"
FORECAST_UNITS = {
    "temperature": "°F",
    "feels_like": "°F",
    "humidity": "%",
    "wind_speed": "mph",
    "rain_probability": "fraction 0-1 (probability of precipitation)",
    "rain_volume_3h": "mm per 3 hours",
}
CURRENT_UNITS = {
    "temperature": "°F",
    "feels_like": "°F",
    "humidity": "%",
    "wind_speed": "mph",
    "cloud_cover": "%",
}
AIR_UNITS = {"pm2_5": "µg/m³", "pm10": "µg/m³", "o3": "µg/m³", "aqi": "index 1-5"}
AQI_SCALE = "OpenWeatherMap AQI: 1=Good, 2=Fair, 3=Moderate, 4=Poor, 5=Very Poor"

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 4.0
RETRY_AFTER_CAP_SECONDS = 5.0
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
DEFAULT_CACHE_TTL_SECONDS = 600
GEOCODE_CACHE_TTL_SECONDS = 24 * 3600
CACHE_MAX_ENTRIES = 256
BLOCK_SECONDS = 3 * 3600
PLACEHOLDER_MARKERS = ("your_", "your-", "placeholder", "replace_me", "changeme")


class WeatherProviderError(RuntimeError):
    """Provider failure with a stable category and a message safe to display.

    ``str(error)`` is ``"[category] message"`` so the category survives the MCP
    text boundary and can be recovered by the agent's evidence layer.
    """

    def __init__(
        self,
        category: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.safe_message = message
        self.retryable = retryable
        self.status_code = status_code


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(value: datetime | float) -> str:
    moment = value if isinstance(value, datetime) else datetime.fromtimestamp(value, UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_local(ts: float, offset_seconds: int | None) -> str | None:
    if offset_seconds is None:
        return None
    tz = timezone(timedelta(seconds=offset_seconds))
    return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")


def observation_id(prefix: str, ts: float) -> str:
    """Deterministic evidence identifier derived from the provider timestamp."""
    return f"{prefix}-{datetime.fromtimestamp(ts, UTC):%Y%m%dT%H%MZ}"


def _dig(container: Any, *path: str | int) -> Any:
    current = container
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, list) or len(current) <= key:
                return None
        elif not isinstance(current, dict):
            return None
        current = current[key] if isinstance(key, int) else current.get(key)
    return current


def _number(container: Any, *path: str | int) -> float | None:
    value = _dig(container, *path)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class _TTLCache:
    """Small thread-safe cache of raw provider responses with original fetch times."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._entries: OrderedDict[tuple, tuple[datetime, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, key: tuple, ttl: float, now: datetime) -> tuple[datetime, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            fetched_at, _ = entry
            if ttl <= 0 or (now - fetched_at).total_seconds() > ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: tuple, fetched_at: datetime, payload: Any) -> None:
        with self._lock:
            self._entries[key] = (fetched_at, payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _cache_ttl_from_env() -> float:
    raw = (os.getenv(CACHE_TTL_ENV_VAR) or "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS


class OpenWeatherMapClient:
    """Resilient OpenWeatherMap client used by the MCP weather tools."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        cache_ttl_seconds: float | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        source: str = SOURCE_NAME,
        require_api_key: bool = True,
    ) -> None:
        self._explicit_api_key = api_key
        self._clock = clock or _utc_now
        self._sleep = sleep or time.sleep
        self._cache_ttl = _cache_ttl_from_env() if cache_ttl_seconds is None else cache_ttl_seconds
        self._max_attempts = max(1, max_attempts)
        self._require_api_key = require_api_key
        self.source = source
        self._cache = _TTLCache()
        self._http = httpx.Client(base_url=BASE_URL, timeout=DEFAULT_TIMEOUT, transport=transport)

    def close(self) -> None:
        self._http.close()

    # -- transport --------------------------------------------------------
    def _api_key(self) -> str:
        """Return the validated API key without exposing its value."""
        key = (self._explicit_api_key or os.getenv("OPENWEATHERMAP_API_KEY") or "").strip()
        if not self._require_api_key:
            return key or "offline-fixture"
        if not key:
            raise WeatherProviderError("config_missing", "OPENWEATHERMAP_API_KEY environment variable is missing")
        if any(marker in key.casefold() for marker in PLACEHOLDER_MARKERS):
            raise WeatherProviderError("config_missing", "OPENWEATHERMAP_API_KEY appears to contain placeholder text")
        return key

    @staticmethod
    def _error_for_status(status: int) -> WeatherProviderError:
        if status in (401, 403):
            return WeatherProviderError(
                "provider_auth_error",
                f"OpenWeatherMap rejected the API key (HTTP {status})",
                status_code=status,
            )
        if status == 404:
            return WeatherProviderError(
                "provider_not_found", "OpenWeatherMap resource not found (HTTP 404)", status_code=404
            )
        if status == 429:
            return WeatherProviderError(
                "provider_rate_limited",
                "OpenWeatherMap rate limit reached (HTTP 429)",
                retryable=True,
                status_code=429,
            )
        if 400 <= status < 500:
            return WeatherProviderError(
                "provider_bad_request",
                f"OpenWeatherMap rejected the request (HTTP {status})",
                status_code=status,
            )
        return WeatherProviderError(
            "provider_unavailable",
            f"OpenWeatherMap is unavailable (HTTP {status})",
            retryable=True,
            status_code=status,
        )

    def _backoff_seconds(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), RETRY_AFTER_CAP_SECONDS)
            except ValueError:
                pass
        delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        return delay * (0.5 + random.random() / 2)

    def _get(self, path: str, params: dict) -> Any:
        """Call an endpoint with bounded retries and return decoded JSON."""
        query = {**params, "appid": self._api_key()}
        for attempt in range(1, self._max_attempts + 1):
            retry_after = None
            try:
                response = self._http.get(path, params=query)
            except httpx.TimeoutException:
                error = WeatherProviderError("provider_timeout", "OpenWeatherMap request timed out", retryable=True)
            except httpx.TransportError:
                # Transport exception strings can contain the full URL, including appid.
                error = WeatherProviderError(
                    "provider_unavailable",
                    "OpenWeatherMap request failed due to a network error",
                    retryable=True,
                )
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        raise WeatherProviderError(
                            "provider_bad_response", "OpenWeatherMap returned invalid JSON"
                        ) from None
                error = self._error_for_status(response.status_code)
                retry_after = response.headers.get("Retry-After")

            if not error.retryable or attempt == self._max_attempts:
                if attempt > 1:
                    error = WeatherProviderError(
                        error.category,
                        f"{error.safe_message} after {attempt} attempts",
                        retryable=error.retryable,
                        status_code=error.status_code,
                    )
                raise error from None
            self._sleep(self._backoff_seconds(attempt, retry_after))
        raise AssertionError("unreachable")  # pragma: no cover

    def _get_cached(self, kind: str, path: str, params: dict, ttl: float) -> tuple[Any, datetime, bool]:
        key = (kind, path, tuple(sorted(params.items())))
        now = self._clock()
        cached = self._cache.get(key, ttl, now)
        if cached is not None:
            fetched_at, payload = cached
            return copy.deepcopy(payload), fetched_at, True
        payload = self._get(path, params)
        fetched_at = self._clock()
        if ttl > 0 and payload:
            self._cache.put(key, fetched_at, copy.deepcopy(payload))
        return payload, fetched_at, False

    def _provenance(self, fetched_at: datetime, cache_hit: bool) -> dict:
        age = max(0, int((self._clock() - fetched_at).total_seconds()))
        return {
            "source": self.source,
            "fetched_at_utc": _iso_utc(fetched_at),
            "cache": {"hit": cache_hit, "age_seconds": age if cache_hit else 0},
        }

    @staticmethod
    def _bad_response(detail: str) -> WeatherProviderError:
        return WeatherProviderError("provider_bad_response", f"OpenWeatherMap response {detail}")

    # -- tools ------------------------------------------------------------
    def geocode_city(self, city: str, country_code: str = "", limit: int = 1) -> dict:
        """Convert a city name into coordinates using OpenWeatherMap geocoding."""
        # Geocoding is needed because forecast/weather endpoints require coordinates.
        query = f"{city},{country_code}" if country_code else city
        results, fetched_at, hit = self._get_cached(
            "geocode", "/geo/1.0/direct", {"q": query, "limit": limit}, GEOCODE_CACHE_TTL_SECONDS
        )
        if not isinstance(results, list):
            raise self._bad_response("for geocoding was not a list")
        if not results:
            raise WeatherProviderError("location_not_found", f"No geocoding results found for '{query[:80]}'")
        first = results[0]
        lat, lon = _number(first, "lat"), _number(first, "lon")
        if lat is None or lon is None or not isinstance(first.get("name"), str):
            raise self._bad_response("for geocoding is missing name or coordinates")
        return {
            "lat": lat,
            "lon": lon,
            "name": first["name"],
            "state": first.get("state", "") or "",
            "country": first.get("country", "") or "",
            "observation_id": f"geo-{lat:.4f}_{lon:.4f}",
            **self._provenance(fetched_at, hit),
        }

    def get_current_weather(self, lat: float, lon: float, units: str = UNITS) -> dict:
        """Return the current conditions needed by the marketing agent."""
        params = {"lat": round(lat, 4), "lon": round(lon, 4), "units": units}
        data, fetched_at, hit = self._get_cached("current", "/data/2.5/weather", params, self._cache_ttl)
        dt = _number(data, "dt")
        temperature = _number(data, "main", "temp")
        if dt is None or temperature is None:
            raise self._bad_response("for current weather is missing dt or temperature")
        offset = _number(data, "timezone")
        fields = {
            "feels_like": _number(data, "main", "feels_like"),
            "humidity": _number(data, "main", "humidity"),
            "wind_speed": _number(data, "wind", "speed"),
            "cloud_cover": _number(data, "clouds", "all"),
        }
        description = _dig(data, "weather", 0, "description")
        result = {
            # Cleaned fields the LLM needs, plus provenance and units.
            "city": data.get("name") if isinstance(data, dict) else None,
            "temperature": temperature,
            **fields,
            "description": description if isinstance(description, str) else None,
            "observation_id": observation_id("cw", dt),
            "observed_at_utc": _iso_utc(dt),
            "observed_at_local": _iso_local(dt, int(offset) if offset is not None else None),
            "timezone_offset_seconds": int(offset) if offset is not None else None,
            "units": CURRENT_UNITS,
            **self._provenance(fetched_at, hit),
        }
        missing = [name for name, value in fields.items() if value is None]
        if result["description"] is None:
            missing.append("description")
        if missing:
            result["missing_fields"] = missing
        return result

    @staticmethod
    def _forecast_entry(item: Any, offset: int | None) -> dict | None:
        dt = _number(item, "dt")
        if dt is None:
            return None
        values = {
            "temperature": _number(item, "main", "temp"),
            "feels_like": _number(item, "main", "feels_like"),
            "humidity": _number(item, "main", "humidity"),
            "wind_speed": _number(item, "wind", "speed"),
            "rain_probability": _number(item, "pop"),
        }
        pop = values["rain_probability"]
        if pop is not None and not 0.0 <= pop <= 1.0:
            values["rain_probability"] = None
        description = _dig(item, "weather", 0, "description")
        dt_txt = item.get("dt_txt") if isinstance(item, dict) else None
        entry = {
            "observation_id": observation_id("fc", dt),
            # ``datetime`` is the original UTC provider text, kept for compatibility.
            "datetime": dt_txt if isinstance(dt_txt, str) else _iso_utc(dt),
            "datetime_utc": _iso_utc(dt),
            "datetime_local": _iso_local(dt, offset),
            **values,
            "description": description if isinstance(description, str) else None,
            # OpenWeatherMap omits ``rain`` when no rain is expected, so absence means 0 mm.
            "rain_volume_3h": _number(item, "rain", "3h") or 0.0,
        }
        missing = [name for name, value in values.items() if value is None]
        if entry["description"] is None:
            missing.append("description")
        if missing:
            entry["missing_fields"] = missing
        return entry

    def get_forecast(self, lat: float, lon: float, units: str = UNITS) -> dict:
        """Return the five-day forecast as cleaned three-hour blocks."""
        params = {"lat": round(lat, 4), "lon": round(lon, 4), "units": units}
        data, fetched_at, hit = self._get_cached("forecast", "/data/2.5/forecast", params, self._cache_ttl)
        raw_list = _dig(data, "list")
        if not isinstance(raw_list, list):
            raise self._bad_response("for forecast is missing the 'list' field")
        offset_value = _number(data, "city", "timezone")
        offset = int(offset_value) if offset_value is not None else None
        entries = [entry for item in raw_list if (entry := self._forecast_entry(item, offset))]
        result = {
            "city": _dig(data, "city", "name"),
            "country": _dig(data, "city", "country"),
            "timezone_offset_seconds": offset,
            "block_hours": 3,
            "observation_count": len(entries),
            "skipped_entries": len(raw_list) - len(entries),
            "units": FORECAST_UNITS,
            **self._provenance(fetched_at, hit),
            # 3-hour blocks let the LLM identify the best dayparts for campaigns.
            "forecast": entries,
        }
        return result

    def get_air_quality(self, lat: float, lon: float) -> dict:
        """Return current AQI plus a three-hour AQI forecast when available."""
        params = {"lat": round(lat, 4), "lon": round(lon, 4)}
        data, fetched_at, hit = self._get_cached("air", "/data/2.5/air_pollution", params, self._cache_ttl)
        reading = _dig(data, "list", 0)
        aqi = _number(reading, "main", "aqi")
        dt = _number(reading, "dt")
        if aqi is None or dt is None or not 1 <= aqi <= 5:
            raise self._bad_response("for air quality is missing a valid AQI reading")
        components = {name: _number(reading, "components", name) for name in ("pm2_5", "pm10", "o3")}
        result: dict = {
            "aqi": int(aqi),
            **components,
            "observation_id": observation_id("aqc", dt),
            "observed_at_utc": _iso_utc(dt),
            "scale": AQI_SCALE,
            "units": AIR_UNITS,
            **self._provenance(fetched_at, hit),
        }
        missing = [name for name, value in components.items() if value is None]
        if missing:
            result["missing_fields"] = missing
        result.update(self._air_quality_forecast(params))
        return result

    def _air_quality_forecast(self, params: dict) -> dict:
        """Aggregate the hourly AQI forecast into three-hour blocks (max AQI per block)."""
        try:
            data, fetched_at, _ = self._get_cached(
                "air_forecast", "/data/2.5/air_pollution/forecast", params, self._cache_ttl
            )
        except WeatherProviderError as exc:
            return {"forecast_available": False, "forecast_error": exc.category, "forecast": []}
        blocks: dict[int, int] = {}
        for item in _dig(data, "list") or []:
            dt, aqi = _number(item, "dt"), _number(item, "main", "aqi")
            if dt is None or aqi is None or not 1 <= aqi <= 5:
                continue
            start = int(dt) - int(dt) % BLOCK_SECONDS
            blocks[start] = max(blocks.get(start, 0), int(aqi))
        forecast = [
            {
                "observation_id": observation_id("aqf", start),
                "datetime_utc": _iso_utc(start),
                "aqi_max": blocks[start],
            }
            for start in sorted(blocks)
        ]
        return {
            "forecast_available": bool(forecast),
            "forecast_fetched_at_utc": _iso_utc(fetched_at),
            "forecast": forecast,
        }


_default_client: OpenWeatherMapClient | None = None
_default_lock = threading.Lock()


def _client_from_env() -> OpenWeatherMapClient:
    fixture = (os.getenv(FIXTURE_ENV_VAR) or "").strip()
    if fixture:
        # Offline mode for tests and demos: synthetic payloads, no network, no key.
        from mcp_server.fixtures import FixtureTransport

        return OpenWeatherMapClient(
            transport=FixtureTransport(fixture),
            source=f"fixture:{fixture}",
            require_api_key=False,
        )
    return OpenWeatherMapClient()


def get_client() -> OpenWeatherMapClient:
    """Return the process-wide client, created lazily from the environment."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = _client_from_env()
        return _default_client


def set_client(client: OpenWeatherMapClient | None) -> None:
    """Install a specific client (tests) or reset to environment configuration."""
    global _default_client
    with _default_lock:
        _default_client = client


# Backwards-compatible module-level functions used by the MCP server.
def geocode_city(city: str, country_code: str = "", limit: int = 1) -> dict:
    """Convert a city name into coordinates using OpenWeatherMap geocoding."""
    return get_client().geocode_city(city, country_code, limit)


def get_current_weather(lat: float, lon: float, units: str = UNITS) -> dict:
    """Return the current conditions needed by the marketing agent."""
    return get_client().get_current_weather(lat, lon, units)


def get_forecast(lat: float, lon: float, units: str = UNITS) -> dict:
    """Return the five-day forecast as cleaned three-hour blocks."""
    return get_client().get_forecast(lat, lon, units)


def get_air_quality(lat: float, lon: float) -> dict:
    """Return current AQI and the pollutants most relevant to outdoor plans."""
    return get_client().get_air_quality(lat, lon)
