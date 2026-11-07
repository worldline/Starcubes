"""core/lease.py gegen PostgreSQL — Lease, Liveness, Stop-Requests."""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

from core.lease import (
    acquire_process_lease,
    application_name,
    current_lease,
    is_local,
    lease_state,
    reap_dead_runs,
    request_stop,
    verify_local_pid,
)


class TestLease:
    def test_lease_und_spalten(self, pg_dsn, meta_engine):
        lease = acquire_process_lease(pg_dsn)
        assert lease.check() is True
        assert current_lease() is lease

        cols = lease.columns()
        assert cols["lease_pid"] == lease.backend_pid
        assert cols["pid"] == os.getpid()
        assert cols["lease_start"] == lease.backend_start
        assert cols["instance_id"] == lease.instance_id
        assert cols["hostname"]

        assert application_name(os.getpid()).startswith("starcubes/")

    def test_lease_state_und_verify(self, pg_dsn, meta_engine):
        lease = acquire_process_lease(pg_dsn)
        with meta_engine.connect() as conn:
            assert lease_state(conn, lease.backend_pid,
                               lease.backend_start) is True
            assert lease_state(conn, None, None) is None
            assert lease_state(conn, lease.backend_pid + 1,
                               lease.backend_start) is False

            row = {
                "instance_id": lease.instance_id,
                "pid": os.getpid(),
                "lease_pid": lease.backend_pid,
                "lease_start": lease.backend_start,
            }
            assert is_local(row) is True
            assert verify_local_pid(conn, row) is True

            foreign = dict(row, instance_id="fremde-instanz")
            assert is_local(foreign) is False
            assert verify_local_pid(conn, foreign) is False

            broken = dict(row, lease_start=lease.backend_start
                          - timedelta(seconds=1))
            assert verify_local_pid(conn, broken) is False


class TestControl:
    def _run(self, meta_engine, ds):
        with meta_engine.begin() as conn:
            return conn.execute(text(
                "INSERT INTO starcubes_meta.etl_run "
                "  (datasource_id, started_at, status, kind) "
                "VALUES (:ds, now(), 'running', 'incremental') RETURNING id"),
                {"ds": ds}).scalar()

    def _control(self, meta_engine, run_id):
        with meta_engine.connect() as conn:
            return conn.execute(text(
                "SELECT control FROM starcubes_meta.etl_run WHERE id = :id"),
                {"id": run_id}).scalar()

    def test_term_und_kill(self, meta_engine, make_datasource):
        ds = make_datasource("t_lease_" + uuid.uuid4().hex[:8])
        run_id = self._run(meta_engine, ds)
        with meta_engine.begin() as conn:
            request_stop(conn, "etl_run", run_id, force=False,
                         instance="inst")
        assert self._control(meta_engine, run_id) == "term"

        with meta_engine.begin() as conn:
            request_stop(conn, "etl_run", run_id, force=True, instance="inst")
        assert self._control(meta_engine, run_id) == "kill"

        # Ein späteres term darf kill nicht herabstufen.
        with meta_engine.begin() as conn:
            request_stop(conn, "etl_run", run_id, force=False,
                         instance="inst")
        assert self._control(meta_engine, run_id) == "kill"

    def test_nicht_running_wird_nicht_angefasst(self, meta_engine,
                                                make_datasource):
        ds = make_datasource("t_lease_" + uuid.uuid4().hex[:8])
        run_id = self._run(meta_engine, ds)
        with meta_engine.begin() as conn:
            conn.execute(text(
                "UPDATE starcubes_meta.etl_run SET status = 'ok' "
                " WHERE id = :id"), {"id": run_id})
            request_stop(conn, "etl_run", run_id, force=True, instance="x")
        assert self._control(meta_engine, run_id) is None


class TestReap:
    def test_tote_lease_wird_geschlossen(self, meta_engine, make_datasource):
        ds = make_datasource("t_lease_" + uuid.uuid4().hex[:8])
        with meta_engine.begin() as conn:
            run_id = conn.execute(text(
                "INSERT INTO starcubes_meta.etl_run "
                "  (datasource_id, started_at, status, kind, "
                "   lease_pid, lease_start) "
                "VALUES (:ds, now(), 'running', 'incremental', "
                "        999999999, now()) RETURNING id"),
                {"ds": ds}).scalar()
        with meta_engine.begin() as conn:
            assert reap_dead_runs(conn, ds) >= 1
        with meta_engine.connect() as conn:
            status = conn.execute(text(
                "SELECT status FROM starcubes_meta.etl_run WHERE id = :id"),
                {"id": run_id}).scalar()
        assert status == "failed"
