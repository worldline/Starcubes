"""Contact and notification-preference columns on ``starcubes_meta.auth_user``
that the Django user model does not know: ``xmpp_jid`` (0030) and the channel
opt-in pair ``notify_email``/``notify_xmpp`` (0032, deliberate default off).

One module owns the raw SQL for those columns so the column list — and the
knowledge of which fields live outside the ORM — cannot drift between the
callers (``sources/views.py``'s self-service pages and the dispatcher in
``sources/alarm_notify.py``). Django-free on purpose, like ``core/``: every
function takes the engine, so both the web process (`_meta_engine()`) and the
management commands (their own cached engine) can call it.
"""

from sqlalchemy import text

# The full contact/preference column list of an auth_user row as the
# notification stack reads it. Build SELECTs from this so a new column
# lands everywhere at once.
CONTACT_COLS = "id, username, email, xmpp_jid, notify_email, notify_xmpp"


def user_contacts(engine, user_id: int) -> dict | None:
    """One account's contact/preference row, or None for an unknown id."""
    with engine.begin() as conn:
        row = conn.execute(text(
            f"SELECT {CONTACT_COLS} FROM starcubes_meta.auth_user "
            " WHERE id = :id"
        ), {"id": user_id}).mappings().one_or_none()
    return dict(row) if row else None


def list_contacts(engine) -> list[dict]:
    """Every account as a contact row — the alarm forms' address book
    (``recipients_list``). Ordered by username for the select options."""
    with engine.begin() as conn:
        rows = conn.execute(text(
            f"SELECT {CONTACT_COLS} FROM starcubes_meta.auth_user "
            " ORDER BY username"
        )).mappings().all()
    return [dict(r) for r in rows]


def set_xmpp_jid(engine, user_id: int, jid: str) -> None:
    """Write the account's XMPP JID. Empty string clears it (NULL), mirroring
    the email field's semantics."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE starcubes_meta.auth_user SET xmpp_jid = NULLIF(:j, '') "
            " WHERE id = :id"
        ), {"j": jid, "id": user_id})


def set_notify_optin(engine, user_id: int,
                     email_ok: bool, xmpp_ok: bool) -> None:
    """Write the channel opt-in pair. Dispatch (alarm_notify._addresses)
    requires the flag IN ADDITION to the alarm listing the account."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE starcubes_meta.auth_user "
            "   SET notify_email = :e, notify_xmpp = :x "
            " WHERE id = :id"
        ), {"e": email_ok, "x": xmpp_ok, "id": user_id})