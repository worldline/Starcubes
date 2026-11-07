"""Die vorhandenen Selbsttest-Commands als Regressionstests.

Sie sind bereits eigenständige Prüfungen; hier werden sie in die Suite
gezogen, damit ein Testlauf sie nicht vergisst. Alle vier arbeiten ohne
Datenbank.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command


@pytest.mark.parametrize("command", [
    "check_migration_mirror",
    "check_help_docs",
    "check_stream_stats",
    "check_logsource",
])
def test_check_command_laeuft_durch(command):
    # CommandError bei einer Abweichung; hier zählt: kein Fehler.
    call_command(command, verbosity=0)


def test_django_systemcheck():
    call_command("check", verbosity=0)
