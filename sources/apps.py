from django.apps import AppConfig


class SourcesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sources"

    def ready(self):
        from django.conf import settings
        from core.schema import ensure_starcubes_meta
        from core.source import _get_cached_engine

        dsn = settings.STARCUBES_POSTGRES_DSN
        if dsn:
            ensure_starcubes_meta(_get_cached_engine(dsn))
            # DB-stored SSO config onto django.conf.settings, once per
            # process, before the URLconf mounts /oidc/. Exception-safe:
            # on a fresh install app_config may not exist yet (ready()
            # runs before migrate in the bootstrap process).
            from starcubes.oidc import apply_db_overrides
            apply_db_overrides()
