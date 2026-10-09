"""MCP weather server package for ClearCast."""

import warnings

# mcp's FastMCP settings model triggers a harmless pydantic-settings warning on import.
warnings.filterwarnings("ignore", message="Field 'lifespan' has an incomplete definition")
