"""Report safe metadata about ClearCast's project-root .env file.

This diagnostic never prints API key values or complete environment variables.
Run it manually from any working directory with: python safe_env_check.py
"""

import re
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
KEY_NAME = "OPENWEATHERMAP_API_KEY"
KEY_PATTERN = re.compile(rf"^\s*(?:export\s+)?{KEY_NAME}\s*=")
PLACEHOLDER_MARKERS = ("your_", "your-", "placeholder", "replace_me", "changeme")


def main() -> None:
    """Print only non-secret configuration metadata."""
    env_exists = ENV_PATH.is_file()
    print(f"Project .env exists: {env_exists}")
    if not env_exists:
        print(f"{KEY_NAME} exists: False")
        return

    lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    matching_lines = [line for line in lines if KEY_PATTERN.match(line)]
    values = dotenv_values(dotenv_path=ENV_PATH)
    raw_value = values.get(KEY_NAME)
    value = raw_value if isinstance(raw_value, str) else ""
    stripped_value = value.strip()
    raw_assignment = matching_lines[-1].split("=", 1)[1] if matching_lines else ""

    print(f"{KEY_NAME} exists: {bool(stripped_value)}")
    print(f"{KEY_NAME} length: {len(stripped_value)}")
    print(
        f"{KEY_NAME} contains placeholder text: "
        f"{any(marker in stripped_value.casefold() for marker in PLACEHOLDER_MARKERS)}"
    )
    print(
        f"{KEY_NAME} has leading/trailing spaces: "
        f"{raw_assignment != raw_assignment.strip()}"
    )
    print(f"{KEY_NAME} duplicate lines: {len(matching_lines) > 1}")
    print(f"{KEY_NAME} line count: {len(matching_lines)}")


if __name__ == "__main__":
    main()