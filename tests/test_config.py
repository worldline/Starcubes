"""core/config.py — env-first-Auflösung und Cache-Invalidierung."""
from __future__ import annotations

import pytest

import core.config as config


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    """Jeder Test startet ohne DB und ohne Cache."""
    monkeypatch.delenv("STARCUBES_POSTGRES_DSN", raising=False)
    config.invalidate()
    yield
    config.invalidate()


class TestGetWithoutDb:
    def test_env_gewinnt(self, monkeypatch):
        monkeypatch.setenv("STARCUBES_SMTP_HOST", "mail.example.org")
        assert config.get("STARCUBES_SMTP_HOST") == "mail.example.org"

    def test_ohne_env_none(self):
        assert config.get("STARCUBES_SMTP_HOST") is None

    def test_leerer_env_wert_gilt_als_unset(self, monkeypatch):
        monkeypatch.setenv("STARCUBES_SMTP_HOST", "")
        assert config.get("STARCUBES_SMTP_HOST") is None


class TestGetWithSnapshot:
    def test_db_wert_wird_gelesen(self, monkeypatch):
        monkeypatch.setattr(
            config, "_snapshot",
            lambda: {"STARCUBES_SMTP_HOST": "aus-der-db"})
        assert config.get("STARCUBES_SMTP_HOST") == "aus-der-db"

    def test_env_gewinnt_gegen_db(self, monkeypatch):
        monkeypatch.setenv("STARCUBES_SMTP_HOST", "aus-env")
        monkeypatch.setattr(
            config, "_snapshot",
            lambda: {"STARCUBES_SMTP_HOST": "aus-der-db"})
        assert config.get("STARCUBES_SMTP_HOST") == "aus-env"


class TestEffective:
    def test_abgeleitete_email_und_xmpp_schalter(self, monkeypatch):
        monkeypatch.setattr(config, "_snapshot", lambda: {
            "STARCUBES_SMTP_HOST": "mail",
            "STARCUBES_SMTP_FROM": "a@example.org",
            "STARCUBES_XMPP_JID": "bot@jabber",
            "STARCUBES_XMPP_PASSWORD": "",
        })
        out = config.effective()
        assert out["alarm_email_enabled"] is True
        # Passwort fehlt → XMPP aus.
        assert out["alarm_xmpp_enabled"] is False

    def test_oidc_keys(self, monkeypatch):
        monkeypatch.setattr(config, "_snapshot", lambda: {
            "STARCUBES_OIDC_ISSUER": "https://sso",
            "STARCUBES_OIDC_CLIENT_ID": "starcubes",
            "STARCUBES_OIDC_CLIENT_SECRET": "geheim",
            "STARCUBES_OIDC_DISPLAY_NAME": "SSO",
        })
        assert config.effective_oidc() == {
            "STARCUBES_OIDC_ISSUER": "https://sso",
            "STARCUBES_OIDC_CLIENT_ID": "starcubes",
            "STARCUBES_OIDC_CLIENT_SECRET": "geheim",
            "STARCUBES_OIDC_DISPLAY_NAME": "SSO",
        }


class TestState:
    def test_ohne_dsn_nur_env(self, monkeypatch):
        monkeypatch.setenv("STARCUBES_SMTP_HOST", "mail")
        env, db = config.state()
        assert env["STARCUBES_SMTP_HOST"] == "mail"
        assert db == {}

    def test_invalidate_verwirft_snapshot(self, monkeypatch):
        config._cache = {"STARCUBES_SMTP_HOST": "alt"}
        config._cache_at = 1.0
        config.invalidate()
        assert config._cache is None
        assert config._cache_at == 0.0
