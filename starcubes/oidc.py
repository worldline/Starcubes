"""Custom mozilla-django-oidc backend for Keycloak SSO.

Three departures from the library's defaults, all deliberate:

1. Django username is taken from Keycloak's `preferred_username` claim
   instead of the library's default (a base64/sha1 hash of the email
   address) — needed because this app already has password-based
   usernames, and username collisions/mapping should be an explicit,
   documented choice rather than an opaque hash nobody can look up.

2. `create_user()` force-clears is_superuser/is_staff — belt-and-suspenders
   on top of Django's own model defaults, so SSO-provisioned accounts can
   never come back with elevated access even if a future Django release
   changes create_user()'s defaults. Admin stays a manually-granted local
   flag for both password and SSO accounts; the `@_admin_required_json` /
   `@_admin_required_page` gates in `sources/views.py` are the checks.

3. `filter_users_by_claims()` keeps the library's email-based lookup as
   the PRIMARY key — an admin whose local password account shares an
   email address with their Keycloak identity still logs in as that same
   account on first SSO login rather than getting a second one. That is
   intentional merge behaviour, not a bug. But email alone is not enough,
   because point 1 decoupled the username from the matching key, which
   left two dead ends the stock lookup cannot escape:

     - Two accounts, one email → `len(users) > 1` → the library raises
       SuspiciousOperation("Multiple users returned") and the login fails
       silently, forever. Happens as soon as a local account (typically
       the bootstrap admin) shares an email with the SSO identity.
     - Empty email claim → `verify_claims()` is satisfied ("email" in
       claims is true for "" too) but the lookup returns none(), so every
       login retries create_user() and hits the unique-username
       constraint from the second attempt on.

   Both are resolved via `preferred_username`, which is what the account
   was named after in the first place: the >1 case is narrowed by it, and
   the empty-email case falls back to it. Accounts are never merged or
   renamed by this.

   The username is deliberately NOT consulted when the identity presents
   an email that simply matches no local account. Doing so would let
   anyone able to choose their own preferred_username in the IdP take over
   a same-named local account, superuser ones included. That case keeps
   the old behaviour (create_user, and a uniqueness error if the username
   is taken) — see HOWTO.md "SSO login".

`LoggingOIDCCallbackView` exists because every failure branch of
`OIDCAuthenticationCallbackView.get()` redirects to
LOGIN_REDIRECT_URL_FAILURE with no log line of its own — and with that
setting left at its default, that URL is "/", exactly where a SUCCESSFUL
login lands. Success and failure were literally indistinguishable. See
`starcubes/settings.py` (OIDC block) and HOWTO.md "SSO login".
"""
from __future__ import annotations

import logging
import os

from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView

LOGGER = logging.getLogger(__name__)


def apply_db_overrides() -> None:
    """Apply DB-stored SSO configuration (starcubes_meta.app_config) onto
    django.conf.settings — the DB half of the env-first resolution.

    Runs once per process from `SourcesConfig.ready()`, which fires before
    the URLconf is imported in every entry path, so `/oidc/` route mounting
    and the authentication backend list pick the DB values up at boot.
    Exception-safe: any error logs a WARNING and leaves the env-derived
    settings untouched (the bootstrap process calls this before `migrate`,
    when app_config may not exist yet).

    SSO enable/disable through the GUI therefore applies on restart; the
    per-request reads inside mozilla_django_oidc (client secret, token/
    userinfo endpoints via get_settings) would even apply live, but the
    honest contract is the restart.
    """
    from django.conf import settings as s

    from core.config import effective_oidc

    try:
        cfg = effective_oidc()
    except Exception:
        LOGGER.warning("could not read app_config — SSO settings stay "
                       "environment-only")
        return

    def _db(key):
        # env wins; a set env var shadows the DB row by design.
        return os.environ.get(key) or cfg.get(key)

    issuer = _db("STARCUBES_OIDC_ISSUER")
    client_id = _db("STARCUBES_OIDC_CLIENT_ID")
    client_secret = _db("STARCUBES_OIDC_CLIENT_SECRET")
    display_name = _db("STARCUBES_OIDC_DISPLAY_NAME")

    if display_name:
        s.STARCUBES_OIDC_DISPLAY_NAME = display_name
    if not (issuer and client_id and client_secret):
        return
    if getattr(s, "OIDC_ENABLED", False):
        return      # env already enabled SSO — settings.py did this block
    # The same derived block settings.py builds from the env trio, only
    # when the DB completes a trio the env did not.
    issuer = issuer.rstrip("/")
    s.OIDC_ENABLED = True
    s.AUTHENTICATION_BACKENDS = [
        "starcubes.oidc.KeycloakOIDCBackend",
        "django.contrib.auth.backends.ModelBackend",
    ]
    s.OIDC_RP_CLIENT_ID = client_id
    s.OIDC_RP_CLIENT_SECRET = client_secret
    s.OIDC_RP_SIGN_ALGO = "RS256"
    s.OIDC_RP_SCOPES = "openid email profile"
    s.OIDC_OP_AUTHORIZATION_ENDPOINT = f"{issuer}/protocol/openid-connect/auth"
    s.OIDC_OP_TOKEN_ENDPOINT = f"{issuer}/protocol/openid-connect/token"
    s.OIDC_OP_USER_ENDPOINT = f"{issuer}/protocol/openid-connect/userinfo"
    s.OIDC_OP_JWKS_ENDPOINT = f"{issuer}/protocol/openid-connect/certs"
    s.LOGIN_REDIRECT_URL_FAILURE = "/login/?sso=failed"
    s.OIDC_CALLBACK_CLASS = "starcubes.oidc.LoggingOIDCCallbackView"


class LoggingOIDCCallbackView(OIDCAuthenticationCallbackView):
    """Callback view that says which branch it took.

    Only `login_failure()` and `login_success()` are overridden: every one
    of `get()`'s exits funnels through exactly one of the two, so this
    covers all of them without duplicating the library's flow control.
    """

    def login_failure(self):
        request = self.request
        # `self.user` is only assigned on the branch that reaches
        # auth.authenticate() — getattr, not self.user, or this handler
        # raises AttributeError on the earlier exits.
        user = getattr(self, "user", None)
        LOGGER.warning(
            "OIDC login failed: op_error=%r op_error_description=%r "
            "had_oidc_states=%s user=%r is_active=%s redirecting_to=%r",
            request.GET.get("error"),
            request.GET.get("error_description"),
            "oidc_states" in request.session,
            getattr(user, "username", None),
            getattr(user, "is_active", None),
            self.failure_url,
        )
        return super().login_failure()

    def login_success(self):
        response = super().login_success()
        LOGGER.info(
            "OIDC login succeeded: user=%r redirecting_to=%r",
            getattr(self.user, "username", None),
            response.headers.get("Location"),
        )
        return response


class KeycloakOIDCBackend(OIDCAuthenticationBackend):
    def get_username(self, claims):
        username = claims.get("preferred_username")
        if username:
            return username
        return super().get_username(claims)

    def verify_claims(self, claims):
        """Accept the claims, but never let a missing email pass unnoticed.

        The library's implementation returns False when "email" is absent
        from a scope set that asked for it, which aborts the login with no
        usable diagnosis. Since filter_users_by_claims() below can match on
        the username instead, an email-less identity is workable — it just
        has to be visible in the log, because it disables the merge-by-email
        behaviour this backend otherwise relies on.
        """
        if not claims.get("email"):
            LOGGER.warning(
                "OIDC identity %r has no email claim: matching falls back to "
                "the username, and merging with an existing local account by "
                "email cannot happen.",
                claims.get("preferred_username"),
            )
        return True

    def filter_users_by_claims(self, claims):
        """Match by email first, then narrow / fall back to the username.

        Returns a QuerySet, as the library's caller expects (it takes
        len() of it and indexes into it). See the module docstring for why
        the plain email lookup is not sufficient.
        """
        email = claims.get("email")
        username = self.get_username(claims)

        by_email = self.UserModel.objects.none()
        if email:
            by_email = self.UserModel.objects.filter(email__iexact=email)

        count = len(by_email)
        if count == 1:
            return by_email

        if not username:
            return by_email

        if count == 0:
            # Deliberately NOT a general fallback. Matching a claimed email
            # against no local account and then grabbing whatever account
            # happens to carry the same username would let anyone who can pick
            # their own preferred_username in the IdP take over a local
            # account — including a superuser one. The username is only
            # consulted when there is no email to go on at all, where it is
            # the sole key available and refusing it means the identity can
            # never log in.
            if email:
                return by_email
            by_username = self.UserModel.objects.filter(username__iexact=username)
            if by_username:
                LOGGER.info(
                    "OIDC: identity %r has no email claim; matched an existing "
                    "account by username.",
                    username,
                )
            return by_username

        # count > 1: several local accounts share this email. Prefer the one
        # actually named after the Keycloak identity; if that is not exactly
        # one, hand the ambiguous set back so the library still refuses to
        # guess — but now the log says why.
        narrowed = by_email.filter(username__iexact=username)
        if len(narrowed) == 1:
            LOGGER.warning(
                "OIDC: %d accounts share email %r; picked %r by username.",
                count,
                email,
                username,
            )
            return narrowed
        LOGGER.warning(
            "OIDC: %d accounts share email %r and none of them is uniquely "
            "named %r — login will be refused as ambiguous. Fix the duplicate "
            "email addresses in auth_user.",
            count,
            email,
            username,
        )
        return by_email

    def create_user(self, claims):
        user = super().create_user(claims)
        if user.is_superuser or user.is_staff:
            user.is_superuser = False
            user.is_staff = False
            user.save(update_fields=["is_superuser", "is_staff"])
        LOGGER.info("OIDC: provisioned new account %r (email %r).",
                    user.username, user.email)
        return user
