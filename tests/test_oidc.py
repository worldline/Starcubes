"""starcubes/oidc.py — Claim-Matrix des Keycloak-Backends (ohne DB)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from starcubes.oidc import KeycloakOIDCBackend


@pytest.fixture(autouse=True)
def _oidc_settings(monkeypatch):
    """Das Backend liest seine Endpunkte im __init__ aus den Settings."""
    from django.conf import settings
    monkeypatch.setattr(settings, "OIDC_OP_TOKEN_ENDPOINT",
                        "https://sso/token", raising=False)
    monkeypatch.setattr(settings, "OIDC_OP_USER_ENDPOINT",
                        "https://sso/userinfo", raising=False)
    monkeypatch.setattr(settings, "OIDC_RP_CLIENT_ID", "starcubes",
                        raising=False)
    monkeypatch.setattr(settings, "OIDC_RP_CLIENT_SECRET", "geheim",
                        raising=False)


class User:
    def __init__(self, username, email="", is_superuser=False, is_staff=False):
        self.username = username
        self.email = email
        self.is_superuser = is_superuser
        self.is_staff = is_staff
        self.saved = None

    def save(self, update_fields=None):
        self.saved = list(update_fields or [])


class FakeQS(list):
    def filter(self, **kw):
        out = list(self)
        for key, value in kw.items():
            if key == "email__iexact":
                out = [u for u in out if (u.email or "").lower() == value.lower()]
            elif key == "username__iexact":
                out = [u for u in out
                       if (u.username or "").lower() == value.lower()]
            else:                      # pragma: no cover - unerwarteter Filter
                raise AssertionError(f"unexpected filter {key}")
        return FakeQS(out)


class FakeManager:
    def __init__(self, users):
        self.users = list(users)

    def none(self):
        return FakeQS()

    def filter(self, **kw):
        return FakeQS(self.users).filter(**kw)


def _backend(users=()):
    b = KeycloakOIDCBackend()
    b.UserModel = SimpleNamespace(objects=FakeManager(users))
    return b


class TestUsername:
    def test_preferred_username_gewinnt(self):
        b = _backend()
        assert b.get_username({"preferred_username": "anna",
                               "email": "a@example.org"}) == "anna"

    def test_fallback_der_library(self):
        b = _backend()
        got = b.get_username({"email": "a@example.org", "sub": "1"})
        assert got and got != "anna"


class TestVerifyClaims:
    def test_ohne_email_trotzdem_true(self, monkeypatch):
        import starcubes.oidc as oidc_mod
        warnings = []
        monkeypatch.setattr(oidc_mod.LOGGER, "warning",
                            lambda *a, **k: warnings.append(a))
        b = _backend()
        assert b.verify_claims({"preferred_username": "anna"}) is True
        assert any("no email claim" in str(a) for a in warnings)

    def test_mit_email_true(self):
        assert _backend().verify_claims({"email": "a@example.org"}) is True


class TestFilterUsersByClaims:
    def test_ein_email_treffer_gewinnt(self):
        user = User("anna", "a@example.org")
        b = _backend([user, User("bob", "b@example.org")])
        got = b.filter_users_by_claims(
            {"email": "A@example.org", "preferred_username": "anna"})
        assert list(got) == [user]

    def test_ohne_email_wird_per_username_gematcht(self):
        user = User("anna")
        b = _backend([user])
        got = b.filter_users_by_claims({"preferred_username": "anna"})
        assert list(got) == [user]

    def test_email_ohne_treffer_kein_username_takeover(self):
        # Ein lokaler Account heißt wie die SSO-Identität, hat aber eine
        # andere E-Mail — er darf NICHT übernommen werden.
        local = User("anna", "lokal@example.org")
        b = _backend([local])
        got = b.filter_users_by_claims(
            {"email": "sso@example.org", "preferred_username": "anna"})
        assert list(got) == []

    def test_mehrere_konten_mit_einer_email_werden_per_username_eingeengt(self):
        a = User("anna", "gleich@example.org")
        b = User("admin", "gleich@example.org")
        backend = _backend([a, b])
        got = backend.filter_users_by_claims(
            {"email": "gleich@example.org", "preferred_username": "anna"})
        assert list(got) == [a]

    def test_mehrdeutig_bleibt_mehrdeutig(self):
        a = User("anna", "gleich@example.org")
        b = User("admin", "gleich@example.org")
        backend = _backend([a, b])
        got = backend.filter_users_by_claims(
            {"email": "gleich@example.org", "preferred_username": "niemand"})
        assert len(got) == 2

    def test_keine_email_kein_username_treffer(self):
        assert list(_backend([User("bob")]).filter_users_by_claims(
            {"preferred_username": "anna"})) == []


class TestCreateUser:
    def test_superuser_und_staff_werden_entfernt(self, monkeypatch):
        user = User("anna", is_superuser=True, is_staff=True)

        def fake_create(self, claims):
            return user

        monkeypatch.setattr(OIDCAuthenticationBackend, "create_user",
                            fake_create)
        got = _backend().create_user({"preferred_username": "anna"})
        assert got is user
        assert user.is_superuser is False
        assert user.is_staff is False
        assert user.saved == ["is_superuser", "is_staff"]

    def test_normales_konto_wird_nicht_gespeichert(self, monkeypatch):
        user = User("anna")

        def fake_create(self, claims):
            return user

        monkeypatch.setattr(OIDCAuthenticationBackend, "create_user",
                            fake_create)
        _backend().create_user({"preferred_username": "anna"})
        assert user.saved is None
