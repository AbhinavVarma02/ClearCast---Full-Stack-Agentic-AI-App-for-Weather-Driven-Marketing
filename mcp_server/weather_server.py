"""Expose the raw weather API layer as documented MCP tools."""

from mcp.server.fastmcp import FastMCP

from mcp_server import weather_api

# MCP Server wrapping OpenWeatherMap API. Pattern follows accounts_server.py from Week 6 MCP course.
mcp = FastMCP("weather_server")


@mcp.tool()
async def geocode_city(city: str, country_code: str = "") -> dict:
    """Convert a city name to geographic coordinates (latitude and longitude).
    Use this tool first to get coordinates before calling weather or forecast tools.
    Args:
        city: City name (e.g., 'Baltimore', 'New York', 'London')
        country_code: Optional ISO 3166 country code (e.g., 'US', 'GB')
    """
    # Call the raw API wrapper
    return weather_api.geocode_city(city, country_code)


@mcp.tool()
async def get_current_weather(lat: float, lon: float) -> dict:
    """Get current weather conditions for a location using coordinates.
    Call geocode_city first to convert a city name to coordinates.
    Args:
        lat: Latitude of the location
        lon: Longitude of the location
    """
    return weather_api.get_current_weather(lat, lon)


@mcp.tool()
async def get_forecast(lat: float, lon: float) -> dict:
    """Get a 5-day weather forecast in 3-hour intervals for a location.
    Returns temperature, rain probability, and conditions for each 3-hour block.
    This data is useful for identifying the best dayparts for marketing campaigns.
    Args:
        lat: Latitude of the location
        lon: Longitude of the location
    """
    return weather_api.get_forecast(lat, lon)


@mcp.tool()
async def get_air_quality(lat: float, lon: float) -> dict:
    """Get air quality index and pollutant levels for a location.
    Useful for determining if outdoor activities and promotions are advisable.
    AQI scale: 1=Good, 2=Fair, 3=Moderate, 4=Poor, 5=Very Poor.
    Args:
        lat: Latitude of the location
        lon: Longitude of the location
    """
    return weather_api.get_air_quality(lat, lon)


# MCP servers use stdio transport by default for local operation.
# The agent backend spawns this as a subprocess and communicates over stdin/stdout.
if __name__ == "__main__":
    mcp.run(transport="stdio")
