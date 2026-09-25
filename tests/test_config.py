from __future__ import annotations

from pathlib import Path

import pytest

from wayback_verify import Config, Credentials, load_ia_credentials
from wayback_verify.config import ANON_SPN_LIMIT, AUTH_SPN_LIMIT


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("IA_CONFIG_FILE", "IA_ACCESS_KEY_ID", "IA_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


def write_ini(path: Path, access: str = "AK", secret: str = "SK") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[s3]\naccess = {access}\nsecret = {secret}\n", encoding="utf-8")
    return path


def test_reads_ia_ini_from_xdg(tmp_path: Path) -> None:
    write_ini(tmp_path / "xdg" / "internetarchive" / "ia.ini")
    assert load_ia_credentials() == Credentials("AK", "SK")


def test_ia_config_file_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ini(tmp_path / "xdg" / "internetarchive" / "ia.ini")
    other = write_ini(tmp_path / "other.ini", "A2", "S2")
    monkeypatch.setenv("IA_CONFIG_FILE", str(other))
    assert load_ia_credentials() == Credentials("A2", "S2")


def test_legacy_home_file(tmp_path: Path) -> None:
    write_ini(tmp_path / "home" / ".ia")
    assert load_ia_credentials() == Credentials("AK", "SK")


def test_env_keys_override_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ini(tmp_path / "xdg" / "internetarchive" / "ia.ini")
    monkeypatch.setenv("IA_ACCESS_KEY_ID", "EA")
    monkeypatch.setenv("IA_SECRET_ACCESS_KEY", "ES")
    assert load_ia_credentials() == Credentials("EA", "ES")


def test_env_keys_must_come_in_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IA_ACCESS_KEY_ID", "EA")
    with pytest.raises(ValueError):
        load_ia_credentials()


def test_no_credentials() -> None:
    assert load_ia_credentials() is None
    assert Config.from_environment().credentials is None


def test_explicit_file_and_spn_limits(tmp_path: Path) -> None:
    ini = write_ini(tmp_path / "custom.ini")
    config = Config.from_environment(ini)
    assert config.credentials == Credentials("AK", "SK")
    assert config.effective_spn_limit == AUTH_SPN_LIMIT
    assert Config().effective_spn_limit == ANON_SPN_LIMIT
    assert "SK" not in repr(config.credentials)
    assert config.credentials is not None
    assert config.credentials.authorization == "LOW AK:SK"
