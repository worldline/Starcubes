"""Writing a loaded profile into ``starcubes_meta``.

Both consumers of a profile — ``manage.py seed_example`` (CLI) and the
``/sources/`` GUI (create form and Apply Profile) — need the same INSERT
statements. They used to carry a copy each, held together by a comment
saying "the SQL literal list mirrors the other so the two paths cannot
drift". This module is that guarantee in code instead.

Everything here takes an open SQLAlchemy connection so the caller decides
the transaction boundary: the create form writes the datasource row and its
children in one transaction, and Apply Profile replaces every child row in
one transaction.

Reading goes the other way — see ``core.profile.dump_profile``.
"""
from __future__ import annotations

from sqlalchemy import text

from core.query import slugify_group_set
from core.schema import validate_schema_name

# Child tables a profile owns in full. ``dim_group_set`` and below hang off
# ``dimension`` by ON DELETE CASCADE, so deleting dimensions clears them.
_CHILD_TABLES = ("fact_column", "dimension", "fact_index", "measure")


def _executemany(conn, sql: str, params: list[dict]) -> None:
    """``conn.execute`` for a batch, tolerating an empty batch.

    SQLAlchemy raises ``InvalidRequestError: A value is required for bind
    parameter`` when handed an empty parameter list, because it has no row
    to infer the bind values from. Empty is normal here: a profile exported
    from a datasource with no indices, or no measures yet, has empty
    sections.
    """
    if params:
        conn.execute(text(sql), params)


def delete_profile_children(conn, ds_id: int) -> None:
    """Remove every child row a profile owns, ready for a fresh insert."""
    for table in _CHILD_TABLES:
        conn.execute(text(f"DELETE FROM starcubes_meta.{table} "
                          f" WHERE datasource_id = :ds"), {"ds": ds_id})


def insert_profile_children(conn, ds_id: int, profile) -> None:
    """Insert a profile's child rows.

    Writes ``fact_column``, ``dimension``, ``fact_index``, ``measure`` and
    the ``dim_group_set`` tree. Stops short of ``create_fact`` and
    ``import_dimensions`` — those are separate, explicitly triggered steps.

    Deliberately does **not** touch the ``datasource`` row; the callers
    differ there (the create form stores what the user sees in the prefilled
    form, Apply Profile writes ``profile.datasource_scalars()``).
    """
    _executemany(conn,
        "INSERT INTO starcubes_meta.fact_column "
        "  (datasource_id, name, pg_type, role, ordinal, source_expr) "
        "VALUES (:ds, :n, :t, :r, :o, :e)",
        [{"ds": ds_id, "n": n, "t": t, "r": r, "o": o, "e": e}
         for (n, t, r, o, e) in profile.fact_columns_as_tuples()])
    _executemany(conn,
        "INSERT INTO starcubes_meta.dimension "
        "  (datasource_id, fact_column, dim_table, source_table, "
        "   source_key, source_name, source_description, is_primary, "
        "   inline_values, filter_group) "
        "VALUES (:ds, :fc, :dt, :st, :sk, :sn, :sd, :p, :iv, :fg)",
        [{"ds": ds_id, "fc": fc, "dt": dt, "st": st,
          "sk": sk, "sn": sn, "sd": sd, "p": p, "iv": iv, "fg": fg}
         for (fc, dt, st, sk, sn, sd, p, iv, fg) in profile.dimensions_as_tuples()])
    _executemany(conn,
        "INSERT INTO starcubes_meta.fact_index "
        "  (datasource_id, column_name, method) "
        "VALUES (:ds, :c, :m)",
        [{"ds": ds_id, "c": c, "m": m}
         for (c, m) in profile.fact_indices_as_tuples()])
    _executemany(conn,
        "INSERT INTO starcubes_meta.measure "
        "  (datasource_id, name, agg, graph_style, is_default, ordinal, percentiles) "
        "VALUES (:ds, :n, :a, :gs, :d, :o, :p)",
        [{"ds": ds_id, "n": n, "a": a, "gs": gs, "d": d, "o": o, "p": p}
         for (n, a, gs, d, o, p) in profile.measures_as_tuples()])
    insert_profile_group_dims(conn, ds_id, profile)


def insert_profile_group_dims(conn, ds_id: int, profile) -> None:
    """Recreate the profile's group dimensions.

    A profile identifies the carrying dimension by ``fact_column`` (it has
    no row ids), so this resolves those against the dimensions just
    inserted. A set naming an unknown fact column is skipped rather than
    failing the whole write.
    """
    if not profile.group_dims:
        return
    dim_ids = {r["fact_column"]: r["id"] for r in conn.execute(text(
        "SELECT id, fact_column FROM starcubes_meta.dimension "
        " WHERE datasource_id = :ds"), {"ds": ds_id}).mappings()}
    for gs in profile.group_dims:
        dim_id = dim_ids.get(gs.dimension)
        if dim_id is None:
            continue
        set_id = conn.execute(text(
            "INSERT INTO starcubes_meta.dim_group_set "
            "  (dimension_id, name, slug, position) "
            "VALUES (:d, :n, :s, :p) RETURNING id"
        ), {"d": dim_id, "n": gs.name,
            "s": gs.slug or slugify_group_set(gs.name),
            "p": gs.position}).scalar()
        for group in gs.groups:
            group_id = conn.execute(text(
                "INSERT INTO starcubes_meta.dim_group (group_set_id, group_name, color) "
                "VALUES (:s, :n, :c) RETURNING id"
            ), {"s": set_id, "n": group.name, "c": group.color}).scalar()
            if group.members:
                conn.execute(text(
                    "INSERT INTO starcubes_meta.dim_group_member (group_id, dim_index) "
                    "VALUES (:g, :i)"
                ), [{"g": group_id, "i": i} for i in group.members])


def update_datasource_scalars(conn, ds_id: int, profile) -> None:
    """Write the profile's ``datasource`` column values onto an existing row.

    Name, schema and DSN are never in a profile, so they survive untouched —
    applying a configuration must not rename or re-credential a live
    datasource.
    """
    scalars = profile.datasource_scalars()
    if not scalars:
        return
    assignments, params = [], {"id": ds_id}
    for i, (field_name, value) in enumerate(scalars.items()):
        param = f"v{i}"
        if field_name == "interval_ladder":
            # ``:param::type`` conflicts with SQLAlchemy named params, so the
            # array cast has to be spelled with CAST(). Same trick as the
            # datasource INSERT in sources/views.py.
            assignments.append(f"{field_name} = CAST(:{param} AS int[])")
            params[param] = "{" + ",".join(str(x) for x in value) + "}"
        else:
            assignments.append(f"{field_name} = :{param}")
            params[param] = value
    conn.execute(text(f"UPDATE starcubes_meta.datasource "
                      f"   SET {', '.join(assignments)} WHERE id = :id"), params)


def insert_datasource(conn, profile, name: str, schema: str,
                      source_dsn: str = "") -> int:
    """Create a datasource row from a profile and return its id.

    The CLI seed path: identity comes from the caller, everything else from
    the profile. The GUI create form does not use this — there the user may
    edit any prefilled value before submitting, so the form is the source of
    truth and only the child rows come from the profile.
    """
    # Interpolated unquoted into the warehouse DDL downstream, so it has to
    # be case-fold stable — fail here rather than at create_fact time.
    validate_schema_name(schema)

    # Become the default source only when no other source claims it (the
    # partial unique index ux_datasource_default allows just one).
    has_default = conn.execute(text(
        "SELECT 1 FROM starcubes_meta.datasource WHERE is_default LIMIT 1"
    )).scalar()

    scalars = profile.datasource_scalars()
    columns = ["name", "schema_name", "source_dsn", "is_default"]
    values = [":name", ":schema", ":dsn", ":is_def"]
    params: dict = {"name": name, "schema": schema, "dsn": source_dsn or "",
                    "is_def": not has_default}
    for i, (field_name, value) in enumerate(scalars.items()):
        param = f"v{i}"
        columns.append(field_name)
        if field_name == "interval_ladder":
            values.append(f"CAST(:{param} AS int[])")
            params[param] = "{" + ",".join(str(x) for x in value) + "}"
        else:
            values.append(f":{param}")
            params[param] = value
    return conn.execute(text(
        f"INSERT INTO starcubes_meta.datasource ({', '.join(columns)}) "
        f"VALUES ({', '.join(values)}) RETURNING id"), params).scalar()


# The pipeline alarms a datasource gets on the day it is created. Two, and
# deliberately these two: "the ETL is failing" and "the ETL is not running"
# are the questions an operator would have asked for on day one and never
# does, because setting up monitoring is always tomorrow's job.
#
# They are created *enabled* but with no channels, which is the whole point
# of the compromise: they open and close episodes in the Firings tab from
# the first load onwards, so the feature is discoverable and the history is
# real, and they send nothing at all until somebody picks recipients. A
# default that mails strangers would be worse than no default.
#
# 3600 s for "no run completed" is a guess, not a measurement — the honest
# number is a small multiple of the datasource's forever interval, which
# does not exist yet at creation time. It is on the rule for anyone to
# change, which is more than the nothing that was there before.
_DEFAULT_PIPELINE_ALARMS = (
    ("ETL runs are failing", "run_failed", ">=", 1.0),
    ("No ETL run is completing", "no_recent_run", ">", 3600.0),
)


def insert_default_pipeline_alarms(conn, datasource_id: int) -> list[str]:
    """Give a fresh datasource its two pipeline alarms. Returns their names.

    Shared by the CLI seed path and the GUI create form for the same reason
    the rest of this module is: the two must not drift, and a datasource
    that gets monitoring only when it was made one particular way is worse
    than one that never gets it, because nobody knows which kind they have.

    ``ON CONFLICT DO NOTHING`` on the (datasource_id, name) unique index, so
    re-seeding an existing datasource is a no-op rather than a 409.
    """
    created = []
    for name, condition, cond_op, threshold in _DEFAULT_PIPELINE_ALARMS:
        done = conn.execute(text(
            "INSERT INTO starcubes_meta.alarm "
            "  (datasource_id, name, rule_kind, condition, cond_op, "
            "   threshold, notify_channels, enabled) "
            "VALUES (:ds, :n, 'pipeline', :c, :op, :t, '{}', true) "
            "ON CONFLICT (datasource_id, name) DO NOTHING RETURNING id"
        ), {"ds": datasource_id, "n": name, "c": condition,
            "op": cond_op, "t": threshold}).scalar()
        if done is not None:
            created.append(name)
    return created
