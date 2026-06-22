"""Raw OpenWeatherMap HTTP API layer, separated from MCP for clean architecture."""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

OPENWEATHERMAP_API_KEY = (os.getenv("OPENWEATHERMAP_API_KEY") or "").strip()

BASE_URL = "https://api.openweathermap.org"


def _api_key() -> str:
    """Return the validated API key without exposing its value."""
    if not OPENWEATHERMAP_API_KEY:
        raise ValueError(
            "OPENWEATHERMAP_API_KEY is missing from the project .env file"
        )

    placeholder_markers = ("your_", "your-", "placeholder", "replace_me", "changeme")
    if any(
        marker in OPENWEATHERMAP_API_KEY.casefold()
        for marker in placeholder_markers
    ):
        raise ValueError(
            "OPENWEATHERMAP_API_KEY still appears to contain placeholder text in "
            "the project .env file, or a stale environment variable is being used"
        )

    return OPENWEATHERMAP_API_KEY


def _get(path: str, params: dict) -> object:
    """Call an OpenWeatherMap endpoint and return its decoded JSON response."""
    params = {**params, "appid": _api_key()}
    try:
        response = requests.get(f"{BASE_URL}{path}", params=params, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        text = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"OpenWeatherMap API request failed ({status}): {text}") from exc
    except requests.RequestException as exc:
        # Request exception strings may contain the full URL, including appid.
        raise RuntimeError(
            "OpenWeatherMap API request failed due to a network error"
        ) from exc
    except ValueError as exc:
        raise RuntimeError("OpenWeatherMap returned an invalid JSON response") from exc


def geocode_city(city: str, country_code: str = "", limit: int = 1) -> dict:
    """Convert a city name into coordinates using OpenWeatherMap geocoding."""
    # Geocoding is needed because forecast/weather endpoints require coordinates
    query = f"{city},{country_code}" if country_code else city
    results = _get("/geo/1.0/direct", {"q": query, "limit": limit})
    if not results:
        raise ValueError(f"No geocoding results found for '{query}'")

    first = results[0]
    return {
        "lat": first["lat"],
        "lon": first["lon"],
        "name": first["name"],
        "state": first.get("state", ""),
        "country": first.get("country", ""),
    }


def get_current_weather(lat: float, lon: float, units: str = "imperial") -> dict:
    """Return the current conditions needed by the marketing agent."""
    data = _get("/data/2.5/weather", {"lat": lat, "lon": lon, "units": units})
    # We clean the raw API response to only include fields the LLM needs
    return {
        "city": data["name"],
        "temperature": data["main"]["temp"],
        "feels_like": data["main"]["feels_like"],
        "humidity": data["main"]["humidity"],
        "wind_speed": data["wind"]["speed"],
        "description": data["weather"][0]["description"],
        "cloud_cover": data["clouds"]["all"],
    }


def get_forecast(lat: float, lon: float, units: str = "imperial") -> dict:
    """Return the five-day forecast as cleaned three-hour blocks."""
    data = _get("/data/2.5/forecast", {"lat": lat, "lon": lon, "units": units})
    # 3-hour blocks let the LLM identify the best dayparts for campaigns
    entries = [
        {
            "datetime": item["dt_txt"],
            "temperature": item["main"]["temp"],
            "feels_like": item["main"]["feels_like"],
            "humidity": item["main"]["humidity"],
            "wind_speed": item["wind"]["speed"],
            "description": item["weather"][0]["description"],
            "rain_probability": item.get("pop", 0),
            "rain_volume_3h": item.get("rain", {}).get("3h", 0),
        }
        for item in data["list"]
    ]
    return {"city": data["city"]["name"], "forecast": entries}


def get_air_quality(lat: float, lon: float) -> dict:
    """Return current AQI and the pollutants most relevant to outdoor plans."""
    data = _get("/data/2.5/air_pollution", {"lat": lat, "lon": lon})
    reading = data["list"][0]
    components = reading["components"]
    # AQI: 1=Good, 2=Fair, 3=Moderate, 4=Poor, 5=Very Poor
    return {
        "aqi": reading["main"]["aqi"],
        "pm2_5": components["pm2_5"],
        "pm10": components["pm10"],
        "o3": components["o3"],
    }
