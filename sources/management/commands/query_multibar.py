"""Produce multibar plot JSON for a datasource and print to stdout.

Equivalent to scripts/query_multibar.py, but usable via ``manage.py query_multibar``.

Filter syntax: --filter <col>=<v1>,<v2>,...  (is in, default)
               --filter <col>!=<v1>,<v2>,...  (is not in)
               --filter <col>~=<pat1>,<pat2>  (starts with)
               --filter <col>*=<pat1>,<pat2>  (contains)

Measure syntax: --measure <col>               (defaults to sum)
               --measure <func>(<col>)        (avg, min, max, sum)
"""
from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand

from core.query import timeseries_multibar
from core.source import Source, _get_cached_engine
from core.utils import _parse_dt


def _parse_filter(spec: str) -> tuple[str, list[str], str]:
    if "!=" in spec:
        col, vals = spec.split("!=", 1)
        return col.strip(), [v.strip() for v in vals.split(",") if v.strip()], "not_in"
    if "~=" in spec:
        col, vals = spec.split("~=", 1)
        return col.strip(), [v.strip() for v in vals.split(",") if v.strip()], "starts_with"
    if "*=" in spec:
        col, vals = spec.split("*=", 1)
        return col.strip(), [v.strip() for v in vals.split(",") if v.strip()], "contains"
    if "=" in spec:
        col, vals = spec.split("=", 1)
        return col.strip(), [v.strip() for v in vals.split(",") if v.strip()], "in"
    raise ValueError(
        f"--filter must be <col>=<v1>[,<v2>...] or <col>!=... or <col>~=... or <col>*=..., "
        f"got {spec!r}")


class Command(BaseCommand):
    help = "Produce multibar plot JSON for a datasource."

    def add_arguments(self, parser):
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")
        parser.add_argument("--name", required=True,
                            help="datasource name")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--group-by", required=True,
                            help="fact column to split into series")
        parser.add_argument("--from", dest="t_from", required=True, type=_parse_dt,
                            help="window start, ISO datetime")
        parser.add_argument("--to",   dest="t_to",   required=True, type=_parse_dt,
                            help="window end, ISO datetime")
        parser.add_argument("--interval", type=int, default=60,
                            help="seconds per slot (default: 60)")
        parser.add_argument("--filter", action="append", type=_parse_filter,
                            default=[], metavar="COL=V1,V2,...",
                            help="filter clause; use != for 'not_in', "
                                 "~= for 'starts_with', *= for 'contains'")
        parser.add_argument("--measure", default=None,
                            help="aggregation, e.g. 'stay', 'avg(stay)', 'min(stay)'")
        parser.add_argument("--series-label", default="source_id",
                            choices=["source_id", "name", "index"])
        parser.add_argument("--label", default=None,
                            help="optional `name` field for the plot legend")
        parser.add_argument("--indent", type=int, default=None,
                            help="pretty-print JSON with this indent")

    def handle(self, *args, **options):
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        filter_groups = []
        for col, vals, op in options["filter"]:
            filter_groups.append([{"col": col, "op": op, "vals": vals}])

        meta = _get_cached_engine(dsn)
        name_or_id = options["id"] if options["id"] else options["name"]
        source = Source.load(meta, dest_dsn=dsn, name_or_id=name_or_id)

        plot = timeseries_multibar(
            source,
            group_by=options["group_by"],
            t_from=options["t_from"],
            t_to=options["t_to"],
            interval=options["interval"],
            filter_groups=filter_groups or None,
            measure=options["measure"],
            name=options["label"],
            series_label=options["series_label"],
            meta_engine=meta,
        )

        json.dump(plot, self.stdout, indent=options["indent"], default=str)
        self.stdout.write("\n")