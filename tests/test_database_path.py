"""The database file and .env must not depend on the process working directory.

Both used to be resolved against the current working directory:
DATABASE_URL defaults to "sqlite+aiosqlite:///./lucky_num.db" and the settings
read "./.env". Started from anywhere but the project directory — a service unit
with no WorkingDirectory, a scheduled task, an IDE run configuration — the app
opened a *different* SQLite file, and the startup schema gate then bootstrapped
it as a new, empty production database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, Settings

PROJECT_DB = PROJECT_ROOT / "lucky_num.db"


def _resolved_file(url: str) -> Path | None:
    database = make_url(url).database
    return Path(database).resolve() if database else None


def test_project_root_is_the_directory_holding_the_app_package() -> None:
    assert (PROJECT_ROOT / "app" / "config.py").is_file()
    assert PROJECT_ROOT.is_absolute()


def test_relative_sqlite_path_is_anchored_to_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: from another working directory this resolved to
    <cwd>/lucky_num.db, and the schema gate created an empty database there.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    url = Settings().database_url

    assert _resolved_file(url) == PROJECT_DB.resolve()
    assert Path(make_url(url).database or "").is_absolute()
    assert not (tmp_path / "lucky_num.db").exists()


def test_relative_sqlite_path_from_the_project_root_is_unchanged_in_meaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(PROJECT_ROOT)
    assert _resolved_file(Settings().database_url) == PROJECT_DB.resolve()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",  # the test suite's own database
        "postgresql+asyncpg://user:pw@localhost:5432/lucky_num",  # production target
    ],
)
def test_non_file_and_non_sqlite_urls_are_left_alone(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    assert Settings().database_url == url


def test_absolute_sqlite_path_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "elsewhere.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{explicit.as_posix()}")
    assert _resolved_file(Settings().database_url) == explicit.resolve()


def test_env_file_is_read_from_the_project_regardless_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural check only — no secret values are read or printed."""
    configured = Settings.model_config["env_file"]
    assert Path(str(configured)) == PROJECT_ROOT / ".env"
    assert Path(str(configured)).is_absolute()

    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".env").exists()
    assert os.getcwd() != str(PROJECT_ROOT)
    Settings()  # must not raise, and must not depend on a .env in the cwd
