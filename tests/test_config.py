import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import ConfigurationError, Settings
from app.main import create_app


def test_missing_url_fails_startup(monkeypatch):
    monkeypatch.delenv("SNG_CONTROL_BASE_URL", raising=False)
    with pytest.raises(ConfigurationError, match="SNG_CONTROL_BASE_URL"):
        with TestClient(create_app()):
            pass


@pytest.mark.parametrize("timeout", ["bad", "0", "-1", "nan", "inf", "-inf", ""])
def test_invalid_timeout(monkeypatch, timeout):
    monkeypatch.setenv("SNG_CONTROL_BASE_URL", "https://control.test")
    monkeypatch.setenv("SNG_CONTROL_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ConfigurationError):
        Settings.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "ftp://control.test",
        "https://user:secret@control.test",
        "https://control.test/api",
        "https://control.test?key=secret",
        "https://control.test#fragment",
        "https://control.test:bad",
        "http://control.test",
        "https://control.test/../",
        "https://control.test\\evil",
        "https://control.test\n",
    ],
)
def test_invalid_origin(url):
    with pytest.raises(ValidationError):
        Settings(sng_control_base_url=url)


@pytest.mark.parametrize(
    "url", ["https://control.test/", "http://localhost:8000", "http://[::1]:8000"]
)
def test_valid_config(monkeypatch, url):
    monkeypatch.setenv("SNG_CONTROL_BASE_URL", url)
    monkeypatch.setenv("SNG_CONTROL_TIMEOUT_SECONDS", "2.5")
    settings = Settings.from_env()
    assert settings.sng_control_base_url == url.rstrip("/")
    assert settings.sng_control_timeout_seconds == 2.5


def test_startup_error_has_no_config_value_or_context(monkeypatch):
    monkeypatch.setenv("SNG_CONTROL_BASE_URL", "https://private:credential@control.test")
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env()
    assert "credential" not in str(error.value)
    assert error.value.__context__ is None
