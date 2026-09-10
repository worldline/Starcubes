"""Write a datasource's alarms — or every alarm — to an export file.

    manage.py alarm_export --name payments --out alarms.yaml
    manage.py alarm_export --all --out alarms-all.yaml
    manage.py alarm_export --all                   # to stdout

The file is the unit a backup is taken in. It carries configuration only:
no ids, no ``eval_end``, no firing history — see ``core/alarm_io.py`` for
what travels and why. Recipients are written as usernames, because an
``auth_user.id`` means nothing outside the installation that issued it.

Restore with ``manage.py alarm_import``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from sqlalchemy import text

from core.alarm_io import dump_alarms
from core.source import _get_cached_engine
from sources.alarm_store import collect_alarms


class Command(BaseCommand):
    help = "Export alarm rules to a YAML file."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None,
                            help="datasource name to export")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id to export")
        parser.add_argument("--all", action="store_true",
                            help="export every datasource's alarms")
        parser.add_argument("--out", default=None,
                            help="file to write (default: stdout)")

    def handle(self, *args, **opts):
        dsn = settings.STARCUBES_POSTGRES_DSN
        meta = _get_cached_engine(dsn)

        if not opts["all"] and not opts["name"] and opts["id"] is None:
            raise CommandError("name one datasource with --name/--id, "
                               "or pass --all")
        ids = None
        if not opts["all"]:
            ids = [_resolve_datasource(meta, opts["name"], opts["id"])]

        entries = collect_alarms(meta, dsn, datasource_ids=ids)
        count = sum(len(e["alarms"]) for e in entries)
        text_out = dump_alarms(
            entries,
            exported_at=datetime.now(timezone.utc).replace(
                microsecond=0).isoformat())

        if opts["out"]:
            with open(opts["out"], "w", encoding="utf-8") as fh:
                fh.write(text_out)
            self.stdout.write(self.style.SUCCESS(
                f"{count} alarm(s) from {len(entries)} datasource(s) "
                f"→ {opts['out']}"))
        else:
            self.stdout.write(text_out)


def _resolve_datasource(meta, name: str | None, ds_id: int | None) -> int:
    """Name or id → id, with a CommandError naming what was not found."""
    with meta.begin() as conn:
        if ds_id is not None:
            found = conn.execute(text(
                "SELECT id FROM starcubes_meta.datasource WHERE id = :i"),
                {"i": ds_id}).scalar()
            if found is None:
                raise CommandError(f"no datasource with id {ds_id}")
            return found
        found = conn.execute(text(
            "SELECT id FROM starcubes_meta.datasource WHERE name = :n"),
            {"n": name}).scalar()
        if found is None:
            raise CommandError(f"no datasource named {name!r}")
        return found
