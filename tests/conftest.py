"""Isolated pytest configuration; no application .env or real API calls by default.

SQLite, Chroma, logs and generated secrets stay under .pytest-data. MySQL
requires explicit HNE_TEST_MYSQL_* credentials for a database ending in _test.
Use --live to opt into the four real LLM requests (which may incur API charges).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_DIR = REPO_ROOT / ".pytest-data"
_INHERITED = {
    key: value for key, value in os.environ.items() if key.upper().startswith("HNE_")
}
# Tests must not inherit production paths, keys or providers from a shell/IDE.
for key in _INHERITED:
    os.environ.pop(key)
os.environ.update(
    {
        "HNE_DATA_DIR": str(TEST_DATA_DIR),
        "HNE_DB_BACKEND": _INHERITED.get("HNE_DB_BACKEND", "sqlite"),
        "HNE_SQLITE_PATH": str(TEST_DATA_DIR / "data" / "pytest.sqlite3"),
        "HNE_CHROMA_PERSIST_DIR": str(TEST_DATA_DIR / "data" / "chroma"),
        "HNE_LOG_DIR": str(TEST_DATA_DIR / "logs"),
        "HNE_LOG_LEVEL": "WARNING",
        "HNE_APP_ENV": "testing",
        "HNE_EMBEDDING_BACKEND": "onnx_default",
        # Real bcrypt still runs; the low cost applies only to disposable test accounts.
        "HNE_BCRYPT_ROUNDS": _INHERITED.get("HNE_TEST_BCRYPT_ROUNDS", "4"),
        "HNE_SECRET_KEY": "test-only-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz",
        "HNE_API_KEY_ENCRYPTION_KEY": "KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio=",
    }
)
if "HNE_REAL_PRESET" in _INHERITED:
    os.environ["HNE_REAL_PRESET"] = _INHERITED["HNE_REAL_PRESET"]

if os.environ["HNE_DB_BACKEND"] == "mysql":
    db_name = _INHERITED.get("HNE_TEST_MYSQL_DB", "")
    if not db_name.endswith("_test") or not _INHERITED.get("HNE_TEST_MYSQL_USER"):
        raise pytest.UsageError(
            "MySQL tests require HNE_TEST_MYSQL_DB (ending in _test) and "
            "HNE_TEST_MYSQL_USER; application credentials are never reused."
        )
    for suffix in ("HOST", "PORT", "USER", "PASSWORD", "DB"):
        value = _INHERITED.get(f"HNE_TEST_MYSQL_{suffix}", "")
        if value:
            os.environ[f"HNE_MYSQL_{suffix}"] = value
    os.environ.setdefault("HNE_MYSQL_HOST", "127.0.0.1")

# Set this before any application module can create engines or cached settings.
from app.core.config import Settings, get_settings  # noqa: E402

Settings.model_config["env_file"] = None
get_settings.cache_clear()


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        help="Allow real LLM API tests (may incur charges)",
    )


def pytest_configure(config) -> None:
    if config.getoption("--live"):
        from dotenv import dotenv_values

        values = {**dotenv_values(REPO_ROOT / ".env"), **_INHERITED}
        for suffix in ("BASE_URL", "API_KEY", "MODEL"):
            key = f"HNE_DEFAULT_LLM_{suffix}"
            os.environ[key] = values.get(key) or ""
        get_settings.cache_clear()


def pytest_collection_modifyitems(config, items) -> None:
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(
            reason="Real LLM API tests require explicit --live"
        )
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)


def pytest_report_header(config) -> str:
    return (
        f"HNE tests: DB_BACKEND={os.environ['HNE_DB_BACKEND']} | "
        f"DATA_DIR={TEST_DATA_DIR} | live={config.getoption('--live')}"
    )


def pytest_terminal_summary(terminalreporter) -> None:
    passed = terminalreporter.stats.get("passed", [])
    counts = {
        layer: sum(layer in report.keywords for report in passed)
        for layer in ("unit", "integration", "acceptance", "live")
    }
    terminalreporter.write_line(
        "Passed by layer: "
        + ", ".join(f"{key}={value}" for key, value in counts.items())
    )


def _verify_isolation() -> None:
    settings = get_settings()
    paths = [settings.chroma_dir, settings.log_dir]
    if settings.is_sqlite:
        paths.append(settings.sqlite_file)
    for path in paths:
        if TEST_DATA_DIR.resolve() not in path.resolve().parents:
            raise pytest.UsageError(f"Test data escaped .pytest-data: {path}")


_verify_isolation()
