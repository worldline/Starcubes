#!/usr/bin/env python
"""Django CLI entry point for the starcubes project."""
import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "starcubes.settings")

    # Auto-bootstrap before runserver (once per process).
    # Django 6.x uses reversed() in get_commands(), so overriding the
    # built-in runserver via a management command doesn't work —
    # django.contrib.staticfiles always wins.  Instead, intercept
    # here in manage.py.
    if len(sys.argv) > 1 and sys.argv[1] == "runserver" and not os.environ.get("STARCUBES_BOOTSTRAPPED"):
        os.environ["STARCUBES_BOOTSTRAPPED"] = "1"
        from django.core.management import execute_from_command_line

        execute_from_command_line(["manage.py", "bootstrap", "--skip-collectstatic"])

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
