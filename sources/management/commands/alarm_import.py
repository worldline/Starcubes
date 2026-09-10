"""Restore alarm rules from an export file.

    manage.py alarm_import alarms.yaml              # dry run, writes nothing
    manage.py alarm_import alarms.yaml --confirm
    manage.py alarm_import alarms.yaml --confirm --name payments

A dry run by default, because a restore is the kind of thing you want to
read before it happens: it prints what would be created, changed, left
alone and skipped, and every warning. ``--confirm`` repeats the plan and
writes it in one transaction.

The import is an **upsert on (datasource_id, name)**, never a replace.
Rules the file does not name are left where they are, and an existing
rule keeps its ``eval_end`` and its firing history across the update —
see ``sources/alarm_store.py`` for why a replace would be the wrong shape
for a restore.

Take the file with ``manage.py alarm_export``.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.alarm_io import AlarmIOError, parse_alarms_text
from core.source import _get_cached_engine
from sources.alarm_store import AlarmImportError, import_alarms
from sources.management.commands.alarm_export import _resolve_datasource


class Command(BaseCommand):
    help = "Import alarm rules from a YAML file (dry run without --confirm)."

    def add_arguments(self, parser):
        parser.add_argument("file", help="the alarm export file to read")
        parser.add_argument("--confirm", action="store_true",
                            help="actually write; without it, a dry run")
        parser.add_argument("--name", default=None,
                            help="import into this datasource only")
        parser.add_argument("--id", type=int, default=None,
                            help="import into this datasource id only")
        parser.add_argument("--user", default="cli",
                            help="username recorded in alarm_history")

    def handle(self, *args, **opts):
        dsn = settings.STARCUBES_POSTGRES_DSN
        meta = _get_cached_engine(dsn)

        try:
            with open(opts["file"], encoding="utf-8") as fh:
                entries = parse_alarms_text(fh.read())
        except OSError as exc:
            raise CommandError(f"cannot read {opts['file']}: {exc}") from exc
        except AlarmIOError as exc:
            raise CommandError(str(exc)) from exc

        ds_id = None
        if opts["name"] or opts["id"] is not None:
            ds_id = _resolve_datasource(meta, opts["name"], opts["id"])

        try:
            report = import_alarms(meta, dsn, entries,
                                   username=opts["user"],
                                   datasource_id=ds_id,
                                   confirm=opts["confirm"])
        except AlarmImportError as exc:
            raise CommandError(str(exc)) from exc

        self._render(report)
        if report["errors"]:
            raise CommandError(
                f"{len(report['errors'])} rule(s) could not be read — "
                f"nothing was written")
        if not opts["confirm"]:
            self.stdout.write(self.style.WARNING(
                "dry run — nothing written. Repeat with --confirm."))

    def _render(self, report: dict) -> None:
        for section in report["datasources"]:
            self.stdout.write(f"\n{section['datasource']}:")
            for name in section["added"]:
                self.stdout.write(self.style.SUCCESS(f"  + {name}"))
            for change in section["changed"]:
                fields = ", ".join(sorted(change["deltas"]))
                self.stdout.write(f"  ~ {change['name']}  ({fields})")
            for name in section["unchanged"]:
                self.stdout.write(f"  = {name}")
            for skip in section["skipped"]:
                self.stdout.write(self.style.WARNING(
                    f"  ! {skip['name']} skipped: {skip['reason']}"))
            if not any((section["added"], section["changed"],
                        section["unchanged"], section["skipped"])):
                self.stdout.write("  (no alarms in the file)")

        for skipped in report["skipped_datasources"]:
            self.stdout.write(self.style.WARNING(
                f"\n{skipped['datasource']}: skipped — {skipped['reason']}"))
        for warning in report["warnings"]:
            self.stdout.write(self.style.WARNING(f"warning: {warning}"))
        for error in report["errors"]:
            self.stdout.write(self.style.ERROR(
                f"error: {error['alarm']}: {error['error']}"))
        if report["applied"]:
            written = sum(len(s["added"]) + len(s["changed"])
                          for s in report["datasources"])
            self.stdout.write(self.style.SUCCESS(
                f"\n{written} alarm(s) written."))
