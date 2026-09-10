"""Reading and writing alarms for the export/import.

The counterpart to ``core/alarm_io.py``, which owns the file format. This
module owns everything that touches the database and the user table, and
it is shared by the GUI endpoints and the two management commands so a
restore run from a shell and one run from a button cannot diverge — the
same guarantee ``sources/profile_store.py`` gives the profile paths.

Three decisions are worth reading before changing anything here.

**The import is an upsert on (datasource_id, name), never a replace.**
Replacing would delete the target's alarms and take their
``alarm_history`` and ``alarm_firing`` rows with them through
``ON DELETE CASCADE``. A restore that destroys the operating history is
not a restore. Upsert also happens to be the only thing that works on a
fresh datasource: it already carries the two default pipeline alarms from
``profile_store.insert_default_pipeline_alarms``, and importing a file
that names them updates them instead of colliding with the unique index.

**Recipients travel as usernames.** ``notify_email_recipients`` and
``notify_xmpp_recipients`` are ``int[]`` of ``auth_user.id``, and an id
means nothing outside the installation that issued it. They are resolved
back here — and an unresolvable name is dropped with a warning rather than
refused, because the alternative is a backup that cannot be restored after
somebody deleted an account. Dropping the last recipient of a channel
drops the channel too: that is the pairing rule
``check_alarm_notify`` enforces anyway, and doing it here turns a hard
400 into a warning the operator can act on.

**Every rule goes through the CRUD's own validation.** ``alarm_form_data``
translates a file entry into the form shape, and
``sources/alarm_fields.parse_alarm_fields`` judges it — the same function
the Alarms tab posts into. An importer with its own idea of a valid alarm
would be a second truth, and the two would drift.
"""

from __future__ import annotations

import json as _json

from django.utils.datastructures import MultiValueDict
from sqlalchemy import text

from core.alarm import check_no_grouped_filters, normalize_context_rows
from core.query import build_filter_tree
from sources.accounts import CONTACT_COLS
from sources.alarm_fields import (alarm_row_to_dict, check_alarm_context,
                                  check_alarm_notify, context_labels,
                                  parse_alarm_fields)

# The columns the file carries, in the order a diff reads best. `name` is
# the key, so it is not a delta candidate.
_DIFF_FIELDS = ("rule_kind", "condition", "measure", "cond_op", "threshold",
                "window_s", "context_filter", "comment", "enabled",
                "severity", "notify_channels", "notify_email_recipients",
                "notify_xmpp_recipients")

_SELECT_ALARM = (
    "SELECT id, datasource_id, name, rule_kind, condition, context_filter, "
    "       comment, measure, cond_op, threshold, window_s, "
    "       notify_channels, notify_email_recipients, "
    "       notify_xmpp_recipients, severity, enabled, created_at, "
    "       updated_at "
    "  FROM starcubes_meta.alarm ")


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def user_maps(meta) -> tuple[dict, dict]:
    """``({id: username}, {username: id})`` for every account.

    One query for both directions: the export translates ids out, the
    import translates names back, and neither should walk the table per
    alarm.
    """
    with meta.begin() as conn:
        rows = conn.execute(text(
            f"SELECT {CONTACT_COLS} FROM starcubes_meta.auth_user"
        )).mappings().all()
    by_id = {r["id"]: r["username"] for r in rows}
    return by_id, {v: k for k, v in by_id.items()}


# --------------------------------------------------------------------------
# Reading — the export side
# --------------------------------------------------------------------------

def collect_alarms(meta, dsn: str, *, datasource_ids=None) -> list[dict]:
    """Alarms grouped by datasource, ready for ``core.alarm_io.dump_alarms``.

    ``datasource_ids`` selects; ``None`` takes every datasource. A
    datasource with no alarms is still returned, so a restore can tell "had
    none" from "was not exported".
    """
    by_id, _ = user_maps(meta)
    with meta.begin() as conn:
        params: dict = {}
        sql = "SELECT id, name FROM starcubes_meta.datasource"
        if datasource_ids is not None:
            sql += " WHERE id = ANY(:ids)"
            params["ids"] = list(datasource_ids)
        sources = conn.execute(text(sql + " ORDER BY name"),
                               params).mappings().all()
        rows = conn.execute(text(
            _SELECT_ALARM + " ORDER BY datasource_id, name")).mappings().all()

    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row["datasource_id"], []).append(row)

    out = []
    for ds in sources:
        alarms = []
        contexts = [r["context_filter"] for r in grouped.get(ds["id"], ())]
        labels = context_labels(meta, dsn, ds["id"], contexts)
        for row in grouped.get(ds["id"], ()):
            alarm = alarm_row_to_dict(row)
            for field in ("notify_email_recipients",
                          "notify_xmpp_recipients"):
                # An id with no account behind it is a leftover the settings
                # page's delete branch missed; it cannot be named, so it
                # cannot travel.
                alarm[field] = [by_id[i] for i in alarm[field] if i in by_id]
            # `log` is not a channel any more — the episode row and the
            # evaluator's log line are always on (core/alarm.py). Legacy
            # rows still store it and the field parser drops it on the next
            # edit; carrying it in a backup would export a channel that
            # cannot be chosen and would come back as a spurious change.
            alarm["notify_channels"] = [c for c in alarm["notify_channels"]
                                        if c != "log"]
            alarm["context_labels"] = _labels_for(alarm["context_filter"],
                                                  labels)
            alarms.append(alarm)
        out.append({"datasource": ds["name"], "alarms": alarms})
    return out


def _labels_for(context, labels: dict) -> dict:
    """The slice of the label map this one context actually uses."""
    if not context or not labels:
        return {}
    out: dict = {}
    for row in context:
        col = row.get("col")
        known = labels.get(col)
        if not known:
            continue
        for val in row.get("vals") or ():
            name = known.get(str(val))
            if name:
                out.setdefault(col, {})[str(val)] = name
    return out


# --------------------------------------------------------------------------
# Writing — the import side
# --------------------------------------------------------------------------

def alarm_form_data(alarm: dict, recipient_ids: dict) -> MultiValueDict:
    """One file entry → the form shape ``parse_alarm_fields`` reads.

    Every value is a string, because that is what a form submits and what
    the parser's ``.strip()`` calls expect. The recipient lists arrive as
    already-resolved ids.

    The fields are set *unconditionally* (empty where the file says
    nothing), which picks the file as the authority: an absent field in
    that parser means "keep what is stored", and a backup that silently
    kept the target's channels would not be a backup of anything.
    ``rule_kind`` is written into the data rather than passed as the
    parser's keyword argument, so a pipeline rule with no ``condition`` is
    rejected as the incomplete row it is.
    """
    ctx = alarm.get("context_filter")
    data = {
        "name": [alarm.get("name") or ""],
        "rule_kind": [alarm.get("rule_kind") or "threshold"],
        "comment": [alarm.get("comment") or ""],
        "cond_op": [str(alarm.get("cond_op") or ">")],
        "threshold": [str(alarm.get("threshold")
                          if alarm.get("threshold") is not None else 0)],
        "severity": [alarm.get("severity") or ""],
        "enabled": ["on" if alarm.get("enabled", True) else ""],
        "notify_channels": [",".join(alarm.get("notify_channels") or ())],
        "notify_email_recipients":
            [",".join(str(i) for i in recipient_ids.get("email", ()))],
        "notify_xmpp_recipients":
            [",".join(str(i) for i in recipient_ids.get("xmpp", ()))],
    }
    if alarm.get("condition"):
        data["condition"] = [alarm["condition"]]
    if (alarm.get("rule_kind") or "threshold") != "pipeline":
        data["measure"] = [alarm.get("measure") or "count(*)"]
        # `or 300` would be wrong here: window_s = 0 is a value the file
        # must be *refused* for (CHECK window_s > 0), and a falsy fallback
        # would quietly turn it into the default instead.
        window = alarm.get("window_s")
        data["window_s"] = [str(window if window is not None else 300)]
        # Always submitted, so the file decides the context. `[]` is a
        # global alarm, which is a configured state, not an absent one.
        data["context"] = [_json.dumps(ctx or [])]
    return MultiValueDict(data)


def _resolve_recipients(alarm: dict, by_name: dict, where: str,
                        warnings: list) -> dict:
    """Usernames → ids, dropping and reporting the ones with no account."""
    out = {}
    for kind, field in (("email", "notify_email_recipients"),
                        ("xmpp", "notify_xmpp_recipients")):
        ids, missing = [], []
        for username in alarm.get(field) or ():
            if username in by_name:
                ids.append(by_name[username])
            else:
                missing.append(username)
        if missing:
            warnings.append(
                f"{where}: no account named {', '.join(sorted(missing))} — "
                f"dropped from the {kind} recipients")
        out[kind] = sorted(set(ids))
    return out


def _drop_unusable_channels(alarm: dict, recipient_ids: dict,
                            has_pagerduty_key: bool, where: str,
                            warnings: list) -> None:
    """Remove channels this installation cannot honour, in place.

    Each of these is a 400 from ``check_alarm_notify`` when a form submits
    it. For an import they are the ordinary consequence of restoring
    somewhere else, so they degrade to a warning and a disabled channel —
    the alarm itself still lands, and still writes its firing episodes.
    """
    channels = list(alarm.get("notify_channels") or ())
    kept = []
    for channel in channels:
        if channel in ("email", "xmpp") and not recipient_ids.get(channel):
            warnings.append(
                f"{where}: the {channel} channel has no recipient left — "
                f"channel disabled")
            continue
        if channel == "pagerduty" and not has_pagerduty_key:
            warnings.append(
                f"{where}: this datasource has no PagerDuty integration "
                f"key — pagerduty channel disabled")
            continue
        kept.append(channel)
    alarm["notify_channels"] = kept


def _label_warnings(meta, dsn: str, ds_id: int, alarm: dict, where: str,
                    warnings: list) -> None:
    """Compare the file's context_labels with what the ids resolve to now.

    A context stores dimension **ids**, and a dim_index is an import
    position rather than a stable identifier — the same caveat
    ``docs/source-profiles.md`` records for ``group_dims.members``. When
    the exported name no longer matches, the alarm still imports: the id is
    what the fact table joins on, and only a human can say whether the
    slice is still the intended one.
    """
    expected = alarm.get("context_labels")
    context = alarm.get("context_filter")
    if not expected or not context:
        return
    actual = _labels_for(context, context_labels(meta, dsn, ds_id, [context]))
    for col, wanted in expected.items():
        for value, name in wanted.items():
            found = (actual.get(col) or {}).get(str(value))
            if found is None:
                warnings.append(
                    f"{where}: {col}={value} was exported as {name!r} but "
                    f"resolves to nothing here — verify the slice")
            elif found != name:
                warnings.append(
                    f"{where}: {col}={value} was exported as {name!r} but "
                    f"is {found!r} here — verify the slice")


def _comparable(values: dict) -> dict:
    """The submitted values in the shape ``_deltas`` compares."""
    return {
        "rule_kind": values["rule_kind"],
        "condition": values["condition"],
        "measure": values["measure"],
        "cond_op": values["cond_op"],
        "threshold": float(values["threshold"]),
        "window_s": values["window_s"],
        "context_filter": normalize_context_rows(values["context_rows"]),
        "comment": values["comment"] or None,
        "enabled": bool(values["enabled"]),
        "severity": values["severity"] or None,
        "notify_channels": sorted(values["notify_channels"] or ()),
        "notify_email_recipients":
            sorted(values["notify_email_recipients"] or ()),
        "notify_xmpp_recipients":
            sorted(values["notify_xmpp_recipients"] or ()),
    }


def _stored_comparable(row: dict) -> dict:
    """The stored row in the same shape."""
    return {
        "rule_kind": row["rule_kind"],
        "condition": row["condition"],
        "measure": row["measure"],
        "cond_op": row["cond_op"],
        "threshold": float(row["threshold"]),
        "window_s": row["window_s"],
        "context_filter": normalize_context_rows(row["context_filter"]),
        "comment": row["comment"] or None,
        "enabled": bool(row["enabled"]),
        "severity": row["severity"] or None,
        "notify_channels": sorted(row["notify_channels"] or ()),
        "notify_email_recipients": sorted(row["notify_email_recipients"]
                                          or ()),
        "notify_xmpp_recipients": sorted(row["notify_xmpp_recipients"] or ()),
    }


def _deltas(old: dict, new: dict) -> dict:
    """``{field: {"from": …, "to": …}}`` for the fields that differ.

    A pipeline rule's measure, window_s and context are the column defaults
    and mean nothing to it, so they never count as a change — otherwise
    every pipeline alarm in a file would read as modified.
    """
    skip = ({"measure", "window_s", "context_filter"}
            if new.get("rule_kind") == "pipeline" else set())
    out = {}
    for field in _DIFF_FIELDS:
        if field in skip:
            continue
        if old.get(field) != new.get(field):
            out[field] = {"from": old.get(field), "to": new.get(field)}
    return out


_UPSERT_ALARM = (
    "INSERT INTO starcubes_meta.alarm "
    "  (datasource_id, name, rule_kind, condition, context_filter, comment, "
    "   measure, cond_op, threshold, window_s, notify_channels, "
    "   notify_email_recipients, notify_xmpp_recipients, severity, enabled) "
    "VALUES (:ds, :name, :rk, :cond, CAST(:ctx AS jsonb), :cmt, "
    "        :m, :op, :thr, :ws, CAST(:nc AS text[]), :ner, :nxr, "
    "        :sev, :en) "
    "ON CONFLICT (datasource_id, name) DO UPDATE SET "
    "   rule_kind = EXCLUDED.rule_kind, condition = EXCLUDED.condition, "
    "   context_filter = EXCLUDED.context_filter, "
    "   comment = EXCLUDED.comment, measure = EXCLUDED.measure, "
    "   cond_op = EXCLUDED.cond_op, threshold = EXCLUDED.threshold, "
    "   window_s = EXCLUDED.window_s, "
    "   notify_channels = EXCLUDED.notify_channels, "
    "   notify_email_recipients = EXCLUDED.notify_email_recipients, "
    "   notify_xmpp_recipients = EXCLUDED.notify_xmpp_recipients, "
    "   severity = EXCLUDED.severity, enabled = EXCLUDED.enabled, "
    "   updated_at = now() "
    "RETURNING id, datasource_id, name, rule_kind, condition, "
    "          context_filter, comment, measure, cond_op, threshold, "
    "          window_s, notify_channels, notify_email_recipients, "
    "          notify_xmpp_recipients, severity, enabled, created_at, "
    "          updated_at")
# eval_end and created_at are absent from the SET list on purpose. eval_end
# is the evaluator's runtime high-watermark and must never look like a user
# edit (docs/alarm.md); created_at is when the alarm first existed here,
# which an import does not change.


class AlarmImportError(ValueError):
    """The import cannot proceed. Carries the report that says why."""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}


def import_alarms(meta, dsn: str, entries, *, username: str,
                  datasource_id: int | None = None,
                  confirm: bool = False) -> dict:
    """Plan — and with ``confirm``, apply — an alarm import.

    Two phases in one function, the same shape ``profile_apply`` uses:
    without ``confirm`` nothing is written and the caller gets the diff to
    show; with it, the writes land in one transaction.

    ``datasource_id`` restricts the import to one datasource. A file
    holding exactly one block is then applied to that datasource **even if
    the names differ** — the operator picked the target explicitly, and a
    datasource renamed since the backup is the ordinary case. With several
    blocks the match is by name and the rest are reported as skipped,
    because guessing which block was meant is not something a restore
    should do.

    A **malformed file** writes nothing at all, ``confirm`` or not: a
    half-applied restore from a file that cannot be trusted is worse than
    a refused one. A rule that merely does not *fit this installation* is
    a different case and is skipped rather than fatal — a fact column
    dropped since the backup, a deleted recipient account, a missing
    PagerDuty key, a dimension id that no longer carries the exported
    name. Refusing the file over one stale rule would make the other forty
    unrestorable exactly when they are needed. Everything skipped is named
    in the report, per rule and with its reason.

    Only added and changed rules are written. An unchanged one is left
    alone so a second import moves no ``updated_at`` and writes no
    ``alarm_history`` row.
    """
    _, by_name = user_maps(meta)
    report: dict = {"applied": False, "warnings": [], "errors": [],
                    "datasources": [], "skipped_datasources": []}

    with meta.begin() as conn:
        ds_rows = conn.execute(text(
            "SELECT id, name, "
            "       pagerduty_routing_key IS NOT NULL AS has_pagerduty "
            "  FROM starcubes_meta.datasource")).mappings().all()
    by_ds_name = {r["name"]: dict(r) for r in ds_rows}
    by_ds_id = {r["id"]: dict(r) for r in ds_rows}

    target = None
    if datasource_id is not None:
        target = by_ds_id.get(datasource_id)
        if target is None:
            raise AlarmImportError(f"datasource {datasource_id} not found",
                                   report)

    planned: list[dict] = []
    for entry in entries:
        name = entry["datasource"]
        if target is not None:
            if len(entries) == 1:
                ds = target
                if name != target["name"]:
                    report["warnings"].append(
                        f"the file was exported from {name!r}; importing it "
                        f"into {target['name']!r}")
            elif by_ds_name.get(name, {}).get("id") == target["id"]:
                ds = target
            else:
                report["skipped_datasources"].append(
                    {"datasource": name,
                     "reason": f"not the datasource being imported into "
                               f"({target['name']})"})
                continue
        else:
            ds = by_ds_name.get(name)
            if ds is None:
                report["skipped_datasources"].append(
                    {"datasource": name,
                     "reason": "no datasource of that name on this "
                               "installation"})
                continue

        section = _plan_datasource(meta, dsn, ds, entry, by_name,
                                   report, planned)
        report["datasources"].append(section)

    if report["errors"]:
        return report
    if not confirm:
        return report

    with meta.begin() as conn:
        for item in planned:
            row = conn.execute(text(_UPSERT_ALARM),
                               item["params"]).mappings().one()
            conn.execute(text(
                "INSERT INTO starcubes_meta.alarm_history "
                "  (alarm_id, kind, username, old_row, new_row) "
                "VALUES (:aid, :kind, :user, CAST(:old AS jsonb), "
                "        CAST(:new AS jsonb))"
            ), {"aid": row.id,
                "kind": "updated" if item["old"] else "created",
                "user": username,
                "old": _json.dumps(item["old"]) if item["old"] else None,
                "new": _json.dumps(alarm_row_to_dict(row))})
    report["applied"] = True
    return report


def _plan_datasource(meta, dsn: str, ds: dict, entry: dict, by_name: dict,
                     report: dict, planned: list) -> dict:
    """Validate one datasource's block and append its writes to ``planned``."""
    section = {"datasource": ds["name"], "id": ds["id"], "added": [],
               "changed": [], "unchanged": [], "skipped": []}
    with meta.begin() as conn:
        rows = conn.execute(text(_SELECT_ALARM + " WHERE datasource_id = :ds"),
                            {"ds": ds["id"]}).mappings().all()
    stored = {r["name"]: alarm_row_to_dict(r) for r in rows}

    for alarm in entry["alarms"]:
        where = f"{ds['name']}: {alarm['name']}"
        recipient_ids = _resolve_recipients(alarm, by_name, where,
                                            report["warnings"])
        _drop_unusable_channels(alarm, recipient_ids, ds["has_pagerduty"],
                                where, report["warnings"])

        values, errors = parse_alarm_fields(
            alarm_form_data(alarm, recipient_ids))
        if errors:
            report["errors"].append(
                {"alarm": where,
                 "error": "; ".join(f"{k}: {v}" for k, v in errors.items())})
            continue

        old = stored.get(alarm["name"])
        if old is not None and old["rule_kind"] != values["rule_kind"]:
            # rule_kind is not editable in the CRUD either: a threshold rule
            # and a pipeline rule ask different questions, and silently
            # turning one into the other would keep the name and change the
            # meaning.
            section["skipped"].append(
                {"name": alarm["name"],
                 "reason": f"stored alarm is a {old['rule_kind']} rule, the "
                           f"file has a {values['rule_kind']} rule — an "
                           f"alarm cannot change its kind"})
            continue

        unfit = _check_semantics(meta, dsn, ds, values, where, report)
        if unfit is not None:
            section["skipped"].append({"name": alarm["name"],
                                       "reason": unfit})
            continue
        _label_warnings(meta, dsn, ds["id"], alarm, where, report["warnings"])

        new_cmp = _comparable(values)
        if old is None:
            section["added"].append(alarm["name"])
        else:
            deltas = _deltas(_stored_comparable(old), new_cmp)
            if not deltas:
                section["unchanged"].append(alarm["name"])
                continue
            section["changed"].append({"name": alarm["name"],
                                       "deltas": deltas})
        planned.append({"old": old,
                        "params": _upsert_params(ds["id"], values, old)})
    return section


def _check_semantics(meta, dsn: str, ds: dict, values: dict, where: str,
                     report: dict):
    """Run the CRUD's two semantic checks against the target datasource.

    Returns ``None`` when the rule fits, else the reason it does not — and
    a rule that does not fit is **skipped, not fatal**. A restore meets
    rules that no longer match the schema they were written against (a
    fact column dropped since the backup, a measure renamed), and refusing
    the whole file over one stale rule would make the other forty
    unrestorable. That is the same call already made for a deleted
    recipient account and a missing PagerDuty key: what is wrong about
    *this installation* is reported, never fatal. A malformed **file** is
    a different matter and still refuses everything.

    A context check needs ``Source.load``, which raises on a datasource
    that is not fully configured yet. That is the ordinary state of a
    freshly rebuilt installation — precisely when a restore happens — so an
    unloadable source downgrades to a warning and the rule still imports.
    The ids in the context are what the fact table joins on and they are
    carried through untouched either way.

    The grouped-token ban is checked *first and unconditionally*, because
    it needs no Source at all: a ``<col>:grouped:<slug>`` reference points
    at a user-editable group set, so it would keep working while its
    meaning changed. Leaving it to the tolerant branch above would let a
    file smuggle in what the CRUD refuses.
    """
    if values["context_rows"]:
        try:
            check_no_grouped_filters(build_filter_tree(values["context_rows"]))
        except ValueError as exc:
            # A grouped token is the file being wrong, not the installation
            # — the CRUD refuses it too, so this one is fatal.
            report["errors"].append({"alarm": where, "error": str(exc)})
            return "grouped filter column"
    try:
        error, _fields, _status = check_alarm_context(meta, dsn, ds["id"],
                                                      values)
    except Exception as exc:
        report["warnings"].append(
            f"{where}: could not check the context against the datasource "
            f"({exc}) — imported unverified")
        error = None
    if error:
        report["warnings"].append(f"{where}: {error} — rule skipped")
        return error

    error, _fields, _status = check_alarm_notify(meta, ds["id"], values)
    if error:
        report["warnings"].append(f"{where}: {error} — rule skipped")
        return error
    return None


def _upsert_params(ds_id: int, values: dict, old: dict | None) -> dict:
    """Bind parameters for ``_UPSERT_ALARM``.

    A pipeline rule keeps whatever measure/window_s/context the stored row
    had rather than taking the parser's defaults: those three columns mean
    nothing to it, and rewriting them would show up as churn in a diff that
    should be about the rule.
    """
    measure, window_s, ctx = (values["measure"], values["window_s"],
                              values["context_rows"])
    if values["rule_kind"] == "pipeline" and old is not None:
        measure, window_s, ctx = (old["measure"], old["window_s"],
                                  old["context_filter"])
    return {"ds": ds_id, "name": values["name"],
            "rk": values["rule_kind"], "cond": values["condition"],
            "ctx": _json.dumps(ctx) if ctx else None,
            "cmt": values["comment"], "m": measure,
            "op": values["cond_op"], "thr": values["threshold"],
            "ws": window_s, "nc": list(values["notify_channels"]),
            "ner": values["notify_email_recipients"] or [],
            "nxr": values["notify_xmpp_recipients"] or [],
            "sev": values["severity"], "en": values["enabled"]}
