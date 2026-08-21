"""Server-side cache for frozen plot_json responses.

Eligibility (caller-checked, see `is_cacheable`):
  * measure is count(*) (None or the literal string)
  * `to` is at a UTC interval boundary
  * `to` <= now() - interval (block does not include "now")
  * `to` <= last successful HWM minus late_arrival_overlap_s (data behind
    the cache is final — the overlap span can still take late arrivals)

The key includes the datasource's `cache_epoch`, so any operation that
mutates historical fact rows can invalidate the whole datasource's cache by
calling `bump_cache_epoch`.

No Django imports — this module is callable from `core/` and management
commands the same way `core/query.py` is.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from sqlalchemy import text


def make_cache_key(
    *,
    datasource_id: int,
    group_by: str,
    measure: Optional[str],
    t_from_epoch: int,
    t_to_epoch: int,
    interval: int,
    filter_tree: Optional[list],
    series_label: str,
    cache_epoch: int,
) -> str:
    """Build a stable sha256 hex key for a cacheable plot request.

    `t_from_epoch` is normalized to the server-side `interval_start`
    (`(t_from // interval) * interval`) so trivially-different `from` values
    that produce the same plot collide on the same key.
    """
    interval_start = (int(t_from_epoch) // interval) * interval
    payload = {
        "ds": int(datasource_id),
        "gb": group_by,
        "m":  measure or "count(*)",
        "f":  interval_start,
        "t":  int(t_to_epoch),
        "i":  int(interval),
        "ft": filter_tree or [],
        "sl": series_label,
        "ce": int(cache_epoch),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_cacheable(
    *,
    measure: Optional[str],
    interval: int,
    t_to_epoch: int,
    hwm_epoch: Optional[int],
    now_epoch: Optional[int] = None,
    late_arrival_overlap_s: int = 0,
) -> bool:
    """Return True if a request is eligible for the server-side cache.

    ``late_arrival_overlap_s`` pulls the cacheable horizon back below the
    high watermark: with an overlap configured, every incremental run may
    still insert late-arriving rows into that span, so a payload cached
    there would go stale without any invalidation event to catch it.

    ``watermark_lag_s`` / ``wallclock_lag_s`` deliberately do *not* appear
    here, and subtracting them too would be wrong. The overlap writes
    *behind* the HWM, which is what makes that span uncacheable; the lags
    do the opposite -- they hold the window's upper bound back so nothing
    lands behind the HWM in the first place. They make the watermark more
    honest, not less, so a second subtraction would only shorten the
    cacheable horizon for no reason.
    """
    if measure not in (None, "", "count(*)"):
        return False
    if hwm_epoch is None:
        return False
    if interval <= 0:
        return False
    if t_to_epoch % interval != 0:
        return False
    if now_epoch is None:
        now_epoch = int(time.time())
    if t_to_epoch > now_epoch - interval:
        return False
    if t_to_epoch > hwm_epoch - max(0, late_arrival_overlap_s):
        return False
    return True


def get_cached_plot(meta_engine, cache_key: str) -> Optional[dict]:
    """Return the cached payload (and bump LRU stats), or None on miss."""
    with meta_engine.begin() as conn:
        row = conn.execute(text(
            "UPDATE starcubes_meta.query_cache "
            "   SET last_hit_at = now(), hits = hits + 1 "
            " WHERE cache_key = :k "
            " RETURNING payload"
        ), {"k": cache_key}).scalar()
    return row  # psycopg2 returns json as a dict already


def put_cached_plot(
    meta_engine,
    *,
    cache_key: str,
    datasource_id: int,
    cache_epoch: int,
    group_by: str,
    measure: Optional[str],
    payload: dict,
) -> None:
    """Store a plot under `cache_key`. Overwrites on conflict.

    `group_by` and `measure` are stored alongside the hash so selective
    deletion (see `clear_cache`) can match by either or both. `measure`
    is normalized to ``'count(*)'`` when None/empty — same rule as
    `make_cache_key` uses when hashing.
    """
    blob = json.dumps(payload, default=str)
    measure_norm = measure or "count(*)"
    with meta_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO starcubes_meta.query_cache "
            "    (cache_key, datasource_id, cache_epoch, "
            "     group_by, measure, payload) "
            "VALUES (:k, :ds, :ce, :gb, :m, CAST(:p AS json)) "
            "ON CONFLICT (cache_key) DO UPDATE "
            "  SET payload     = EXCLUDED.payload, "
            "      cache_epoch = EXCLUDED.cache_epoch, "
            "      group_by    = EXCLUDED.group_by, "
            "      measure     = EXCLUDED.measure, "
            "      last_hit_at = now()"
        ), {"k": cache_key, "ds": datasource_id,
            "ce": cache_epoch, "gb": group_by,
            "m": measure_norm, "p": blob})


def bump_cache_epoch(meta_engine, datasource_id: int) -> int:
    """Increment cache_epoch on a datasource. Returns the new epoch."""
    with meta_engine.begin() as conn:
        return conn.execute(text(
            "UPDATE starcubes_meta.datasource "
            "   SET cache_epoch = cache_epoch + 1 "
            " WHERE id = :id "
            " RETURNING cache_epoch"
        ), {"id": datasource_id}).scalar()


def get_cache_epoch(meta_engine, datasource_id: int) -> int:
    """Read the current cache_epoch for a datasource."""
    with meta_engine.begin() as conn:
        v = conn.execute(text(
            "SELECT cache_epoch FROM starcubes_meta.datasource WHERE id = :id"
        ), {"id": datasource_id}).scalar()
    return int(v or 0)


def clear_cache(
    meta_engine,
    datasource_id: Optional[int] = None,
    *,
    group_by: Optional[str] = None,
    measure: Optional[str] = None,
) -> int:
    """Delete cache rows. Returns the number of rows deleted.

    Scopes:
      - ``datasource_id=None`` and no filters → delete the whole table.
      - ``datasource_id=N`` alone → delete every row for that datasource.
      - ``datasource_id=N`` + ``group_by`` and/or ``measure`` → delete
        only rows matching the given breakdown for that datasource.

    Passing ``group_by`` or ``measure`` without a datasource_id is
    rejected — finer-grained scopes are always anchored to one
    datasource so cross-source neighbours cannot be wiped by mistake.
    """
    if (group_by is not None or measure is not None) and datasource_id is None:
        raise ValueError(
            "group_by/measure filters require a datasource_id")

    where: list[str] = []
    params: dict = {}
    if datasource_id is not None:
        where.append("datasource_id = :id")
        params["id"] = datasource_id
    if group_by is not None:
        where.append("group_by = :gb")
        params["gb"] = group_by
    if measure is not None:
        where.append("measure = :m")
        params["m"] = measure

    sql = "DELETE FROM starcubes_meta.query_cache"
    if where:
        sql += " WHERE " + " AND ".join(where)

    with meta_engine.begin() as conn:
        res = conn.execute(text(sql), params)
    return res.rowcount or 0
