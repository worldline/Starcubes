"""Ad-hoc read-only SQL execution for the chart's Query console.

Three concerns live here: validating a user-supplied statement, running it
against a datasource's warehouse schema under a read-only transaction, and
keeping a per-user history of what was run.

Read-only is enforced by four independent layers, deliberately redundant
because each one alone has a hole:

  1. `Source.console_engine()` connects with `-cdefault_transaction_read_only=on`
     as a libpq startup option, so it holds even if a caller skips the SETs below.
  2. `run_query` issues `SET TRANSACTION READ ONLY` as the first statement.
  3. `validate_sql` rejects multi-statement text and anything not starting with
     SELECT / WITH.
  4. `stream_results=True` makes psycopg2 use a named server-side cursor, and
     Postgres refuses `DECLARE CURSOR` over a data-modifying CTE — the one
     writing shape that gets past layer 3, since `WITH x AS (DELETE ...
     RETURNING 1) SELECT * FROM x` legitimately starts with WITH.

Measured, so that nobody has to re-derive it: a multi-statement string that
somehow bypassed layer 3 is *not* refused by the cursor (Postgres' simple
query protocol accepts `SELECT 1; SELECT 2`), but every write inside one is
still rejected by layers 1+2 with SQLSTATE 25006. Layer 3 remains the only
thing enforcing "one statement"; layers 1+2 remain the only thing enforcing
"no writes".

None of this is a substitute for a Postgres role with SELECT-only grants.
`default_transaction_read_only` blocks writes but not `pg_sleep()`,
`pg_read_file()`, or reading every table in the schema. If the console is ever
exposed to logins that are not trusted operators, the correct fix is a second
DSN pointing at a dedicated reader role.

Query cancellation is cross-process by design: the app runs under uWSGI with
`processes = 2`, so an in-memory `{token: pid}` map would frequently be
consulted in the wrong worker. Instead `run_query` tags its backend via
`application_name` and `cancel_query` finds it through `pg_stat_activity`,
which is authoritative, self-cleaning, and visible from every worker.

No Django imports — same constraint as `core/query.py` and `core/cache.py`.
"""
from __future__ import annotations

import hashlib
import re
import time

from sqlalchemy import text

#: Cap on the accepted statement length. Guards against a runaway paste
#: reaching the lexer and the history table.
MAX_SQL_LEN = 100_000

#: `application_name` is truncated to NAMEDATALEN-1 bytes by Postgres. A tag
#: longer than this would silently not match in `cancel_query`.
MAX_TAG_LEN = 63

_FIRST_KEYWORD = re.compile(r"(select|with)\b", re.I)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")


class SqlRejected(ValueError):
    """User SQL rejected before it ever reached the database."""


# ── Validation ──────────────────────────────────────────────────────────────

def _statement_end_positions(sql: str) -> list[int]:
    """Return the offsets of every top-level `;` in `sql`.

    A single left-to-right scan that skips over the four places a semicolon can
    legally appear without terminating a statement: single-quoted strings
    (including the `''` escape), double-quoted identifiers, dollar-quoted
    bodies, and line/block comments. Postgres nests block comments, so their
    depth is tracked rather than matched greedily.

    A naive `sql.split(';')` gets `SELECT 'a;b'` wrong, which is why this
    exists.
    """
    out: list[int] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                if sql[i] == "\\" and i + 1 < n:
                    # Covers E'...\'...' escape strings. Harmless for standard
                    # strings, where a backslash is an ordinary character.
                    i += 2
                    continue
                i += 1
            continue
        if c == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                i = n if end == -1 else end + len(tag)
                continue
            i += 1
            continue
        if c == "-" and sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if c == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if c == ";":
            out.append(i)
        i += 1
    return out


def _first_code_offset(sql: str) -> int:
    """Offset of the first character that is neither whitespace nor a comment.

    Deliberately a scanner and not a regex: Postgres nests block comments, and
    a regex for `/* ... */` stops at the first `*/`, so `/* a /* b */ c */ SELECT 1`
    would leave `c */ SELECT 1` behind and the keyword check would reject a
    perfectly valid query.
    """
    i, n = 0, len(sql)
    while i < n:
        if sql[i].isspace():
            i += 1
        elif sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        else:
            break
    return i


def validate_sql(sql: str) -> str:
    """Return the normalized single read-only statement, or raise `SqlRejected`.

    This is a lexer, not a parser, and it is *not* the security boundary. In
    particular `WITH x AS (DELETE FROM t RETURNING 1) SELECT * FROM x` passes
    the prefix test and is perfectly legal writing SQL — it is stopped by the
    read-only transaction and by `DECLARE CURSOR`, both in `run_query`, not
    here. The job of this function is a good error message and blocking the
    obvious `SELECT 1; DROP TABLE t`.

    It is, however, the *only* layer enforcing one-statement-per-run: the
    server-side cursor happily accepts `SELECT 1; SELECT 2`. Writes in such a
    string are still caught downstream (SQLSTATE 25006), so the failure mode
    of a hole here is extra reads, not mutation.
    """
    if sql is None:
        raise SqlRejected("no SQL given")
    sql = sql.strip()
    if not sql:
        raise SqlRejected("no SQL given")
    if len(sql) > MAX_SQL_LEN:
        raise SqlRejected(f"statement too long ({len(sql)} > {MAX_SQL_LEN} characters)")

    ends = _statement_end_positions(sql)
    for pos in ends:
        if sql[pos + 1:].strip():
            raise SqlRejected("only a single statement is allowed")
    if ends:
        sql = sql[:ends[-1]].rstrip()
        if not sql:
            raise SqlRejected("no SQL given")

    if not _FIRST_KEYWORD.match(sql, _first_code_offset(sql)):
        raise SqlRejected("only SELECT / WITH queries are allowed here")
    return sql


def sql_hash(sql: str) -> str:
    """sha256 hex of the normalized statement — the history dedup key."""
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


# ── Execution ───────────────────────────────────────────────────────────────

def make_tag(user_id: int, token: str) -> str:
    """Build the `application_name` tag identifying one running console query.

    Short on purpose: Postgres truncates `application_name` at 63 bytes and a
    truncated tag would never match in `cancel_query`. The user id is baked in
    by the caller from `request.user.id`, never from the request body, so a
    guessed token cannot cancel somebody else's query.
    """
    return f"sq:{int(user_id)}:{token}"


def _jsonable(v):
    """Make one result cell safe for `JsonResponse(..., default=str)`.

    Only binary needs help: `default=str` renders a memoryview as
    `<memory at 0x7f...>`, which tells the user nothing. Everything else —
    Decimal, datetime, UUID, jsonb — either serializes natively or becomes a
    faithful string, which is what the results table wants anyway.
    """
    if isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        return "\\x" + raw[:32].hex() + ("…" if len(raw) > 32 else "")
    return v


def run_query(console_engine, sql: str, *, tag: str,
              row_limit: int = 1000, timeout_ms: int = 30_000) -> dict:
    """Run one validated read-only statement and return at most `row_limit` rows.

    The statement order inside the transaction is load-bearing:

    * `SET TRANSACTION READ ONLY` must precede the first query of the
      transaction, and `SELECT set_config(...)` already counts as one — so it
      goes first.
    * `set_config(name, value, is_local => true)` rather than `SET LOCAL`,
      because `SET` takes no bind parameters (`SET LOCAL application_name = :tag`
      is a syntax error under the extended protocol). `is_local => true` gives
      exactly `SET LOCAL` semantics, so the tag reverts at transaction end and
      never leaks to the next user of this pooled connection.
    * `stream_results=True` gives a named server-side cursor. Without it libpq
      buffers the *entire* result before `fetchmany` returns, so
      `SELECT * FROM fact` would exhaust the worker's memory despite the row cap.
    * The user statement goes through `exec_driver_sql`, never `text()`, so
      colons inside it stay literal instead of being read as bind parameters.

    One consequence of the server-side cursor worth knowing: `statement_timeout`
    then bounds each individual `DECLARE` / `FETCH` rather than the cursor's
    whole lifetime. With exactly one `fetchmany` call that is effectively the
    same bound.
    """
    if len(tag) > MAX_TAG_LEN:
        raise ValueError(f"application_name tag too long: {len(tag)} > {MAX_TAG_LEN}")

    with console_engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                     {"ms": str(int(timeout_ms))})
        conn.execute(text("SELECT set_config('application_name', :tag, true)"),
                     {"tag": tag})
        started = time.perf_counter()
        # exec_driver_sql, not text(): text() parses `:name` as a bind
        # parameter, so an ordinary `SELECT '{"a":1}'::jsonb` from the console
        # would die with "A value is required for bind parameter '1'". The
        # driver-level call hands the string straight to psycopg2, which does
        # no interpolation of its own when no parameters are passed.
        result = conn.execution_options(stream_results=True).exec_driver_sql(sql)
        columns = [str(c) for c in result.keys()]
        rows = result.fetchmany(row_limit + 1)
        result.close()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        conn.rollback()

    truncated = len(rows) > row_limit
    if truncated:
        rows = rows[:row_limit]
    return {
        "columns": columns,
        "rows": [[_jsonable(v) for v in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }


def cancel_query(meta_engine, tag: str) -> int:
    """Cancel the backend tagged `tag`; return how many were signalled.

    Issued on the *meta* engine rather than the console engine on purpose: both
    point at the same Postgres database (a datasource's `dest_dsn` is
    `STARCUBES_POSTGRES_DSN`), but the console pool may be saturated by the very
    queries we are trying to cancel, and a checkout there would block for
    `pool_timeout=30` before doing anything.

    A return of 0 means "nothing matched" — the query already finished, never
    started, or belongs to another user. That is not an error condition.
    """
    with meta_engine.begin() as conn:
        return int(conn.execute(text(
            "SELECT count(*) FROM ("
            "  SELECT pg_cancel_backend(pid) FROM pg_stat_activity"
            "   WHERE application_name = :tag"
            "     AND datname = current_database()"
            "     AND pid <> pg_backend_pid()) c"
        ), {"tag": tag}).scalar() or 0)


# ── History ─────────────────────────────────────────────────────────────────

#: Entries kept per (user, datasource). Older ones are trimmed on each write.
HISTORY_LIMIT = 100


def history_touch(meta_engine, *, user_id: int, datasource_id: int, sql: str) -> None:
    """Record that `sql` was run, or bump the existing entry's timestamp."""
    params = {"u": int(user_id), "d": int(datasource_id),
              "h": sql_hash(sql), "s": sql}
    with meta_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO starcubes_meta.query_history"
            "       (user_id, datasource_id, sql_hash, sql_text)"
            " VALUES (:u, :d, :h, :s)"
            " ON CONFLICT (user_id, datasource_id, sql_hash)"
            " DO UPDATE SET last_run_at = now(),"
            "               run_count   = starcubes_meta.query_history.run_count + 1"
        ), params)
        conn.execute(text(
            "DELETE FROM starcubes_meta.query_history"
            " WHERE user_id = :u AND datasource_id = :d"
            "   AND id NOT IN (SELECT id FROM starcubes_meta.query_history"
            "                   WHERE user_id = :u AND datasource_id = :d"
            "                   ORDER BY last_run_at DESC LIMIT :lim)"
        ), {"u": params["u"], "d": params["d"], "lim": HISTORY_LIMIT})


def history_list(meta_engine, *, user_id: int, datasource_id: int,
                 limit: int = HISTORY_LIMIT) -> list[dict]:
    """Most-recently-run entries first, for one user and one datasource."""
    with meta_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, sql_text, last_run_at, run_count"
            "  FROM starcubes_meta.query_history"
            " WHERE user_id = :u AND datasource_id = :d"
            " ORDER BY last_run_at DESC LIMIT :lim"
        ), {"u": int(user_id), "d": int(datasource_id),
            "lim": int(limit)}).mappings().all()
    return [{"id": r["id"], "sql": r["sql_text"],
             "last_run_at": r["last_run_at"], "run_count": r["run_count"]}
            for r in rows]


def history_list_user(meta_engine, *, user_id: int, limit: int = 200) -> list[dict]:
    """User-wide query history, joined with datasource name.

    Used by the /sources/me/ profile page so a user can see every query
    they have run across every datasource in one list. The chart
    page's drill-down only shows history for one (user, datasource);
    this is the wider lens.

    Ordered by ``last_run_at DESC``; capped at ``limit`` rows.
    """
    with meta_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT h.id, h.datasource_id, d.name AS datasource_name, "
            "       h.sql_text, h.last_run_at, h.run_count "
            "  FROM starcubes_meta.query_history h "
            "  LEFT JOIN starcubes_meta.datasource d "
            "         ON d.id = h.datasource_id "
            " WHERE h.user_id = :u "
            " ORDER BY h.last_run_at DESC LIMIT :lim"
        ), {"u": int(user_id), "lim": int(limit)}).mappings().all()
    return [{"id": r["id"], "datasource_id": r["datasource_id"],
             "datasource_name": r["datasource_name"],
             "sql": r["sql_text"],
             "last_run_at": r["last_run_at"], "run_count": r["run_count"]}
            for r in rows]


def history_delete(meta_engine, *, user_id: int, entry_id: int) -> int:
    """Delete one history entry. Returns the number of rows removed.

    The `user_id` predicate is not optional: an entry id on its own is not an
    authorization token, and the id comes straight from the request body.
    """
    with meta_engine.begin() as conn:
        return conn.execute(text(
            "DELETE FROM starcubes_meta.query_history"
            " WHERE id = :i AND user_id = :u"
        ), {"i": int(entry_id), "u": int(user_id)}).rowcount


def history_delete_user(meta_engine, *, user_id: int) -> int:
    """Drop every history row belonging to one user.

    Called when an account is deleted. `query_history.user_id` has no foreign
    key to `auth_user` (see migration 0019 for why), so this cleanup is manual.
    """
    with meta_engine.begin() as conn:
        return conn.execute(text(
            "DELETE FROM starcubes_meta.query_history WHERE user_id = :u"
        ), {"u": int(user_id)}).rowcount
