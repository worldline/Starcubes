"""Custom PostgreSQL database backend that ensures starcubes_meta schema exists.

Django tables (auth_user, django_session, etc.) land in starcubes_meta instead of
public via search_path. The schema is created on the first connection so that
manage.py migrate works against a fresh database without manual setup.
"""

from django.db.backends.postgresql.base import DatabaseWrapper as _Base


class DatabaseWrapper(_Base):
    _starcubes_meta_ensured = False

    def _ensure_starcubes_meta(self):
        if self._starcubes_meta_ensured:
            return
        with self.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS starcubes_meta")
        self._starcubes_meta_ensured = True

    def init_connection_state(self):
        super().init_connection_state()
        self._ensure_starcubes_meta()