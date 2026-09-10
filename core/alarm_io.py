"""Serialise and parse the alarm export file.

An alarm is not part of a source profile, and the reason is worth stating
where the code lives. A profile describes what a datasource *is* — fact
columns, dimensions, measures — and applies cleanly to an empty schema.
An alarm is operating state *over* a datasource that already exists: it
hangs off ``datasource_id``, names dimension values by id in its context,
and names its recipients as ``auth_user`` ids. None of that exists at the
moment a profile is rolled out.

There is a mechanical reason too. ``profile_apply`` is a *replace*: it
deletes every child row through ``sources/profile_store.py:_CHILD_TABLES``
and re-inserts the profile's. Were ``alarm`` in that list, every Apply
Profile would take ``alarm_history`` and ``alarm_firing`` with it through
``ON DELETE CASCADE``. So alarms travel in a file of their own, and the
import is an **upsert**, never a replace.

This module owns the *document*: its shape, its header, its size cap. It
deliberately does **not** validate alarm fields — that is
``sources/alarm_fields.py``, the same code a form save goes through, so
there is one truth about what a valid alarm is rather than two.

What the file carries is one half of ``starcubes_meta.alarm``:

- **configuration**, which is the point of a backup: ``name``,
  ``rule_kind``, ``condition``, ``measure``, ``cond_op``, ``threshold``,
  ``window_s``, ``context_filter``, ``comment``, ``enabled``, ``severity``,
  ``notify_channels`` and the two recipient lists;
- **not** identity or runtime: ``id``, ``datasource_id`` (the datasource
  travels as its *name*, in the header), ``eval_end``, ``created_at``,
  ``updated_at``, nor ``alarm_history``/``alarm_firing`` — that is
  operating history, and an upsert leaves the target's own intact.

``eval_end`` is left out for the same reason the CRUD leaves it out of its
history snapshots: it is the evaluator's runtime high-watermark, not
something a user configured (see ``docs/alarm.md``).

Two fields need a word:

``context_filter`` is copied **verbatim**. It is exactly the flat filter
rows ``chart.html:gatherParams()`` serialises and ``alarm.context_filter``
jsonb already stores in that shape. Reformatting it here would be a second
representation of the same thing.

``context_labels`` is the one derived block — the names
``core.alarm.resolve_context_labels`` gives for the ids in the context. It
is documentation, and a check: a context stores dimension **ids**, and
those are import positions, not stable identifiers, exactly as
``docs/source-profiles.md`` warns for ``group_dims.members``. The importer
re-resolves the ids and *warns* when a name no longer matches. It is never
written back — a label is not configuration.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import yaml

# The header every file carries. A profile is identified by its filename in
# a catalogue; this file has no catalogue, so it says what it is, and an
# upload of the wrong YAML is refused instead of half-parsed.
FORMAT = "starcubes-alarms/1"

# Cap on an uploaded export, the same 2 MB core/profile.py caps a profile
# at, and checked before anything reaches the database for the same reason.
MAX_ALARM_FILE_BYTES = 2 * 1024 * 1024

# The alarm keys this format carries. Anything else in a stored row is
# identity or runtime and never travels — see the module docstring.
ALARM_KEYS = ("name", "rule_kind", "condition", "measure", "cond_op",
              "threshold", "window_s", "context_filter", "comment",
              "enabled", "severity", "notify_channels",
              "notify_email_recipients", "notify_xmpp_recipients")


class AlarmIOError(ValueError):
    """Malformed alarm export file.

    A ``ValueError`` subclass like ``core.profile.ProfileError``, so the
    GUI can answer 400 and a management command can re-raise it as a
    ``CommandError`` without either knowing about the other.
    """


class _AlarmDumper(yaml.SafeDumper):
    """SafeDumper that writes multi-line strings as literal blocks.

    An alarm comment is the field that runs to several lines. Without this
    it comes back as one quoted line full of ``\\n`` escapes, which makes
    an exported file unreadable and undiffable — the same reason
    ``core/profile.py`` carries its own dumper.
    """


def _represent_str(dumper: yaml.Dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data,
                                       style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_AlarmDumper.add_representer(str, _represent_str)


def _clean(value: Any) -> Any:
    """Normalise a DB value for YAML output."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _prune(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop keys that carry nothing.

    ``False`` and ``0`` are kept: ``enabled: false`` is a configured state,
    not an absent one. Only ``None``, ``""`` and empty containers go.
    """
    out = {}
    for key, value in mapping.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        out[key] = value
    return out


def dump_alarms(entries: Iterable[Mapping[str, Any]],
                *, exported_at: str = "") -> str:
    """Serialise alarms, grouped by datasource, to export YAML.

    ``entries`` is one mapping per datasource, each with ``datasource`` (the
    *name*, never the id) and ``alarms`` (a list of rows whose recipient
    lists have **already** been translated to usernames — this module knows
    nothing about ``auth_user``, which is Django-managed and off limits to
    ``core/``).

    A datasource with no alarms is still written, so a restore can tell
    "this datasource had none" from "this datasource was not exported".
    """
    doc_datasources = []
    for entry in entries:
        name = entry.get("datasource")
        if not name:
            raise AlarmIOError("every entry needs a datasource name")
        alarms = []
        for alarm in entry.get("alarms") or ():
            row = {key: _clean(alarm.get(key)) for key in ALARM_KEYS}
            if row.get("rule_kind") == "pipeline":
                # A pipeline rule watches ETL state, not fact rows, so its
                # measure, window_s and context are the column defaults and
                # mean nothing. Writing them out would make the row
                # indistinguishable from a threshold alarm that had been
                # mis-saved — the same trap the CRUD's field parser avoids
                # by skipping those branches entirely.
                for dead in ("measure", "window_s", "context_filter"):
                    row.pop(dead, None)
            else:
                labels = alarm.get("context_labels")
                if labels:
                    row["context_labels"] = labels
            alarms.append(_prune(row))
        doc_datasources.append({"datasource": name, "alarms": alarms})

    doc: dict[str, Any] = {"format": FORMAT}
    if exported_at:
        doc["exported_at"] = exported_at
    doc["datasources"] = doc_datasources
    return yaml.dump(doc, Dumper=_AlarmDumper, sort_keys=False,
                     allow_unicode=True, default_flow_style=False)


def parse_alarms_text(text: str) -> list[dict]:
    """Parse export YAML → ``[{datasource, alarms: [...]}, …]``.

    Checks the document's *shape* only — the header, the nesting, and that
    each value is of a type the field could hold. Whether a ``cond_op`` is
    one of five operators, or a ``window_s`` positive, is decided by
    ``sources/alarm_fields.parse_alarm_fields``, which is also what a form
    save goes through.

    Raises ``AlarmIOError`` with a message meant for a user.
    """
    if len(text.encode("utf-8")) > MAX_ALARM_FILE_BYTES:
        raise AlarmIOError(
            f"alarm file is larger than "
            f"{MAX_ALARM_FILE_BYTES // (1024 * 1024)} MB")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AlarmIOError(f"malformed YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise AlarmIOError("the top level must be a mapping")

    fmt = raw.get("format")
    if fmt != FORMAT:
        raise AlarmIOError(
            f"not an alarm export: expected format {FORMAT!r}, "
            f"found {fmt!r}"
            if fmt else
            f"not an alarm export: no 'format: {FORMAT}' header")

    datasources = raw.get("datasources")
    if not isinstance(datasources, list) or not datasources:
        raise AlarmIOError("'datasources' must be a non-empty list")

    out: list[dict] = []
    seen: set[str] = set()
    for idx, entry in enumerate(datasources, 1):
        if not isinstance(entry, dict):
            raise AlarmIOError(f"datasources[{idx}] must be a mapping")
        name = entry.get("datasource")
        if not isinstance(name, str) or not name.strip():
            raise AlarmIOError(
                f"datasources[{idx}] needs a 'datasource' name")
        name = name.strip()
        if name in seen:
            # Two blocks for one datasource would make the upsert order
            # decide the outcome, which is not something a file should
            # leave to chance.
            raise AlarmIOError(f"datasource {name!r} appears twice")
        seen.add(name)

        alarms_raw = entry.get("alarms")
        if alarms_raw is None:
            alarms_raw = []
        if not isinstance(alarms_raw, list):
            raise AlarmIOError(f"{name}: 'alarms' must be a list")

        alarms: list[dict] = []
        names: set[str] = set()
        for aidx, alarm in enumerate(alarms_raw, 1):
            where = f"{name}: alarms[{aidx}]"
            if not isinstance(alarm, dict):
                raise AlarmIOError(f"{where} must be a mapping")
            alarm_name = alarm.get("name")
            if not isinstance(alarm_name, str) or not alarm_name.strip():
                raise AlarmIOError(f"{where} needs a 'name'")
            alarm_name = alarm_name.strip()
            if alarm_name in names:
                # UNIQUE (datasource_id, name) in the DB; catching it here
                # names the file's problem instead of an integrity error.
                raise AlarmIOError(
                    f"{name}: two alarms are both named {alarm_name!r}")
            names.add(alarm_name)
            alarms.append(_check_alarm_shape(where, alarm_name, alarm))
        out.append({"datasource": name, "alarms": alarms})
    return out


def _check_alarm_shape(where: str, name: str, alarm: Mapping) -> dict:
    """Type-check one alarm mapping and return it normalised."""
    row: dict[str, Any] = {"name": name}

    for key in ("rule_kind", "condition", "measure", "cond_op", "comment",
                "severity"):
        value = alarm.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise AlarmIOError(f"{where}: {key} must be a string")
        row[key] = value

    for key in ("threshold", "window_s"):
        value = alarm.get(key)
        if value is None:
            continue
        # bool is an int subclass, and `threshold: true` is a typo, not a 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AlarmIOError(f"{where}: {key} must be a number")
        row[key] = value

    enabled = alarm.get("enabled")
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise AlarmIOError(f"{where}: enabled must be true or false")
        row["enabled"] = enabled

    channels = alarm.get("notify_channels")
    if channels is not None:
        if (not isinstance(channels, list)
                or not all(isinstance(c, str) for c in channels)):
            raise AlarmIOError(
                f"{where}: notify_channels must be a list of strings")
        row["notify_channels"] = channels

    for key in ("notify_email_recipients", "notify_xmpp_recipients"):
        people = alarm.get(key)
        if people is None:
            continue
        if (not isinstance(people, list)
                or not all(isinstance(p, str) for p in people)):
            # Usernames, not ids: an id means nothing on another
            # installation, and even on this one it is not what a human
            # reads in a backup file.
            raise AlarmIOError(
                f"{where}: {key} must be a list of usernames")
        row[key] = [p.strip() for p in people if p.strip()]

    context = alarm.get("context_filter")
    if context is not None:
        if not isinstance(context, list):
            raise AlarmIOError(
                f"{where}: context_filter must be a list of filter rows")
        # The row-level shape ({col, op, vals, …}) is checked by
        # sources/alarm_fields.parse_context_field, which is the same check
        # the chart's Alert form goes through.
        row["context_filter"] = context

    labels = alarm.get("context_labels")
    if labels is not None:
        if not isinstance(labels, dict):
            raise AlarmIOError(
                f"{where}: context_labels must be a mapping")
        row["context_labels"] = labels

    return row
