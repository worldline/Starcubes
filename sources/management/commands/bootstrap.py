"""First-boot bootstrap: migrations, admin user (fresh install only), collectstatic.

Used by both the container entrypoint (``manage.py bootstrap``) and the dev
server (``manage.py runserver`` calls ``bootstrap --skip-collectstatic``).
Both paths share the same setup logic; only collectstatic is production-only.

On an existing database (schema already present), migrations still run
(idempotent) but the admin user is NOT created.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Bootstrap: run migrations, create default admin (fresh install only), collectstatic."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-collectstatic",
            action="store_true",
            help="Skip collectstatic (used by the dev server override).",
        )

    def handle(self, *args, **options):
        # 0. Force-apply the consolidated starcubes_meta.sql even on an
        #    existing installation. SourcesConfig.ready() (which fires
        #    during the migrate call below) only probes cheaply and skips
        #    the script once `datasource` exists, so this is the only path
        #    that lets an existing container pick up new idempotent
        #    ALTER TABLE ... ADD COLUMN IF NOT EXISTS blocks added to
        #    starcubes_meta.sql after first boot (e.g. etl_run.log_path).
        #    The return value is captured locally rather than read back from
        #    core.schema._freshly_bootstrapped below, because migrate()
        #    triggers ready() -> ensure_starcubes_meta() again (without
        #    force) and that second call would overwrite the module global
        #    with False now that `datasource` exists.
        from django.conf import settings

        from core.schema import ensure_starcubes_meta
        from core.source import _get_cached_engine

        dsn = settings.STARCUBES_POSTGRES_DSN
        freshly_bootstrapped = False
        if dsn:
            self.stdout.write("Applying starcubes_meta.sql...")
            freshly_bootstrapped = ensure_starcubes_meta(_get_cached_engine(dsn), force=True)

        # 1. Run Django migrations.
        self.stdout.write("Running migrations...")
        call_command("migrate", interactive=False, stdout=self.stdout)

        # 1b. Apply the consolidated script a second time. Some of its
        #     blocks guard on a Django-managed table that does not exist
        #     yet above (migration 0030's auth_user.xmpp_jid: bootstrap
        #     runs starcubes_meta.sql BEFORE migrate, so on a fresh
        #     install the guarded ALTER was a no-op). Re-running the
        #     script is its idempotency contract, so this costs one cheap
        #     pass and closes the gap.
        if dsn:
            self.stdout.write("Applying starcubes_meta.sql (post-migrate pass)...")
            ensure_starcubes_meta(_get_cached_engine(dsn), force=True)

        # 2. Create default admin user only on a fresh install.
        if freshly_bootstrapped:
            from django.contrib.auth import get_user_model

            User = get_user_model()
            User.objects.create_superuser("admin", "admin@localhost", "admin")
            self.stdout.write(self.style.SUCCESS("Created default admin user (admin/admin)."))
        else:
            self.stdout.write("Existing installation — skipping admin creation.")

        # 3. Collect static files (production only; skipped for dev server).
        if not options["skip_collectstatic"]:
            self.stdout.write("Collecting static files...")
            call_command("collectstatic", interactive=False, clear=True, stdout=self.stdout)