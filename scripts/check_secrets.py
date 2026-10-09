"""Fail if credential-shaped strings or .env files are present in the repository.

Scans tracked files plus untracked, non-ignored files inside ClearCast's own
directories. Reports file, line, and rule only, never the matched value.
Usage: python scripts/check_secrets.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PREFIXES = (
    ".github/",
    "agent/",
    "api/",
    "contracts/",
    "deploy/",
    "docs/",
    "evaluation/",
    "frontend/",
    "gateway/",
    "mcp_server/",
    "scripts/",
    "tests/",
    "app.py",
    "Dockerfile",
    "docker-compose.yml",
    "README.md",
    ".env",
    "requirements",
    "pyproject.toml",
    "test_mcp.py",
    "safe_env_check.py",
    "progress.md",
    ".gitignore",
    ".dockerignore",
)
RULES = {
    "openai_key": re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    "huggingface_token": re.compile(r"hf_[A-Za-z0-9]{30,}"),
    "langsmith_key": re.compile(r"lsv2_(?:pt|sk)_[A-Za-z0-9]{20,}"),
    "github_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
# OpenWeatherMap keys are 32 lowercase alphanumerics; flag them on lines that mention the provider.
CONTEXT_RULES = {
    "openweathermap_key": (re.compile(r"openweather|\bowm|appid", re.I), re.compile(r"\b[0-9a-z]{32}\b")),
}
# Obviously synthetic values used in tests and fixtures.
ALLOW = re.compile(r"fake|test|fixture|offline|example|dummy|redacted|NEVER|SECRET|not-a-real|placeholder", re.I)
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".lock")


def candidate_files() -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]

    tracked = set(git("ls-files", "--cached"))
    untracked = {
        path for path in git("ls-files", "--others", "--exclude-standard") if path.startswith(PROJECT_PREFIXES)
    }
    return sorted(tracked | untracked)


def main() -> int:
    problems: list[str] = []
    for path in candidate_files():
        name = Path(path).name
        if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
            problems.append(f"{path}: environment file must not be committed")
            continue
        if path.endswith(SKIP_SUFFIXES) or path.endswith("package-lock.json"):
            continue
        file = ROOT / path
        if not file.is_file():
            continue
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, 1):
            for rule, pattern in RULES.items():
                for match in pattern.finditer(line):
                    if not ALLOW.search(match.group(0)):
                        problems.append(f"{path}:{number}: possible {rule}")
            for rule, (context, pattern) in CONTEXT_RULES.items():
                if context.search(line) and any(not ALLOW.search(m.group(0)) for m in pattern.finditer(line)):
                    problems.append(f"{path}:{number}: possible {rule}")
    if problems:
        print("Secret scan failed:")
        print("\n".join(f"  {problem}" for problem in problems))
        return 1
    print("Secret scan passed: no credential-shaped strings found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
