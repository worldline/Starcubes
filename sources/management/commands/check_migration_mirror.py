"""Check that every per-step migration has its mirror block in the
consolidated ``starcubes_meta.sql`` — the contract that CLAUDE.md states in
prose and nothing verified so far.

Markers (``-- 00NN_name.sql`` header lines) only exist for the migrations
that were split out after the consolidation started (0025+); earlier ones
live inside the consolidated block without a marker. The baseline therefore
comes from the mirror's own header line ("Combines all previous migrations
(0001–NNNN combined …)") and the command only fails above it.
"""

import re

from django.core.management.base import BaseCommand, CommandError

from core.schema import MIGRATIONS_DIR


class Command(BaseCommand):
    help = ("Verify every per-step migration file has its mirror block in "
            "migrations/starcubes_meta.sql (see CLAUDE.md, verification).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet", action="store_true",
            help="suppress the per-file notes below the baseline")

    def handle(self, *args, **options):
        quiet = options["quiet"]
        mirror = (MIGRATIONS_DIR / "starcubes_meta.sql").read_text()
        markers = {name + ".sql" for name in re.findall(
            r"^--\s+(00\d+_[A-Za-z0-9_]+)\.sql", mirror, re.M)}
        baseline_match = re.search(
            r"Combines all previous migrations \(0001[–-](\d+)\)", mirror)
        if not baseline_match:
            raise CommandError(
                "starcubes_meta.sql has no 'Combines all previous migrations "
                "(0001–NNNN)' header — cannot derive the marker baseline")
        baseline = int(baseline_match.group(1))

        problems = []
        for path in sorted(MIGRATIONS_DIR.glob("0*.sql")):
            if path.name == "starcubes_meta.sql":
                continue
            if path.name in markers:
                continue
            if int(path.name[:4]) <= baseline:
                if not quiet:
                    self.stdout.write(
                        f"note: {path.name} is inside the consolidated block "
                        f"(<= {baseline}), no per-step marker expected")
                continue
            problems.append(path.name)

        if problems:
            raise CommandError(
                "no mirror block in migrations/starcubes_meta.sql for: "
                + ", ".join(problems))
        if not quiet:
            self.stdout.write(self.style.SUCCESS(
                f"mirror OK: every migration above {baseline:04d} has its "
                f"mirror block ({len(markers)} markers)"))