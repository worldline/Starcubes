"""First-boot bootstrap: migrations, admin user (fresh install only), collectstatic.

Used by both the container entrypoint (``manage.py bootstrap``) and the dev
server (``manage.py runserver`` calls ``bootstrap --skip-collectstatic``).
Both paths share the same setup logic; only collectstatic is production-only.

On an existing database (schema already present), migrations still run
(idempotent) but the admin user is NOT created.

Several containers may start against one database at the same moment, so
everything that writes to it runs under one session advisory lock
(``core.pglock.BOOTSTRAP_OBJID``): the second container waits, then finds an
installation that is no longer fresh and changes nothing. Postgres DDL is
not safe to race even with ``IF NOT EXISTS`` — two sessions can both pass the
check and one fails on a catalog unique index — and neither is Django's
``migrate``. collectstatic writes only to this container's filesystem and
stays outside the lock.
"""
import hashlib
import hmac

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# The app_config key holding a fingerprint of STARCUBES_SECRET_KEY. Not one of
# core/config.py's editable keys, so no GUI page lists or changes it.
SECRET_FINGERPRINT_KEY = "STARCUBES_SECRET_KEY_FINGERPRINT"


class Command(BaseCommand):
    help = "Bootstrap: run migrations, create default admin (fresh install only), collectstatic."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-collectstatic",
            action="store_true",
            help="Skip collectstatic (used by the dev server override).",
        )
        parser.add_argument(
            "--rotate-secret",
            action="store_true",
            help="Accept a changed STARCUBES_SECRET_KEY and record it as the "
                 "one every container must use. Existing sessions end.",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        dsn = settings.STARCUBES_POSTGRES_DSN
        if dsn:
            from core.pglock import BOOTSTRAP_OBJID, session_lock

            def _waiting():
                self.stdout.write("Another starcubes container is bootstrapping "
                                  "this database — waiting for it to finish...")
                self.stdout.flush()

            with session_lock(dsn, BOOTSTRAP_OBJID, on_wait=_waiting):
                self._bootstrap_database(dsn)
                self._check_secret_key(dsn, rotate=options["rotate_secret"])
        else:
            call_command("migrate", interactive=False, stdout=self.stdout)

        # 3. Collect static files (production only; skipped for dev server).
        if not options["skip_collectstatic"]:
            self.stdout.write("Collecting static files...")
            call_command("collectstatic", interactive=False, clear=True, stdout=self.stdout)

    def _check_secret_key(self, dsn: str, *, rotate: bool) -> None:
        """Refuse to start with a different STARCUBES_SECRET_KEY than the others.

        Every container sharing a database must sign sessions and CSRF tokens
        with the same key, or a request served by one container rejects what
        another issued — which shows up as sporadic logouts and 403s, the
        worst kind of symptom to trace back to a missing environment
        variable. The first start records an HMAC fingerprint of the key
        (never the key) in app_config; every later start compares.

        Skipped with DEBUG on: a developer's `runserver` against a database a
        container also uses keeps its dev key, as it always could, and a
        production image runs with STARCUBES_DEBUG=0.
        """
        from django.conf import settings
        from sqlalchemy import text

        from core.source import _get_cached_engine

        if settings.DEBUG:
            return

        fingerprint = hmac.new(settings.SECRET_KEY.encode("utf-8"),
                               b"starcubes secret-key fingerprint",
                               hashlib.sha256).hexdigest()[:32]
        with _get_cached_engine(dsn).begin() as conn:
            stored = conn.execute(text(
                "SELECT value FROM starcubes_meta.app_config WHERE key = :k"),
                {"k": SECRET_FINGERPRINT_KEY}).scalar()
            if stored == fingerprint:
                return
            if stored is not None and not rotate:
                raise CommandError(
                    "STARCUBES_SECRET_KEY differs from the key the other "
                    "containers on this database use. Every container must "
                    "share one key, or sessions and CSRF tokens issued by one "
                    "are rejected by another. Set the same key everywhere — "
                    "or, to change it deliberately, run `manage.py bootstrap "
                    "--rotate-secret` once with the new key (existing sessions "
                    "end) and restart the other containers with it.")
            conn.execute(text(
                "INSERT INTO starcubes_meta.app_config (key, value, updated_by) "
                "VALUES (:k, :v, 'bootstrap') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "    updated_at = now(), updated_by = EXCLUDED.updated_by"),
                {"k": SECRET_FINGERPRINT_KEY, "v": fingerprint})
        self.stdout.write("Recorded the STARCUBES_SECRET_KEY fingerprint"
                          + (" (rotated)." if stored is not None else "."))

    def _bootstrap_database(self, dsn: str) -> None:
        from core.schema import ensure_starcubes_meta, freshly_bootstrapped
        from core.source import _get_cached_engine

        # 0. Force-apply the consolidated starcubes_meta.sql even on an
        #    existing installation. SourcesConfig.ready() only probes cheaply
        #    and skips the script once `datasource` exists, so this is the
        #    only path that lets an existing container pick up new idempotent
        #    ALTER TABLE ... ADD COLUMN IF NOT EXISTS blocks added to
        #    starcubes_meta.sql after first boot (e.g. etl_run.log_path).
        self.stdout.write("Applying starcubes_meta.sql...")
        ensure_starcubes_meta(_get_cached_engine(dsn), force=True)

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
        self.stdout.write("Applying starcubes_meta.sql (post-migrate pass)...")
        ensure_starcubes_meta(_get_cached_engine(dsn), force=True)

        # 1c. Saved profiles moved from profiles/user/ into the database
        #     (migration 0045). An installation upgraded with files still in
        #     that directory, or in STARCUBES_PROFILE_DIR, gets them imported
        #     here — once, never over a row that exists, never deleting a
        #     file. Under the bootstrap lock, so two containers starting on
        #     the same volume cannot import the same file twice.
        from core.profile import import_legacy_profiles

        for line in import_legacy_profiles(_get_cached_engine(dsn)):
            self.stdout.write(line)

        # 2. Create default admin user only on a fresh install. Asked of the
        #    process, not of step 0: on a fresh database SourcesConfig.ready()
        #    has already created the tables during django.setup(), before
        #    this command ran, and step 0 then finds them present.
        if freshly_bootstrapped():
            from django.contrib.auth import get_user_model

            User = get_user_model()
            User.objects.create_superuser("admin", "admin@localhost", "admin")
            self.stdout.write(self.style.SUCCESS("Created default admin user (admin/admin)."))
        else:
            self.stdout.write("Existing installation — skipping admin creation.")
