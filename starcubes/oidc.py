"""Custom mozilla-django-oidc backend for Keycloak SSO.

Two departures from the library's defaults, both deliberate:

1. Django username is taken from Keycloak's `preferred_username` claim
   instead of the library's default (a base64/sha1 hash of the email
   address) — needed because this app already has password-based
   usernames, and username collisions/mapping should be an explicit,
   documented choice rather than an opaque hash nobody can look up.

2. `create_user()` force-clears is_superuser/is_staff — belt-and-suspenders
   on top of Django's own model defaults, so SSO-provisioned accounts can
   never come back with elevated access even if a future Django release
   changes create_user()'s defaults. `sources/views.py`'s `settings_page`
   (the only place in the app that checks `is_superuser`) stays a
   manually-granted local flag for both password and SSO accounts.

Everything else is stock OIDCAuthenticationBackend behaviour. Notably
`filter_users_by_claims()` is NOT overridden: it matches existing Django
users by email (case-insensitive), so an admin whose local password
account shares an email address with their Keycloak identity will log in
as that same account on first SSO login rather than getting a second
account. That's intentional merge behaviour, not a bug.

Known edge case: if a local username collides with a Keycloak
preferred_username claim but the two accounts' emails do NOT match,
email-based filter_users_by_claims() won't find a match and create_user()
will hit Django's unique username constraint, surfacing as a 500 on first
SSO login for that identity. See HOWTO.md "SSO login" for the operator
note.
"""
from __future__ import annotations

from mozilla_django_oidc.auth import OIDCAuthenticationBackend


class KeycloakOIDCBackend(OIDCAuthenticationBackend):
    def get_username(self, claims):
        username = claims.get("preferred_username")
        if username:
            return username
        return super().get_username(claims)

    def create_user(self, claims):
        user = super().create_user(claims)
        if user.is_superuser or user.is_staff:
            user.is_superuser = False
            user.is_staff = False
            user.save(update_fields=["is_superuser", "is_staff"])
        return user
