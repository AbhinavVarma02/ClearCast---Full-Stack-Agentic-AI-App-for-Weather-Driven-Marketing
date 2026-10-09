"""OpenWeatherMap client: normalisation, retries, rate limits, cache, and secret safety."""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mcp_server.fixtures import FixtureTransport
from mcp_server.weather_api import OpenWeatherMapClient, WeatherProviderError
from tests.helpers import FAKE_OPENWEATHER_KEY

NOW = datetime(2030, 4, 4, 1, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_client(transport, *, clock=None, sleeps=None, ttl: float = 600, key: str = FAKE_OPENWEATHER_KEY):
    return OpenWeatherMapClient(
        api_key=key,
        transport=transport,
        clock=clock or Clock(),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
        cache_ttl_seconds=ttl,
    )


def scripted_transport(responses):
    """Return responses (or raise exceptions) in order; record requested URLs."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    transport = httpx.MockTransport(handler)
    transport.calls = calls
    return transport


def test_forecast_is_normalised_with_ids_units_and_local_time():
    client = make_client(FixtureTransport("baseline_mild", clock=Clock()))
    result = client.get_forecast(39.29, -76.61)
    first = result["forecast"][0]
    assert result["observation_count"] == 40
    assert result["timezone_offset_seconds"] == -4 * 3600
    assert result["units"]["temperature"] == "°F"
    assert result["units"]["wind_speed"] == "mph"
    assert "fraction" in result["units"]["rain_probability"]
    assert first["observation_id"] == "fc-20300404T0300Z"
    assert first["datetime_utc"] == "2030-04-04T03:00:00Z"
    assert first["datetime_local"] == "2030-04-03T23:00:00-04:00"
    # The original UTC provider text is preserved for compatibility.
    assert first["datetime"] == "2030-04-04 03:00:00"
    assert 0 <= first["rain_probability"] <= 1
    assert result["fetched_at_utc"] == "2030-04-04T01:30:00Z"
    assert result["source"] == "openweathermap"


def test_missing_fields_are_identified_not_defaulted():
    client = make_client(FixtureTransport("missing_fields", clock=Clock()))
    entries = client.get_forecast(1, 2)["forecast"]
    incomplete = entries[1]
    assert incomplete["wind_speed"] is None
    assert incomplete["rain_probability"] is None
    assert set(incomplete["missing_fields"]) == {"wind_speed", "rain_probability"}
    assert "missing_fields" not in entries[0]


def test_retries_rate_limit_then_succeeds_and_honours_retry_after():
    ok = httpx.Response(200, json=[{"lat": 1.0, "lon": 2.0, "name": "X"}])
    transport = scripted_transport([httpx.Response(429, headers={"Retry-After": "2"}), ok])
    sleeps: list[float] = []
    result = make_client(transport, sleeps=sleeps).geocode_city("X")
    assert result["name"] == "X"
    assert len(transport.calls) == 2
    assert sleeps == [2.0]


def test_rate_limit_exhaustion_reports_category_after_bounded_attempts():
    transport = scripted_transport([httpx.Response(429, headers={"Retry-After": "60"})])
    sleeps: list[float] = []
    with pytest.raises(WeatherProviderError) as caught:
        make_client(transport, sleeps=sleeps).get_forecast(1, 2)
    assert caught.value.category == "provider_rate_limited"
    assert "after 3 attempts" in str(caught.value)
    assert len(transport.calls) == 3
    # Retry-After is capped so a provider cannot stall a request indefinitely.
    assert sleeps == [5.0, 5.0]


def test_exponential_backoff_on_server_errors_and_timeouts():
    transport = scripted_transport(
        [httpx.ReadTimeout("slow"), httpx.Response(503), httpx.Response(200, json={"list": [], "city": {}})]
    )
    sleeps: list[float] = []
    result = make_client(transport, sleeps=sleeps).get_forecast(1, 2)
    assert result["observation_count"] == 0
    assert len(sleeps) == 2
    assert 0.25 <= sleeps[0] <= 0.5 and 0.5 <= sleeps[1] <= 1.0


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (httpx.Response(401), "provider_auth_error"),
        (httpx.Response(404), "provider_not_found"),
        (httpx.Response(400), "provider_bad_request"),
    ],
)
def test_non_retryable_errors_fail_fast(response, category):
    transport = scripted_transport([response])
    with pytest.raises(WeatherProviderError) as caught:
        make_client(transport).get_forecast(1, 2)
    assert caught.value.category == category
    assert len(transport.calls) == 1


def test_invalid_json_and_missing_fields_are_bad_responses():
    with pytest.raises(WeatherProviderError) as invalid:
        make_client(FixtureTransport("baseline_mild:owm_invalid_json", clock=Clock())).get_forecast(1, 2)
    assert invalid.value.category == "provider_bad_response"
    with pytest.raises(WeatherProviderError) as missing:
        make_client(FixtureTransport("baseline_mild:forecast_missing_list", clock=Clock())).get_forecast(1, 2)
    assert missing.value.category == "provider_bad_response"
    assert "'list'" in str(missing.value)


def test_geocode_without_results_is_location_not_found():
    with pytest.raises(WeatherProviderError) as caught:
        make_client(FixtureTransport("baseline_mild:geocode_empty", clock=Clock())).geocode_city("Nowhere")
    assert caught.value.category == "location_not_found"


def test_missing_or_placeholder_key_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("OPENWEATHERMAP_API_KEY", raising=False)
    client = OpenWeatherMapClient(transport=FixtureTransport("baseline_mild"))
    with pytest.raises(WeatherProviderError) as missing:
        client.get_forecast(1, 2)
    assert missing.value.category == "config_missing"
    placeholder = OpenWeatherMapClient(
        api_key="your_openweathermap_key_here", transport=FixtureTransport("baseline_mild")
    )
    with pytest.raises(WeatherProviderError) as caught:
        placeholder.get_forecast(1, 2)
    assert "placeholder" in str(caught.value)


def test_api_key_never_appears_in_errors_tracebacks_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    transport = scripted_transport([httpx.ConnectError(f"failed GET https://x/?appid={FAKE_OPENWEATHER_KEY}")])
    with pytest.raises(WeatherProviderError) as caught:
        make_client(transport).get_forecast(1, 2)
    error = caught.value
    rendered = "".join(traceback.format_exception(error))
    assert FAKE_OPENWEATHER_KEY not in str(error)
    assert FAKE_OPENWEATHER_KEY not in repr(error)
    assert FAKE_OPENWEATHER_KEY not in rendered
    assert error.__cause__ is None and error.__suppress_context__
    # httpx would log full request URLs (with appid) at INFO; those loggers are capped.
    make_client(FixtureTransport("baseline_mild", clock=Clock())).get_forecast(1, 2)
    assert FAKE_OPENWEATHER_KEY not in caplog.text


def test_cache_reuses_responses_without_hiding_their_age():
    clock = Clock()
    transport = FixtureTransport("baseline_mild", clock=clock)
    client = make_client(transport, clock=clock)
    first = client.get_forecast(39.29, -76.61)
    clock.now = NOW + timedelta(minutes=4)
    second = client.get_forecast(39.29, -76.61)
    assert transport.calls.count("/data/2.5/forecast") == 1
    assert second["cache"] == {"hit": True, "age_seconds": 240}
    # Cached data keeps its original fetch time; it is never presented as newer.
    assert second["fetched_at_utc"] == first["fetched_at_utc"] == "2030-04-04T01:30:00Z"
    # Different coordinates are a different cache key.
    client.get_forecast(40.0, -75.0)
    assert transport.calls.count("/data/2.5/forecast") == 2
    # Entries expire after the TTL.
    clock.now = NOW + timedelta(minutes=11)
    third = client.get_forecast(39.29, -76.61)
    assert third["cache"]["hit"] is False
    assert transport.calls.count("/data/2.5/forecast") == 3


def test_cache_can_be_disabled():
    transport = FixtureTransport("baseline_mild", clock=Clock())
    client = make_client(transport, ttl=0)
    client.get_forecast(1, 2)
    client.get_forecast(1, 2)
    assert transport.calls.count("/data/2.5/forecast") == 2


def test_air_quality_forecast_is_aggregated_to_three_hour_blocks():
    client = make_client(FixtureTransport("poor_air", clock=Clock()))
    result = client.get_air_quality(1, 2)
    assert result["aqi"] == 4
    assert result["scale"].startswith("OpenWeatherMap AQI")
    assert result["forecast_available"] is True
    starts = [datetime.fromisoformat(b["datetime_utc"].replace("Z", "+00:00")) for b in result["forecast"]]
    assert all(start.hour % 3 == 0 for start in starts)
    assert result["forecast"][1]["observation_id"] == "aqf-20300404T0300Z"
    assert all(block["aqi_max"] == 4 for block in result["forecast"])


def test_air_quality_forecast_failure_degrades_gracefully():
    result = make_client(FixtureTransport("baseline_mild:aq_forecast_down", clock=Clock())).get_air_quality(1, 2)
    assert result["aqi"] == 2
    assert result["forecast_available"] is False
    assert result["forecast_error"] == "provider_unavailable"
