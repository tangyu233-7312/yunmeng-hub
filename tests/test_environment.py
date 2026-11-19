"""Regression tests for the test harness's production-data isolation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_inherited_application_configuration_cannot_escape_test_root(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HNE_")}
    env.update({
        "HNE_DATA_DIR": str(tmp_path / "real-data"),
        "HNE_SQLITE_PATH": str(tmp_path / "real.sqlite3"),
        "HNE_CHROMA_PERSIST_DIR": str(tmp_path / "real-vectors"),
        "HNE_LOG_DIR": str(tmp_path / "real-logs"),
        "HNE_DEFAULT_LLM_BASE_URL": "https://provider.invalid",
        "HNE_DEFAULT_LLM_API_KEY": "fake-do-not-use",
        "HNE_EMBEDDING_BACKEND": "api",
    })
    probe = '''
import json, runpy
runpy.run_path('tests/conftest.py')
from app.core.config import Settings, get_settings
s = get_settings()
print(json.dumps({
    'paths': [str(s.sqlite_file), str(s.chroma_dir), str(s.log_dir)],
    'dotenv': Settings.model_config['env_file'],
    'llm_url': s.DEFAULT_LLM_BASE_URL,
    'llm_key': s.DEFAULT_LLM_API_KEY,
    'embedding': s.EMBEDDING_BACKEND,
}))
'''
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True,
        text=True, encoding="utf-8", check=True, timeout=30,
    )
    report = json.loads(result.stdout)
    assert all(Path(p).is_relative_to(ROOT / ".pytest-data") for p in report["paths"])
    assert report["dotenv"] is None
    assert not report["llm_url"] and not report["llm_key"]
    assert report["embedding"] == "onnx_default"
    assert not list(tmp_path.iterdir())


def test_mysql_without_dedicated_test_credentials_is_rejected() -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HNE_")}
    env.update({"HNE_DB_BACKEND": "mysql", "HNE_MYSQL_DB": "production", "HNE_MYSQL_USER": "app"})
    result = subprocess.run(
        [sys.executable, "-c", "import runpy; runpy.run_path('tests/conftest.py')"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode != 0
    assert "HNE_TEST_MYSQL_DB" in result.stderr
