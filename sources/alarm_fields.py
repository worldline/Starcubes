"""Alarm field validation, shared by the web CRUD and the alarm import.

Everything here used to live in `sources/views.py`. It moved out when the
alarm export/import arrived, because the importer has to validate a rule
exactly the way a form save does — a second, parallel set of rules would
be a second truth about what a valid alarm is, and the two would drift.

Two consequences shape the module:

- **`parse_alarm_fields` takes a mapping, not a request.** It only ever
  used `.get()`, `in` and `.getlist()`, so a `MultiValueDict` built from a
  YAML entry satisfies it just as well as `request.POST` does. The
  "absent vs. present-but-empty" semantics of `enabled`, `severity`,
  `context` and the two recipient lists ride on that distinction, which is
  precisely why the importer must reuse this function instead of
  reimplementing it.
- **The semantic checks return `(error, fields, status)`, not a
  `JsonResponse`.** A management command has no HTTP response to return.
  `sources/views.py` keeps two thin wrappers that turn the tuple back into
  the JSON body the endpoints have always sent, so the CRUD's behaviour is
  unchanged.

The meta engine and the destination DSN arrive as parameters rather than
being fetched here, which keeps this module free of any opinion about
where the caller got them — the same reason `core/` takes them that way.
"""

from __future__ import annotations

import json as _json
import re

from sqlalchemy import text

from core.alarm import (check_no_grouped_filters, measure_value_expr,
                        resolve_context_labels)
from core.pipeline_alarm import CONDITIONS as _PIPELINE_CONDS
from core.query import (MEASURE_FUNCS, build_filter_tree,
                        validate_filter_tree)
from core.source import Source


# Comparison operators accepted by starcubes_meta.alarm.cond_op's CHECK
# constraint — the DB enforces it, but validating here gives a friendlier
# 400 than a Postgres exception bubbling up through the generic except.
ALARM_OPS = {">", "<", "=", ">=", "<="}
# The pipeline conditions, keyed by code → the label the GUI shows. Read from
# core so the list cannot drift from the evaluator or from the CHECK
# constraint that backs it.
PIPELINE_CONDITIONS = {c.code: c.title
                       for c in _PIPELINE_CONDS.values()}

# Notification channels — every value `alarm.notify_channels` accepts.
# email/xmpp/pagerduty dispatch via sources/alarm_notify.py; `log` is no
# longer selectable (the episode row plus the evaluator's log line are
# always on, core/alarm.py) but is silently dropped from submitted lists
# because legacy rows may still store it; `slack` stays reserved without a
# transport — see docs/backlog.md.
ALARM_CHANNELS = {"email", "xmpp", "slack", "pagerduty"}

# PagerDuty trigger severities an alarm.severity may carry (CHECK constraint
# alarm_severity_check mirrors this set in the DB). NULL/absent = fall back
# to STARCUBES_PAGERDUTY_SEVERITY at dispatch time.
ALARM_SEVERITIES = {"critical", "error", "warning", "info"}

# Filter ops an alarm context row may carry — the same six the chart
# serializes (core.query._VALID_OPS).
ALARM_CONTEXT_OPS = {"in", "not_in", "starts_with", "starts_not_with",
                      "contains", "not_contains"}


def parse_context_field(raw_ctx):
    """Parse the `context` POST field → (rows, error_message).

    `None` rows with no error means the field was absent, which the edit
    path reads as "keep the stored context". Shared by the alarm CRUD and
    the duplicate lookup so the accepted shape — and the wording that
    rejects it — cannot drift between them.
    """
    if raw_ctx is None:
        return None, None
    raw = raw_ctx.strip()
    if not raw:
        return [], None
    try:
        parsed = _json.loads(raw)
    except ValueError:
        return None, "context must be a JSON array of filter rows"
    if not isinstance(parsed, list) or not all(
            isinstance(r, dict) and isinstance(r.get("col"), str)
            and r.get("op") in ALARM_CONTEXT_OPS
            and isinstance(r.get("vals"), list)
            and all(isinstance(v, (str, int)) for v in r["vals"])
            for r in parsed):
        return None, ("context rows must be "
                      "{col, op, vals[, andOr, indent]} "
                      f"with op one of {sorted(ALARM_CONTEXT_OPS)}")
    return parsed, None


def parse_alarm_fields(data, *,
                       rule_kind: str | None = None
                       ) -> tuple[dict, dict]:
    """Validate POST fields for an alarm and return (values, errors).

    Both alarms_list (which builds an HTML row) and the add/edit endpoints
    call this so the validation lives in one place. ``errors`` keys are
    field names; values are short user-facing messages. ``values`` carries
    the typed-and-clamped payload to feed straight into an INSERT/UPDATE.

    ``context`` arrives as a JSON string (the chart's flat filter rows) —
    FormData-compatible for both callers. Absent on edit means "keep the
    stored context" (the inline editor never sends it — a filter expression
    is not inline-editable); an explicit empty array means a global alarm.
    Semantic validation (col/measure resolution) happens afterwards in
    `check_alarm_context`, which needs the Source.
    """
    values: dict = {
        "datasource_id": None,
        "name": "",
        "comment": None,
        "context_rows": None,
        "measure": "count(*)",
        "cond_op": ">",
        "threshold": 0.0,
        "window_s": 300,
        "notify_channels": [],
        "notify_email_recipients": [],
        "notify_xmpp_recipients": [],
        "enabled": True,
        "rule_kind": "threshold",
        "condition": None,
    }
    errors: dict = {}

    # A pipeline alarm watches ETL state, not fact rows, so measure,
    # window_s and the context are meaningless for it. Skipping their
    # branches rather than letting the defaults through is the point: a
    # pipeline row silently acquiring measure='count(*)', window_s=300 would
    # be indistinguishable from a threshold row that had been mis-saved.
    # On an edit the caller passes the *stored* kind: an alarm's kind is not
    # editable, and the inline row editor does not send the field at all —
    # letting it default to 'threshold' there would rewrite a pipeline row's
    # measure and window_s on every save.
    rule_kind_from_post = rule_kind is None
    rule_kind = (rule_kind
                 or (data.get("rule_kind") or "threshold").strip())
    if rule_kind not in ("threshold", "pipeline"):
        errors["rule_kind"] = "rule_kind must be threshold or pipeline"
        rule_kind = "threshold"
    values["rule_kind"] = rule_kind
    is_pipeline = rule_kind == "pipeline"
    if is_pipeline and "condition" in data:
        condition = (data.get("condition") or "").strip()
        if condition not in PIPELINE_CONDITIONS:
            errors["condition"] = (f"condition must be one of "
                                   f"{sorted(PIPELINE_CONDITIONS)}")
        else:
            values["condition"] = condition
    elif is_pipeline and rule_kind_from_post:
        # Creating a pipeline alarm without naming its condition is not a
        # keep-stored case, it is an incomplete form.
        errors["condition"] = (f"condition is required; one of "
                               f"{sorted(PIPELINE_CONDITIONS)}")

    name = (data.get("name") or "").strip()
    if not name:
        errors["name"] = "name is required"
    elif len(name) > 200:
        errors["name"] = "name is too long (max 200 characters)"
    else:
        values["name"] = name

    comment = (data.get("comment") or "").strip()
    if len(comment) > 10000:
        errors["comment"] = "comment is too long (max 10000 characters)"
    else:
        values["comment"] = comment or None

    cond_op = (data.get("cond_op") or ">").strip()
    if cond_op not in ALARM_OPS:
        errors["cond_op"] = f"cond_op must be one of {sorted(ALARM_OPS)}"
    else:
        values["cond_op"] = cond_op

    try:
        values["threshold"] = float(data.get("threshold") or 0)
    except (TypeError, ValueError):
        errors["threshold"] = "threshold must be a number"

    if not is_pipeline:
        try:
            window_s = int(data.get("window_s") or 300)
            if window_s <= 0:
                errors["window_s"] = "window_s must be > 0"
            else:
                values["window_s"] = window_s
        except (TypeError, ValueError):
            errors["window_s"] = "window_s must be an integer"

    # Syntactic measure check — the semantic column check needs the Source
    # and lives in check_alarm_context. quantile(col) is rejected here:
    # it is not a scalar a threshold can compare against.
    if not is_pipeline:
        measure = (data.get("measure")
                   or "count(*)").strip() or "count(*)"
        m = re.match(r"(\w+)\((\w+)\)$", measure)
        if (measure != "count(*)" and not m
                and not re.match(r"^\w+$", measure)):
            errors["measure"] = ("measure must be count(*), a column, or "
                                 "func(col) with func in sum/avg/min/max")
        elif m and m.group(1) not in MEASURE_FUNCS:
            errors["measure"] = (f"measure function {m.group(1)!r} is not "
                                 f"comparable; choose from count(*), "
                                 f"{sorted(MEASURE_FUNCS)} or a bare column")
        else:
            values["measure"] = measure

    if not is_pipeline and "context" in data:
        rows, ctx_err = parse_context_field(data.get("context"))
        if ctx_err:
            errors["context"] = ctx_err
        else:
            values["context_rows"] = rows

    # Channels arrive as checkbox values (one entry per checked box), a CSV
    # string, or a mix of both — getlist covers every shape at once.
    channels = []
    for chunk in data.getlist("notify_channels"):
        channels.extend(c.strip() for c in str(chunk).split(",") if c.strip())
    channels = [c for c in channels if c]
    # `log` is always on (not dispatched any more); drop it silently so a
    # legacy alarm whose stored array still carries it survives an edit.
    channels = [c for c in channels if c != "log"]
    bad = [c for c in channels if c not in ALARM_CHANNELS]
    if bad:
        errors["notify_channels"] = (f"unknown channel(s) {bad}; allowed: "
                                     f"{sorted(ALARM_CHANNELS)}")
    else:
        values["notify_channels"] = channels

    # The two recipient arrays: `getlist` (a <select multiple> contributes
    # one entry per selected option) or a CSV string — both shapes arrive
    # from different callers, so accept both like notify_channels does.
    for field in ("notify_email_recipients", "notify_xmpp_recipients"):
        if field not in data:
            # Absent = "not part of this form" — keep the stored value on
            # edit. Only the explicit field drives a change.
            values[field] = None
            continue
        ids: list[int] = []
        ok = True
        for chunk in data.getlist(field):
            for piece in str(chunk).split(","):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    ids.append(int(piece))
                except ValueError:
                    errors[field] = f"{field} must be user ids"
                    ok = False
                    break
            if not ok:
                break
        if ok:
            values[field] = sorted(set(ids))

    # PagerDuty severity: optional per-alarm override. Absent or empty =
    # "fall back to the global STARCUBES_PAGERDUTY_SEVERITY at dispatch".
    # Validated against the same four values the DB CHECK constraint
    # (alarm_severity_check, migration 0032) accepts.
    sev = (data.get("severity") or "").strip().lower()
    if sev and sev not in ALARM_SEVERITIES:
        errors["severity"] = (f"severity must be one of "
                              f"{sorted(ALARM_SEVERITIES)}")
    else:
        values["severity"] = sev or None

    # Checkbox semantics, but only when the field was actually submitted:
    # an absent "enabled" means "not part of this form" (the add form used
    # to omit it entirely), not "disabled". Present-but-empty is the
    # unchecked box that saveRow sends.
    if "enabled" in data:
        values["enabled"] = data.get("enabled") in ("on", "true", "1")

    return values, errors


def alarm_row_to_dict(row) -> dict:
    """One starcubes_meta.alarm row → JSON-safe dict.

    Kept separate from the SELECTs so list/add/edit can all render the
    same shape (the editor reads it back the same way the list does).
    context_filter arrives already decoded (psycopg2 turns jsonb into a
    Python list). eval_end is the evaluator's runtime HWM marker, not user
    configuration — it is deliberately left out.
    """
    return {
        "id": row.id,
        "datasource_id": row.datasource_id,
        "name": row.name,
        "rule_kind": row.rule_kind,
        "condition": row.condition,
        "context_filter": row.context_filter,
        "comment": row.comment,
        "measure": row.measure,
        "cond_op": row.cond_op,
        "threshold": float(row.threshold),
        "window_s": row.window_s,
        "notify_channels": list(row.notify_channels or []),
        "notify_email_recipients": list(row.notify_email_recipients or []),
        "notify_xmpp_recipients": list(row.notify_xmpp_recipients or []),
        "severity": row.severity,
        "enabled": bool(row.enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def context_labels(meta, dsn: str, pk: int, contexts) -> dict:
    """{filter_column: {source_id: name}} for a set of alarm contexts.

    A context stores dimension ids; the names live in the dim tables and are
    resolved for display only (see core.alarm.resolve_context_labels). Purely
    cosmetic, so every failure degrades to raw ids: the alarm list has to
    render even when the fact database is unreachable or a dim table has not
    been created yet. The export leans on the same tolerance — it writes the
    names beside the ids as documentation, and a datasource it cannot reach
    simply exports without them.
    """
    if not any(contexts):
        return {}
    try:
        source = Source.load(meta, dest_dsn=dsn, name_or_id=pk)
        return resolve_context_labels(source, contexts)
    except Exception:
        return {}


def check_alarm_context(meta, dsn: str, pk: int, values: dict):
    """Semantically validate an alarm's context + measure against the Source.

    The stored expression is only as trustworthy as its write-time
    validation: this resolves every filter col through the same rules the
    chart uses (core.query.validate_filter_tree), forbids grouped tokens
    (user-editable group sets would silently change the alarm's meaning),
    and checks the measure column. Returns ``(error, fields, status)`` —
    ``(None, {}, 0)`` when the rule passes. Callers pass the value of
    `values["context_rows"]` inside — None means "not submitted" (edit keeps
    the stored context) and skips context validation; the measure check
    always runs.

    A pipeline alarm has neither a measure nor a context, so there is
    nothing here to check — and skipping the whole function also removes
    `Source.load` from its save path, which matters: a rule that watches an
    ETL must not become uneditable because the source database it watches
    is unreachable.
    """
    if values.get("rule_kind") == "pipeline":
        return None, {}, 0
    try:
        source = Source.load(meta, dest_dsn=dsn, name_or_id=pk)
    except LookupError:
        return f"datasource {pk} not found", {}, 404
    try:
        measure_value_expr(source, values["measure"])
        if values["context_rows"] is not None:
            tree = build_filter_tree(values["context_rows"])
            check_no_grouped_filters(tree)
            validate_filter_tree(source, tree)
    except ValueError as exc:
        return str(exc), {"context": str(exc)}, 400
    return None, {}, 0


def check_alarm_notify(meta, pk: int, values: dict):
    """Semantic validation of the notify fields — the counterpart to
    `check_alarm_context`, called after it by alarm_add/alarm_edit.

    Checks the selected user ids exist (`int[]` cannot carry a foreign key,
    so dangling ids are refused at save time instead), and enforces the
    channel/recipient pairing rules server-side:
    - `email` channel ⇒ at least one recipient with an e-mail address chosen
      (existence was just proven; the address itself may still be NULL on
      the account — caught at dispatch time with a WARNING).
    - `xmpp` channel ⇒ at least one XMPP recipient chosen.
    - `pagerduty` channel ⇒ the datasource has a routing key.
    Returns ``(error, fields, status)``, ``(None, {}, 0)`` when it passes.

    The import resolves usernames to ids and drops the unresolvable ones
    *before* calling this, because a dangling id is a 400 here — which is
    the right answer for a form, and the wrong one for a restore.
    """
    email_ids = values.get("notify_email_recipients")
    xmpp_ids = values.get("notify_xmpp_recipients")
    channels = values["notify_channels"]

    # An absent recipient field (None) means "not part of this form" — the
    # inline row editor sends channels only. The pairing rules then apply to
    # the stored value, which satisfied them at its own save time; `alarm_edit`
    # re-checks them against the effective (submitted-or-stored) value after
    # loading the old row.
    if "email" in channels and email_ids is not None and not email_ids:
        return ("email channel selected but no e-mail recipient chosen",
                {"notify_email_recipients": "choose at least one recipient"},
                400)
    if "xmpp" in channels and xmpp_ids is not None and not xmpp_ids:
        return ("xmpp channel selected but no XMPP recipient chosen",
                {"notify_xmpp_recipients": "choose at least one recipient"},
                400)

    ids = sorted(set(email_ids or []) | set(xmpp_ids or []))
    try:
        with meta.begin() as conn:
            if ids:
                found = {r[0] for r in conn.execute(text(
                    "SELECT id FROM starcubes_meta.auth_user "
                    " WHERE id = ANY(:ids)"
                ), {"ids": ids}).all()}
                missing = [i for i in ids if i not in found]
                if missing:
                    return (f"unknown user id(s) {missing} — the recipients "
                            f"are the login users, maintained on the "
                            f"settings page",
                            {"notify_email_recipients": "unknown user"}, 400)
            if "pagerduty" in channels:
                has_key = conn.execute(text(
                    "SELECT 1 FROM starcubes_meta.datasource "
                    " WHERE id = :ds AND pagerduty_routing_key IS NOT NULL"
                ), {"ds": pk}).scalar()
                if not has_key:
                    return ("pagerduty channel selected but this datasource "
                            "has no PagerDuty integration key configured "
                            "(set it on the source edit page)",
                            {"notify_channels": "no PagerDuty integration "
                                                "key on this datasource"},
                            400)
    except Exception as exc:
        return str(exc), {}, 0
    return None, {}, 0
