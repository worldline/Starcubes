"""Remove a datasource's rollup tables and forget their build state.

    manage.py rollup_drop --name payments
    manage.py rollup_drop --name payments --confirm payments

Safe wherever `fact` still covers the same span: the rollup is purely additive
in the read path, so without a usable state `pick_rollup` returns None and
every chart reads `fact` again — dropping costs speed and nothing else, and
`manage.py rollup_build --backfill` reproduces the rows.

**Not** safe below `datasource.retention_floor`. Retention deliberately leaves
the rollups alone, so down there they are the only copy of those counts and no
rebuild can bring them back. The command refuses that span and names it;
`--force` overrides.

Also removes tables left over from an earlier column set, which the current
configuration no longer names.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from core.rollup import (drop_rollup_tables, read_rollup_states,
                         unreconstructable_span)
from core.source import Source, _get_cached_engine


class Command(BaseCommand):
    help = "Drop the pre-aggregation tables of a datasource."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None)
        parser.add_argument("--id", type=int, default=None)
        parser.add_argument("--confirm", default=None,
                            help="datasource name, to skip the prompt")
        parser.add_argument("--force", action="store_true",
                            help="drop even the span below the retention "
                                 "floor, which no rebuild can restore")
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

        # Below the retention floor the rollup is not a cache but the only
        # copy: `fact` no longer holds those rows, so no rebuild can bring
        # the buckets back. Refuse rather than ask — a prompt invites a
        # reflexive "yes" for something that reads as routine.
        span = unreconstructable_span(source, read_rollup_states(meta, source.id))
        if span and not options["force"]:
            oldest, floor = span
            self.stderr.write(
                f"{source.name}: refusing to drop.\n"
                f"  The rollups hold {oldest:%Y-%m-%d %H:%M} .. "
                f"{floor:%Y-%m-%d %H:%M}, which is below the retention floor "
                f"— fact no longer has those rows, so this span cannot be "
                f"rebuilt.\n"
                f"  Pass --force if you mean to discard it permanently.")
            raise SystemExit(1)

        # Cheap to undo, but it can be a long rebuild — worth a confirmation
        # when nobody passed one on the command line.
        if options["confirm"] != source.name:
            answer = input(
                f"Drop the rollup tables of {source.name!r}? "
                f"Charts fall back to fact and a rebuild reads all of "
                f"{source.schema_name}.fact again. "
                f"Type the datasource name to confirm: ")
            if answer.strip() != source.name:
                self.stdout.write("aborted")
                return

        dropped = drop_rollup_tables(source, source.dest_engine(), meta)
        self.stdout.write(
            f"{source.name}: dropped {len(dropped)} rollup table(s): "
            f"{', '.join(dropped) or '—'}")
        self.stdout.write(
            "  the configuration is untouched — `manage.py rollup_build "
            f"--name {source.name} --backfill` rebuilds them"
            + (" (down to the retention floor only; the span below it is gone)"
               if span else ""))
