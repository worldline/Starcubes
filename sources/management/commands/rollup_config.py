"""Set a datasource's rollup configuration and create the tables.

    manage.py rollup_config --name payments --interval 3600 \\
        --columns response,txn_type,card_type --marginals merch_cat \\
        --measures 'sum(amount),avg(stay)'
    manage.py rollup_config --name payments --show
    manage.py rollup_config --name payments --off

Writes `datasource.rollup_*`, creates the tables and their partitions, and
records the spec hash. It does *not* fill them — run `manage.py rollup_build
--backfill` for that.

Changing the configuration is safe at any time: the new spec hash no longer
matches what the existing rows were built under, so `pick_rollup` stops using
them and every chart goes back to `fact` until the rebuild finishes. Slower,
never wrong.

Run `manage.py rollup_advise` first for the numbers behind the column choice.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text

from core.rollup import (ensure_rollup_tables, read_rollup_states,
                         rollup_specs, spec_hash, validate_config,
                         write_rollup_state)
from core.source import Source, _get_cached_engine


def _split(value: str | None) -> list[str]:
    """Comma-separated list, tolerating spaces and a trailing comma."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


class Command(BaseCommand):
    help = ("Configure the pre-aggregation for a datasource and create its "
            "rollup tables.")

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None, help="datasource name")
        parser.add_argument("--id", type=int, default=None)
        parser.add_argument("--interval", type=int, default=None,
                            help="bucket width in seconds, e.g. 3600")
        parser.add_argument("--columns", default=None,
                            help="comma-separated dimension columns for the "
                                 "core rollup")
        parser.add_argument("--marginals", default=None,
                            help="comma-separated columns that each get a "
                                 "single-column rollup instead")
        parser.add_argument("--measures", default=None,
                            help="comma-separated agg forms beside count(*), "
                                 "e.g. 'sum(amount),avg(stay)'")
        parser.add_argument("--off", action="store_true",
                            help="turn the pre-aggregation off (keeps the "
                                 "tables; use rollup_drop to remove them)")
        parser.add_argument("--show", action="store_true",
                            help="print the current configuration and state, "
                                 "change nothing")
        parser.add_argument("--postgres-dsn", default=None)

    def handle(self, *args, **options):
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write(
                "Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            raise SystemExit(1)
        name_or_id = options["id"] or options["name"]
        if not name_or_id:
            self.stderr.write("Error: --name or --id is required")
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)
        source = Source.load(meta, dest_dsn=dsn, name_or_id=name_or_id)

        if options["show"]:
            self._show(meta, source)
            return

        if options["off"]:
            with meta.begin() as conn:
                conn.execute(text(
                    "UPDATE starcubes_meta.datasource "
                    "   SET rollup_interval_s = 0 WHERE id = :id"
                ), {"id": source.id})
            self.stdout.write(
                f"{source.name}: pre-aggregation off. The tables and their "
                f"contents are untouched — `rollup_config --interval …` with "
                f"the same column set brings them back without a rebuild.")
            return

        # Merge: an unspecified argument keeps its current value, so an
        # operator can change one thing without restating the rest.
        interval = (options["interval"] if options["interval"] is not None
                    else source.rollup_interval_s)
        columns = (_split(options["columns"]) if options["columns"] is not None
                   else list(source.rollup_columns))
        marginals = (_split(options["marginals"])
                     if options["marginals"] is not None
                     else list(source.rollup_marginals))
        measures = (_split(options["measures"])
                    if options["measures"] is not None
                    else list(source.rollup_measures))

        if not interval:
            self.stderr.write(
                "Error: --interval is required (seconds per bucket, e.g. 3600)")
            raise SystemExit(1)
        if not columns and not marginals:
            self.stderr.write(
                "Error: --columns or --marginals must name at least one "
                "dimension column")
            raise SystemExit(1)

        # Validate against a copy so a rejected configuration never reaches
        # the database and the live Source keeps describing what is deployed.
        probe = Source.load(meta, dest_dsn=dsn, name_or_id=source.id)
        probe.rollup_interval_s = interval
        probe.rollup_columns = columns
        probe.rollup_marginals = marginals
        probe.rollup_measures = measures
        problems = validate_config(probe)
        if problems:
            self.stderr.write(f"{source.name}: rollup configuration rejected:")
            for p in problems:
                self.stderr.write(f"  - {p}")
            raise SystemExit(1)

        # What is about to become unusable, so the operator hears it from the
        # command rather than from a chart that got slower.
        old_states = read_rollup_states(meta, source.id)
        invalidated = []
        for spec in rollup_specs(probe):
            st = old_states.get(spec.name)
            if st and st.spec_hash != spec_hash(spec) and st.built_through:
                invalidated.append(spec.name)
        orphaned = [n for n in old_states
                    if n not in {s.name for s in rollup_specs(probe)}]

        with meta.begin() as conn:
            conn.execute(text(
                "UPDATE starcubes_meta.datasource "
                "   SET rollup_interval_s = :iv, rollup_columns = :c, "
                "       rollup_marginals = :mg, rollup_measures = :ms "
                " WHERE id = :id"
            ), {"iv": interval, "c": columns, "mg": marginals,
                "ms": measures, "id": source.id})

        source = Source.load(meta, dest_dsn=dsn, name_or_id=source.id)
        created = ensure_rollup_tables(source, source.dest_engine())
        for spec in rollup_specs(source):
            st = old_states.get(spec.name)
            if st is None or st.spec_hash != spec_hash(spec):
                # New shape: no rows are valid for it yet, so the bounds start
                # empty and pick_rollup keeps its hands off until a build runs.
                write_rollup_state(meta, source.id, spec,
                                   built_from=None, built_through=None)

        self.stdout.write(f"{source.name}: rollup configured")
        self.stdout.write(f"  interval   {interval}s")
        self.stdout.write(f"  columns    {', '.join(columns) or '—'}")
        self.stdout.write(f"  marginals  {', '.join(marginals) or '—'}")
        self.stdout.write("  measures   count(*)"
                          + (", " + ", ".join(measures) if measures else ""))
        self.stdout.write(f"  tables     {', '.join(created) or '—'}")
        if invalidated:
            self.stdout.write(self.style.WARNING(
                f"  {len(invalidated)} rollup(s) no longer match their stored "
                f"rows and are now ignored: {', '.join(invalidated)}"))
        if orphaned:
            self.stdout.write(self.style.WARNING(
                f"  {len(orphaned)} table(s) left over from an earlier "
                f"configuration: {', '.join(orphaned)} — "
                f"`manage.py rollup_drop` removes them"))
        self.stdout.write(
            "  next: manage.py rollup_build --name "
            f"{source.name} --backfill")

    def _show(self, meta, source: Source) -> None:
        w = self.stdout.write
        w(f"{source.name} ({source.schema_name})")
        if not source.rollup_interval_s:
            w("  pre-aggregation is off")
            return
        w(f"  interval   {source.rollup_interval_s}s")
        w(f"  columns    {', '.join(source.rollup_columns) or '—'}")
        w(f"  marginals  {', '.join(source.rollup_marginals) or '—'}")
        w("  measures   count(*)"
          + (", " + ", ".join(source.rollup_measures)
             if source.rollup_measures else ""))
        states = read_rollup_states(meta, source.id)
        w("")
        w("  table                     built from        built through     rows      status")
        for spec in rollup_specs(source):
            st = states.get(spec.name)
            if st is None:
                status, bf, bt, rows = "not built", "—", "—", 0
            elif st.spec_hash != spec_hash(spec):
                status = "STALE (config changed)"
                bf = f"{st.built_from:%Y-%m-%d %H:%M}" if st.built_from else "—"
                bt = f"{st.built_through:%Y-%m-%d %H:%M}" if st.built_through else "—"
                rows = st.rows_built
            elif st.built_through is None:
                status, bf, bt, rows = "empty", "—", "—", 0
            else:
                status = "usable"
                bf = f"{st.built_from:%Y-%m-%d %H:%M}" if st.built_from else "—"
                bt = f"{st.built_through:%Y-%m-%d %H:%M}"
                rows = st.rows_built
            w(f"  {spec.name:<24}  {bf:<16}  {bt:<16}  {rows:>8}  {status}")
