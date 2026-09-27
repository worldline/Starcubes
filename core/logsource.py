"""Reading a log file as if it were a SQL source.

A datasource with ``source_dialect = 'logfile'`` has no query, no cursor and
no WHERE clause. What it has is a regular expression whose **named groups**
are the fact columns, and a pile of files. This module turns that into the
one thing the rest of the pipeline already understands: an iterator of
``pandas.DataFrame`` chunks whose columns are ``Source.fact_column_order()``
and whose time column is tz-aware UTC -- exactly the shape
``pd.read_sql(..., parse_dates={pc: {"utc": True}})`` produces. Everything
downstream of that (transform, ``COPY``, auto-dimensions, partitions,
rollups, alarms, the chart) is untouched and does not learn that log files
exist.

The design decisions worth knowing before changing anything here:

**The watermark is a timestamp, not a position.** The natural cursor into a
file is ``(file, byte offset)``, and it would be faster. But
``etl_run.last_end_time`` is a ``timestamptz``, and backfill, ``--start`` /
``--end``, retention, rewind/repair, the rollups, the alarms and the gap
overlay all read it. So the reader takes a window ``[from, until)`` and
filters on the parsed timestamp, which costs a scan of the files the window
touches and keeps every one of those features working unchanged. The
``mtime`` prefilter is what makes that affordable.

**There is no primary key, and none is needed.** ``last_end_time`` is the
*upper*, half-open bound of a window, so the row carrying that exact
timestamp was never loaded and ``[last_end, until)`` joins on with nothing
read twice. That holds because a log file is kept in timestamp order. The
one place it does not hold is the crash-resume path, where the watermark is
reconstructed from ``max(ts)`` *in fact* -- there the timestamp belongs to a
row that **was** loaded, and ``core/etl.py`` deals with it by counting how
many fact rows carry it and having this reader skip that many (see
``skip_at_start``).

**Time normalisation happens in exactly one place**, ``_parse_time`` below.
That is the file-side counterpart of ``core/source.py:_pin_session_utc``: a
SQL source gets its session pinned to UTC, a file has no session, so the
zone is configuration and the conversion is code. Sub-second precision is
carried through in full -- ``core/etl.py:_ceil_second`` is deliberately not
applied to a log source, because rounding the watermark up to the whole
second would claim up to a second past the measured tip and lose whatever is
appended into that sliver during the run.

**The transform runs before the cast.** ``fact_column.transform_pattern`` /
``transform_replace`` exist for two jobs that turn out to be one mechanism:
collapsing an unbounded value into a usable dimension (a proxied OSM tile
path is millions of distinct values against a 32767 ``smallserial`` ceiling)
and mapping a format's non-value onto a number (nginx writes ``-`` for
bytes, HAProxy ``-1`` for a phase never reached). Neither is exotic; both
are the normal case for their format, and a transform applied *after* the
cast could do neither.

**The two environment guards are read from ``os.environ`` directly and not
through ``core/config.py``**, although everything else global goes through
it. That is deliberate: ``core/config.py`` resolves env-first but falls back
to ``starcubes_meta.app_config``, which the GUI can edit. Routing
``STARCUBES_LOG_EXEC`` through it would let an administrator unlock command
execution for themselves, which is the one thing these guards exist to
prevent. They are a deployer decision, so they live only in the
environment.
"""
from __future__ import annotations

import bz2
import glob as _glob
import gzip
import io
import logging
import lzma
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

# How far back a file's mtime may sit before the window and still be read.
#
# The prefilter's premise is that no line in a file carries a timestamp later
# than the file's own mtime -- true for anything that appends as events happen,
# and true for a rotated-then-compressed generation, whose mtime is the
# compression time and therefore later than its last line. Skipping wrongly is
# silent data loss, while reading one file too many only costs time, so the
# premise is given an hour of slack: enough to absorb clock skew between a
# remote log writer and the host holding the file, which is the one realistic
# way a line can be stamped ahead of its file.
MTIME_SKEW_S = int(os.environ.get("STARCUBES_LOG_MTIME_SKEW_S") or 3600)

# Bytes read off the end of the newest file to find the data tip. A log line is
# a few hundred bytes, so this is thousands of them -- and the tip only has to
# be found, not the file understood.
TAIL_BYTES = int(os.environ.get("STARCUBES_LOG_TAIL_BYTES") or 65536)

# Unparsed lines kept per run for `starcubes_meta.etl_unparsed`. The point is
# to show the operator *which shape* stopped matching, not to mirror the log.
UNPARSED_MAX = int(os.environ.get("STARCUBES_UNPARSED_MAX") or 50)

# Raw line length stored in etl_unparsed. A truncated line still identifies
# the shape; a whole one can be a megabyte of stack trace.
UNPARSED_LINE_CHARS = 500

# Numeric-timestamp spellings for `log_time_format`, mapping to the multiplier
# that turns the number into seconds.
EPOCH_UNITS = {
    "@epoch": 1.0,
    "@epoch_ms": 1e-3,
    "@epoch_us": 1e-6,
    "@epoch_ns": 1e-9,
}

# strptime directives that pin a year. Without one of these the year comes
# from the file's mtime -- syslog RFC3164 is the format that needs it.
_YEAR_DIRECTIVES = ("%Y", "%y", "%G")

_DECOMPRESSORS: dict[str, Callable] = {
    ".gz": gzip.open,
    ".bz2": bz2.open,
    ".xz": lzma.open,
    ".lzma": lzma.open,
}


class LogSourceError(Exception):
    """A log source's **configuration** is wrong.

    Raised for what an operator has to fix and what can be decided without
    touching the network -- an unparseable URI, an unknown scheme, a regex
    that does not compile, a transport a guard refuses, a glob that matches
    nothing. Never raised for a line that does not match: that is counted,
    not fatal.
    """


class LogTransportUnreachable(LogSourceError):
    """The log could not be reached *right now*.

    A subclass so every existing `except LogSourceError` still catches it,
    and separate so `preflight._check_log_source` can keep the asymmetry U5b
    established on the SQL side: a **rejected configuration blocks**, an
    **unreachable source only warns**. A DNS failure, a refused TCP
    connection, an SSH authentication failure or an HTTP error all belong
    here — the extract reports them better than a preflight can, and blocking
    every run on a transient hiccup is worse than letting it try and fail.

    A missing host key is deliberately in here too, although its fix is
    local: the run fails either way, and the message already names the
    remedy, so there is nothing a block adds beyond refusing to try.
    """


def compile_line_regex(pattern: str, what: str = "log_line_regex"):
    """Compile a log pattern with ``re.MULTILINE``.

    The flag is not a detail. A pattern here describes a log *line*, so an
    author writing ``^`` and ``$`` means the start and end of a line -- and
    for a single-line record that is exactly what they get either way. It
    starts to matter once ``log_record_start`` joins a stack trace onto the
    record before it: without the flag, ``$`` anchors at the end of the whole
    joined text, which ``.*`` can never reach because it does not cross a
    newline, and the record silently fails to match. Measured: three
    well-formed records became two rows and one unparsed line.

    An operator who really wants to capture across the newlines writes
    ``(?s)`` in their own pattern and anchors at the true end.
    """
    try:
        return re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise LogSourceError(f"{what} does not compile: {exc}") from exc


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
#
# A plain dataclass rather than reading off `Source`, for one reason that
# matters: the GUI's probe has to work *before* a datasource exists, from
# nothing but the fields typed into the create form. `from_source` is the
# adapter for the ETL's side.


@dataclass
class ColumnSpec:
    """One fact column, as the reader needs it."""
    name: str                                 # fact column name
    group: str                                # the named group (source_expr)
    pg_type: str = "text"
    role: str = "auto-dimension"
    transform_pattern: Optional[str] = None
    transform_replace: Optional[str] = None


@dataclass
class LogConfig:
    """Everything needed to read a log, independent of `Source`."""
    uri: str
    line_regex: str
    time_group: str = ""
    record_start: Optional[str] = None
    time_format: Optional[str] = None
    time_zone: Optional[str] = None
    encoding: Optional[str] = None
    columns: list[ColumnSpec] = field(default_factory=list)
    min_match_pct: float = 0.0

    @classmethod
    def from_source(cls, source) -> "LogConfig":
        cols = [
            ColumnSpec(
                name=c.name, group=(c.source_expr or "").strip(),
                pg_type=c.pg_type, role=c.role,
                transform_pattern=getattr(c, "transform_pattern", None),
                transform_replace=getattr(c, "transform_replace", None),
            )
            for c in sorted(source.fact_columns, key=lambda c: c.ordinal)
        ]
        return cls(
            uri=source.source_dsn,
            line_regex=source.log_line_regex or "",
            time_group=(source.watermark_column or "").strip(),
            record_start=source.log_record_start or None,
            time_format=source.log_time_format or None,
            time_zone=source.log_time_zone or None,
            encoding=source.log_encoding or None,
            columns=cols,
            min_match_pct=float(source.log_min_match_pct or 0.0),
        )


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _resolve_zone(name: Optional[str]) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise LogSourceError(
            f"log_time_zone {name!r} is not a known IANA zone name "
            f"(e.g. 'Europe/Berlin', 'UTC'): {exc}") from exc


def _parse_time(raw: str, fmt: Optional[str], tz: ZoneInfo,
                ref: Optional[datetime]) -> datetime:
    """One log timestamp as tz-aware **UTC**, full precision.

    The single place a log's clock is interpreted, and the reason it is a
    single place: a naive value carries no evidence about its zone, so an
    error here is invisible afterwards. `ref` is the file's mtime and is used
    only to supply a year the format does not carry.

    Precedence, deliberately in this order:

      1. an offset in the value itself (`%z`) always wins -- the line knows
         better than the configuration;
      2. otherwise `log_time_zone`, defaulting to UTC.
    """
    raw = raw.strip()
    if fmt and fmt in EPOCH_UNITS:
        try:
            secs = float(raw) * EPOCH_UNITS[fmt]
        except ValueError as exc:
            raise ValueError(f"{raw!r} is not a number ({fmt})") from exc
        return datetime.fromtimestamp(secs, timezone.utc)

    if not fmt:
        # ISO-8601. fromisoformat handles 'Z' and fractional seconds since
        # 3.11, which is well below this project's floor.
        dt = datetime.fromisoformat(raw)
    else:
        dt = datetime.strptime(raw, fmt)
        if not any(d in fmt for d in _YEAR_DIRECTIVES):
            dt = _supply_year(dt, ref)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _supply_year(dt: datetime, ref: Optional[datetime]) -> datetime:
    """Give a year-less timestamp (syslog RFC3164) the year it belongs to.

    strptime defaults to 1900. The file's mtime is the anchor, and the only
    interesting case is the turn of the year: a line reading ``Dec 31
    23:59:00`` in a file last written on ``Jan 02`` belongs to the *previous*
    year, and taking the mtime's year would date it eleven months into the
    future. So the year is chosen and then walked back if the result sits
    ahead of the file that contains it.

    Without a reference there is nothing better than the current year; a log
    whose transport cannot report an mtime and whose format has no year is
    outside what can be resolved correctly, and the ambiguity is logged
    rather than hidden.
    """
    if ref is None:
        ref = datetime.now(timezone.utc)
    try:
        out = dt.replace(year=ref.year)
    except ValueError:
        # 29 February against a non-leap reference year.
        out = dt.replace(year=ref.year, day=28)
    # One day of slack, not zero: a line written moments before the mtime we
    # are comparing against must not be pushed back a whole year by rounding.
    if out.replace(tzinfo=timezone.utc) > ref + timedelta(days=1):
        try:
            out = out.replace(year=ref.year - 1)
        except ValueError:
            out = out.replace(year=ref.year - 1, day=28)
    return out


# ---------------------------------------------------------------------------
# Values: transform, then cast
# ---------------------------------------------------------------------------


# Roles whose value reaches the DataFrame uncast. A dimension column's fact
# type is the surrogate index's (`smallint`); the value travelling here is the
# source value the ETL maps to that index.
_RAW_ROLES = {"dimension", "auto-dimension"}

_INT_TYPES = {"smallint", "int2", "integer", "int", "int4", "bigint", "int8"}
_FLOAT_TYPES = {"real", "float4", "double precision", "float8", "numeric",
                "decimal"}


def _cast(value: str, pg_type: str):
    """A matched (and transformed) string as the fact column's type.

    Raises ValueError, which the caller turns into an unparsed record naming
    the column -- a cast failure is a configuration problem (a missing
    transform, usually), and saying which field failed is the whole
    diagnostic.
    """
    t = (pg_type or "text").strip().lower()
    if value is None:
        return None
    if t in _INT_TYPES:
        # int(" 12 ") works, int("12.0") does not; a log that writes a
        # decimal into an integer column is a real case (bytes as "1043.0"),
        # so go through float first and keep the failure for genuine junk.
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            return int(float(s))
    if t in _FLOAT_TYPES:
        return float(value.strip())
    return value


class _Transform:
    """A compiled `re.sub` for one column."""

    __slots__ = ("pattern", "replace")

    def __init__(self, pattern: str, replace: Optional[str]):
        try:
            self.pattern = re.compile(pattern)
        except re.error as exc:
            raise LogSourceError(
                f"transform_pattern {pattern!r} does not compile: {exc}"
            ) from exc
        self.replace = replace or ""

    def apply(self, value: str) -> str:
        return self.pattern.sub(self.replace, value)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class LineParser:
    """One log's regex, transforms and clock, compiled once.

    ``parse`` answers with ``(row, reason)``: a dict keyed by **fact column
    name** and ``None``, or ``None`` and a short phrase saying why. The
    reason is what reaches ``etl_unparsed`` alongside the raw line, and it is
    the difference between "the format changed" and "one field needs a
    transform".
    """

    def __init__(self, config: LogConfig):
        if not (config.line_regex or "").strip():
            raise LogSourceError(
                "a log datasource needs log_line_regex -- the named groups in "
                "it are what the fact columns are read from")
        self.line_re = compile_line_regex(config.line_regex)

        self.record_start_re = None
        if (config.record_start or "").strip():
            self.record_start_re = compile_line_regex(
                config.record_start, "log_record_start")

        self.groups = set(self.line_re.groupindex)
        self.time_group = (config.time_group or "").strip()
        self.time_format = config.time_format
        self.tz = _resolve_zone(config.time_zone)

        self.columns = list(config.columns)
        self.transforms: dict[str, _Transform] = {
            c.name: _Transform(c.transform_pattern, c.transform_replace)
            for c in self.columns if (c.transform_pattern or "").strip()
        }

        # Validated here rather than at first line: a datasource whose fact
        # columns name groups the regex does not have produces zero rows and
        # no error, which is the least debuggable outcome available.
        if self.time_group and self.time_group not in self.groups:
            raise LogSourceError(
                f"the time group {self.time_group!r} (watermark_column) is not "
                f"a named group of log_line_regex; it has "
                f"{sorted(self.groups) or 'none'}")
        missing = sorted({c.group for c in self.columns if c.group} - self.groups)
        if missing:
            raise LogSourceError(
                f"fact columns name group(s) {missing} that log_line_regex "
                f"does not capture; it has {sorted(self.groups) or 'none'}")

    def parse(self, raw: str, ref: Optional[datetime]
              ) -> tuple[Optional[dict], Optional[str]]:
        m = self.line_re.search(raw)
        if m is None:
            return None, "no match"
        captured = m.groupdict()
        row: dict = {}
        for col in self.columns:
            value = captured.get(col.group)
            tf = self.transforms.get(col.name)
            if tf is not None and value is not None:
                value = tf.apply(value)
            if col.group == self.time_group or col.role == "time":
                if value is None:
                    return None, f"{col.name}: no timestamp captured"
                try:
                    row[col.name] = _parse_time(
                        value, self.time_format, self.tz, ref)
                except Exception as exc:
                    return None, f"{col.name}: {exc}"
                continue
            if col.role in _RAW_ROLES:
                # **Not** cast to the fact column's type, and this is the one
                # place it would be tempting. A dimension fact column is
                # `smallint` because it stores the dim table's surrogate
                # index — but what belongs in the DataFrame here is still the
                # raw source value, because `core/etl.py:_resolve_auto_dim_
                # chunk` is what turns it into that index, in the parent, on
                # the chunk. Casting here would send every non-numeric value
                # (a hostname, a URL path) to etl_unparsed and leave the
                # dimension empty. It is exactly what a SQL source does: its
                # `source_expr` projects text into a smallint column and the
                # transform worker maps it.
                row[col.name] = value
                continue
            try:
                row[col.name] = _cast(value, col.pg_type)
            except (TypeError, ValueError) as exc:
                return None, f"{col.name}: {exc}"
        return row, None

    def parse_time_only(self, raw: str, ref: Optional[datetime]
                        ) -> Optional[datetime]:
        """The record's timestamp, or None -- the tip and min probes' path.

        Skips the transforms and casts of every other column, because finding
        the newest timestamp in a file must not depend on a column elsewhere
        in the line being well formed.
        """
        m = self.line_re.search(raw)
        if m is None:
            return None
        value = m.groupdict().get(self.time_group)
        if value is None:
            return None
        for col in self.columns:
            if col.group == self.time_group:
                tf = self.transforms.get(col.name)
                if tf is not None:
                    value = tf.apply(value)
                break
        try:
            return _parse_time(value, self.time_format, self.tz, ref)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


@dataclass
class FileRef:
    """One readable generation of a log."""
    key: str                       # what the operator sees (and etl_unparsed stores)
    mtime: Optional[datetime]      # UTC, None when the transport cannot say
    size: Optional[int]
    opener: Callable[[], object]   # -> binary file object
    compressed: bool = False
    # For a transport that cannot seek but can ask for a byte range —
    # `https://` with `Accept-Ranges`. Takes a byte count, returns the tail
    # or None. Without it `seekable_tail` falls back to seek().
    tail_reader: Optional[Callable[[int], Optional[bytes]]] = None

    def open_text(self, encoding: Optional[str]):
        enc = (encoding or "utf-8").strip() or "utf-8"
        # errors='replace' always: one bad byte in a gigabyte of log must not
        # end a load, and a replacement character in one field is a far
        # better outcome than a run that dies at 80%.
        return io.TextIOWrapper(self.opener(), encoding=enc, errors="replace",
                                newline="")

    def seekable_tail(self, encoding: Optional[str], nbytes: int):
        """The last `nbytes` as text lines, or None when not seekable.

        A compressed generation cannot be seeked into, and the newest file --
        the only one the tip probe cares about -- is in practice never
        compressed, so this returning None is the rare path.
        """
        if self.compressed or not self.size:
            return None
        start = max(0, self.size - nbytes)
        if self.tail_reader is not None:
            data = self.tail_reader(nbytes)
            if data is None:
                return None
        else:
            fh = self.opener()
            try:
                fh.seek(start)
                data = fh.read()
            finally:
                fh.close()
        if start > 0:
            # Drop the partial first line rather than mis-parse it.
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        enc = (encoding or "utf-8").strip() or "utf-8"
        return data.decode(enc, errors="replace").splitlines()


def _allowed_roots() -> list[Path]:
    """`STARCUBES_LOG_ROOTS`, the file:// guard. Empty = no restriction.

    Read straight from the environment on purpose -- see the module docstring
    on why this must not go through `core/config.py`.
    """
    raw = (os.environ.get("STARCUBES_LOG_ROOTS") or "").strip()
    if not raw:
        return []
    return [Path(p).resolve() for p in raw.split(":") if p.strip()]


def _check_root(path: Path) -> None:
    roots = _allowed_roots()
    if not roots:
        return
    try:
        real = path.resolve()
    except OSError as exc:
        raise LogSourceError(f"{path}: {exc}") from exc
    for root in roots:
        if real == root or root in real.parents:
            return
    raise LogSourceError(
        f"{real} is outside STARCUBES_LOG_ROOTS "
        f"({':'.join(str(r) for r in roots)}). That variable is set in this "
        f"deployment's environment and only the deployer can widen it.")


def _decompressor_for_name(name: str):
    """The decompressor for a file *name*, or None. Suffix only."""
    for suffix, dec in _DECOMPRESSORS.items():
        if name.lower().endswith(suffix):
            return dec
    return None


def _wrap_decompress(fileobj, name: str):
    """Decompress a stream by its name's suffix.

    The local transport hands `gzip.open` a path; a remote one has only a
    file object, and all three stdlib decompressors take one as their first
    positional argument. Same suffix table either way.
    """
    dec = _decompressor_for_name(name)
    return fileobj if dec is None else dec(fileobj)


def _opener_for(path: Path) -> tuple[Callable[[], object], bool]:
    """A binary opener for `path`, decompressing by suffix.

    Transparent decompression is what makes the history reach past the
    current generation: logrotate's usual shape is `access.log`,
    `access.log.1`, `access.log.2.gz`, and without the `.gz` the window can
    only ever go back a day or two.
    """
    dec = _DECOMPRESSORS.get(path.suffix.lower())
    if dec is not None:
        return (lambda: dec(path, "rb")), True
    return (lambda: open(path, "rb")), False


class LocalGlobTransport:
    """``file:///var/log/nginx/access.log*`` -- a path or a glob on this host.

    Zero dependencies, and it covers more than it looks: a bind-mounted host
    directory, an NFS or CIFS share the host has mounted, and a sidecar
    container sharing a volume all arrive here as a local path.
    """

    scheme = "file"
    # Not a stream: the glob lists generations, each has an mtime the window
    # prefilter can use, and the newest one's tail can be read without
    # reading the file. `probe_tip` relies on all three.
    streaming = False

    def __init__(self, uri: str):
        parts = urlsplit(uri)
        if parts.netloc and parts.netloc != "localhost":
            raise LogSourceError(
                f"file:// URI with a host ({parts.netloc!r}) -- a local path "
                f"is spelled file:///var/log/... with three slashes")
        self.pattern = unquote(parts.path)
        if not self.pattern:
            raise LogSourceError("file:// URI has no path")

    def close(self) -> None:
        """Nothing to release; present so every transport has the method."""

    def list_files(self, from_: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> list[FileRef]:
        # The window is accepted and ignored here: a glob does not narrow by
        # time. `exec://` is the transport that uses it, by handing the bounds
        # to the command.
        matches = sorted(_glob.glob(self.pattern))
        if not matches:
            # Check the root guard even with nothing matched, so a refused
            # path is reported as refused rather than as "no files".
            _check_root(Path(self.pattern).parent)
            return []
        out: list[FileRef] = []
        for name in matches:
            p = Path(name)
            if not p.is_file():
                continue
            _check_root(p)
            try:
                st = p.stat()
            except OSError as exc:
                logger.warning("log source: cannot stat %s: %s", p, exc)
                continue
            opener, compressed = _opener_for(p)
            out.append(FileRef(
                key=p.name,
                mtime=datetime.fromtimestamp(st.st_mtime, timezone.utc),
                size=st.st_size, opener=opener, compressed=compressed))
        return out

    def describe(self) -> str:
        return f"file glob {self.pattern}"


# ---------------------------------------------------------------------------
# sftp://
# ---------------------------------------------------------------------------


SFTP_TIMEOUT_S = float(os.environ.get("STARCUBES_LOG_SFTP_TIMEOUT_S") or 20.0)


class SftpGlobTransport:
    """``sftp://user@host/var/log/nginx/access.log*`` — the same glob, remote.

    The case this exists for is the ordinary one: the log is on the machine
    that wrote it. Behaves like the local transport in every way the reader
    cares about — generations, mtimes, and a seekable tail (paramiko's file
    objects support ``seek``) — so the window prefilter and the tip probe work
    unchanged and nothing above this class knows the file is remote.

    **Host keys are verified**, and that is a deliberate default rather than a
    convenience gap: paramiko's ``RejectPolicy`` means an unknown host is
    refused until somebody puts it in ``known_hosts``.
    ``STARCUBES_LOG_SFTP_AUTOADD=1`` relaxes it to trust-on-first-use, and it
    is an *environment* variable for the same reason
    ``STARCUBES_LOG_EXEC`` is — the deployer decides, not whoever can edit a
    datasource. Note that it is genuinely trust-on-*first*-use rather than
    trust-always: paramiko writes the accepted key into
    ``~/.ssh/known_hosts``, so a later connection is verified against it and a
    changed key is refused even with the variable still set. Verified by
    connecting with a fresh HOME.

    Authentication, in paramiko's order: a password in the URI, then the keys
    under ``~/.ssh``, then an agent. A password in a URI is stored in
    ``source_dsn`` like any other DSN and masked the same way in the GUI; a
    key is better and needs nothing in the datasource at all.

    One connection per transport instance, reused across files and closed by
    `close()`. A fresh handshake per file would cost ~100 ms each, which a
    60-second forever loop notices.
    """

    scheme = "sftp"
    streaming = False

    def __init__(self, uri: str):
        parts = urlsplit(uri)
        if not parts.hostname:
            raise LogSourceError(
                "sftp:// URI has no host — the shape is "
                "sftp://user@host/var/log/app.log*")
        path = unquote(parts.path or "")
        if not path:
            raise LogSourceError("sftp:// URI has no path")
        self.host = parts.hostname
        self.port = parts.port or 22
        self.user = unquote(parts.username) if parts.username else None
        self.password = unquote(parts.password) if parts.password else None
        self.pattern = path
        self._client = None
        self._sftp = None

    def _connect(self):
        if self._sftp is not None:
            return self._sftp
        try:
            import paramiko
        except ImportError as exc:
            raise LogSourceError(
                "sftp:// needs the paramiko package, which this build does "
                "not have. Install it (it is in requirements.txt) or use "
                "file:// against a mount, or exec:// with ssh.") from exc
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # The file has to *exist* before `load_host_keys`, not merely be
        # loadable. paramiko sets `_host_keys_filename` and then reads it, so
        # swallowing the read error still leaves the filename set — and
        # `AutoAddPolicy` then calls `save_host_keys`, which re-reads it and
        # raises `FileNotFoundError`. A container has no ~/.ssh/known_hosts,
        # so the very first sftp:// connection in the shipped image failed
        # with that rather than connecting. Creating it empty is what `ssh`
        # effectively does; a read-only HOME falls back to the system keys.
        known = Path(os.path.expanduser("~/.ssh/known_hosts"))
        try:
            known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            known.touch(mode=0o600, exist_ok=True)
        except OSError:
            logger.debug("cannot create %s; system host keys only", known)
        try:
            client.load_host_keys(str(known))
        except OSError:
            pass
        if (os.environ.get("STARCUBES_LOG_SFTP_AUTOADD") or "") == "1":
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(self.host, port=self.port, username=self.user,
                           password=self.password,
                           timeout=SFTP_TIMEOUT_S,
                           banner_timeout=SFTP_TIMEOUT_S,
                           auth_timeout=SFTP_TIMEOUT_S,
                           allow_agent=True, look_for_keys=True)
        except Exception as exc:
            name = type(exc).__name__
            msg = str(exc).lower()
            # Matched on the *message*, and on both spellings. paramiko says
            # "not found in known_hosts" where the class name says only
            # SSHException — so testing for "host key" alone missed it, and
            # the operator got the bare exception instead of the sentence
            # naming the way out. (The condition also needed its parentheses:
            # `A or B and C` binds as `A or (B and C)`.)
            if ("HostKey" in name or "known_hosts" in msg
                    or "host key" in msg):
                raise LogTransportUnreachable(
                    f"{self.host}: {exc}. The host key is not in known_hosts. "
                    f"Add it there, or set STARCUBES_LOG_SFTP_AUTOADD=1 in "
                    f"this deployment's environment to trust it on first use "
                    f"— that is an environment variable and not a setting, "
                    f"because trusting an unverified host is the deployer's "
                    f"call."
                ) from exc
            raise LogTransportUnreachable(
                f"{self.host}: {name}: {exc}") from exc
        self._client = client
        self._sftp = client.open_sftp()
        return self._sftp

    def close(self) -> None:
        for obj in (self._sftp, self._client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                logger.debug("closing the sftp connection failed",
                             exc_info=True)
        self._sftp = self._client = None

    def list_files(self, from_: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> list[FileRef]:
        import posixpath
        from fnmatch import fnmatch
        sftp = self._connect()
        directory = posixpath.dirname(self.pattern) or "/"
        pattern = posixpath.basename(self.pattern)
        try:
            entries = sftp.listdir_attr(directory)
        except Exception as exc:
            # A missing directory is configuration, an I/O error is not — but
            # both look the same from here and both make the run fail, so the
            # gentler verdict is the right default.
            raise LogTransportUnreachable(
                f"{self.host}:{directory}: {exc}") from exc
        out: list[FileRef] = []
        for attr in entries:
            if not fnmatch(attr.filename, pattern):
                continue
            import stat as _stat
            if attr.st_mode is not None and not _stat.S_ISREG(attr.st_mode):
                continue
            remote = posixpath.join(directory, attr.filename)
            name = attr.filename
            compressed = _decompressor_for_name(name) is not None

            def opener(remote=remote, name=name):
                fh = self._connect().open(remote, "rb")
                # Without this paramiko reads in small requests and a
                # multi-megabyte generation crawls; it is the documented knob
                # for exactly this.
                try:
                    fh.prefetch()
                except Exception:
                    pass
                return _wrap_decompress(fh, name)

            out.append(FileRef(
                key=name,
                mtime=(datetime.fromtimestamp(attr.st_mtime, timezone.utc)
                       if attr.st_mtime else None),
                size=attr.st_size, opener=opener, compressed=compressed))
        return out

    def describe(self) -> str:
        who = f"{self.user}@" if self.user else ""
        return f"sftp glob {who}{self.host}:{self.pattern}"


# ---------------------------------------------------------------------------
# https:// and http://
# ---------------------------------------------------------------------------


HTTP_TIMEOUT_S = float(os.environ.get("STARCUBES_LOG_HTTP_TIMEOUT_S") or 20.0)


class HttpTransport:
    """``https://host/logs/app.log`` — one resource, no glob.

    A URL names one thing, so there are no generations and the history reaches
    exactly as far as that resource does. What it does get is the two things
    the reader wants from a file: ``Last-Modified`` stands in for the mtime, so
    the window prefilter can skip a resource untouched since before the
    window, and a suffix ``Range`` request stands in for seeking, so the tip
    probe reads a few kilobytes instead of the whole log. Where the server
    offers neither the resource is re-read per run, which is worth knowing
    before pointing this at a gigabyte.

    Only stdlib. Credentials in the URI become Basic auth; anything else
    (bearer tokens, presigned URLs) belongs in the URL itself.
    """

    scheme = "https"
    streaming = False

    def __init__(self, uri: str):
        parts = urlsplit(uri)
        if not parts.hostname:
            raise LogSourceError(f"{parts.scheme}:// URI has no host")
        if any(ch in (parts.path or "") for ch in "*?["):
            raise LogSourceError(
                "an http(s) URL names one resource — a glob cannot be "
                "expanded over HTTP. Point it at a single log, or use "
                "file:// / sftp:// for rotated generations.")
        self._auth = None
        if parts.username:
            import base64
            raw = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
            self._auth = "Basic " + base64.b64encode(
                raw.encode()).decode("ascii")
        # Strip the credentials back out of the URL that goes on the wire.
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        self.url = f"{parts.scheme}://{netloc}{parts.path}" + (
            f"?{parts.query}" if parts.query else "")
        self.name = (parts.path or "/").rsplit("/", 1)[-1] or "log"

    def close(self) -> None:
        """Nothing to release: urllib opens per request."""

    def _request(self, method: str, extra: Optional[dict] = None):
        import urllib.request
        req = urllib.request.Request(self.url, method=method)
        if self._auth:
            req.add_header("Authorization", self._auth)
        for k, v in (extra or {}).items():
            req.add_header(k, v)
        return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)

    def _head(self) -> tuple[Optional[datetime], Optional[int], bool]:
        import email.utils
        try:
            with self._request("HEAD") as resp:
                headers = resp.headers
        except Exception as exc:
            raise LogTransportUnreachable(f"{self.url}: {exc}") from exc
        lm = headers.get("Last-Modified")
        mtime = None
        if lm:
            try:
                mtime = email.utils.parsedate_to_datetime(lm)
                if mtime.tzinfo is None:
                    mtime = mtime.replace(tzinfo=timezone.utc)
                mtime = mtime.astimezone(timezone.utc)
            except Exception:
                mtime = None
        try:
            size = int(headers.get("Content-Length") or 0) or None
        except ValueError:
            size = None
        ranges = "bytes" in (headers.get("Accept-Ranges") or "").lower()
        return mtime, size, ranges

    def list_files(self, from_: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> list[FileRef]:
        mtime, size, ranges = self._head()
        compressed = _decompressor_for_name(self.name) is not None

        def opener():
            try:
                resp = self._request("GET")
            except Exception as exc:
                raise LogTransportUnreachable(
                    f"{self.url}: {exc}") from exc
            return _wrap_decompress(resp, self.name)

        def tail(nbytes: int) -> Optional[bytes]:
            if not ranges or not size:
                return None
            try:
                with self._request(
                        "GET", {"Range": f"bytes=-{int(nbytes)}"}) as resp:
                    # 200 means the server ignored the range and sent
                    # everything; treating that as a tail would mis-parse the
                    # first line, so decline and let the caller read normally.
                    if resp.status != 206:
                        return None
                    return resp.read()
            except Exception:
                logger.debug("range request failed", exc_info=True)
                return None

        return [FileRef(key=self.name, mtime=mtime, size=size, opener=opener,
                        compressed=compressed,
                        tail_reader=None if compressed else tail)]

    def describe(self) -> str:
        return f"http resource {self.url}"


class HttpPlainTransport(HttpTransport):
    """``http://`` — the same, unencrypted. Separate only so the scheme maps."""
    scheme = "http"


# ---------------------------------------------------------------------------
# exec://
# ---------------------------------------------------------------------------


class _ProcStream(io.RawIOBase):
    """A subprocess's stdout as a binary stream, with its exit code enforced.

    The point of the class rather than handing over ``proc.stdout``: a command
    that fails must not look like an empty log. ``close`` waits and raises with
    the tail of stderr, so a wrong ``journalctl`` unit name is an error the
    operator sees instead of a run that loaded nothing and reported success.
    """

    def __init__(self, argv: list[str]):
        import subprocess
        self._argv = argv
        try:
            self._p = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False)
        except OSError as exc:
            # The program is not there or not executable: a configuration
            # fault the allowlist could not catch, and no retry fixes it.
            raise LogSourceError(f"{argv[0]}: {exc}") from exc
        self._err: bytes = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b):
        return self._p.stdout.readinto(b)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self._p.poll() is None:
                try:
                    self._p.stdout.close()
                except Exception:
                    pass
            self._err = (self._p.stderr.read() or b"")[-2000:]
            self._p.stderr.close()
            rc = self._p.wait(timeout=30)
        except Exception:
            rc = None
            try:
                self._p.kill()
            except Exception:
                pass
        finally:
            super().close()
        if rc not in (0, None):
            raise LogTransportUnreachable(
                f"{self._argv[0]} exited {rc}: "
                f"{self._err.decode('utf-8', 'replace').strip()[:400]}")


class ExecTransport:
    """``exec:///usr/bin/journalctl -u app --since {from} -o short-iso``

    The transport for everything the other three do not reach: journald,
    ``kubectl logs``, ``ssh host cat``, a vendor's own extraction tool. One
    command, its stdout is the log.

    **Off unless the deployer turns it on.** ``STARCUBES_LOG_EXEC`` is a
    comma-separated allowlist of permitted programs and empty by default, so
    the shipped image refuses ``exec://`` outright. That is the whole reason
    it is an environment variable: an administrator can edit a datasource, and
    a datasource field that runs arbitrary commands would make "can configure
    a datasource" mean "can run anything in the container". The command is
    always an argv list (``shlex.split``), never a shell string — no globbing,
    no pipes, no ``;``.

    ``{from}`` and ``{until}`` are substituted with ISO-8601 UTC, which is what
    makes this transport efficient rather than merely possible: the *source*
    does the windowing, so an incremental run asks journald for two minutes
    instead of reading a week. Without the placeholders the whole stream is
    read every run and filtered here. An absent lower bound (the initial load)
    becomes ``1970-01-01T00:00:00Z``, which every tool that takes a ``--since``
    reads as "everything".

    A stream has no mtime and no tail, so the tip cannot be measured from it —
    see `LogReader.probe_tip` for what stands in and why the lag knobs matter
    here.
    """

    scheme = "exec"
    streaming = True

    def __init__(self, uri: str):
        import shlex
        parts = urlsplit(uri)
        command = unquote(parts.path or "").strip()
        if parts.netloc:
            raise LogSourceError(
                "exec:// takes no host — the shape is exec:///usr/bin/prog "
                "--flag, with three slashes before an absolute path")
        if not command:
            raise LogSourceError("exec:// URI has no command")
        try:
            self.argv = shlex.split(command)
        except ValueError as exc:
            raise LogSourceError(f"could not parse the command: {exc}") from exc
        if not self.argv:
            raise LogSourceError("exec:// URI has no command")
        self._check_allowed()

    def _check_allowed(self) -> None:
        raw = (os.environ.get("STARCUBES_LOG_EXEC") or "").strip()
        if not raw:
            raise LogSourceError(
                "exec:// is disabled in this deployment. Set "
                "STARCUBES_LOG_EXEC to a comma-separated list of permitted "
                "programs in the environment to enable it — it is an "
                "environment variable, not a setting, because it decides "
                "whether configuring a datasource can run commands.")
        allowed = {a.strip() for a in raw.split(",") if a.strip()}
        if self.argv[0] not in allowed:
            raise LogSourceError(
                f"{self.argv[0]} is not in STARCUBES_LOG_EXEC "
                f"({', '.join(sorted(allowed))}). The allowlist matches the "
                f"program exactly, so give the full path as it is written "
                f"here.")
        # Checked here, at construction, rather than left to the first read.
        # It is decidable without running anything, and discovering it later
        # puts the finding on the wrong preflight check: `log_source` cannot
        # see it (listing a stream touches nothing) so it reported the
        # streaming warning while `log_pattern` blocked, which reads as two
        # unrelated problems instead of one missing program.
        if not os.access(self.argv[0], os.X_OK):
            raise LogSourceError(
                f"{self.argv[0]} is not an executable file in this container. "
                f"It is on the STARCUBES_LOG_EXEC allowlist, so the path is "
                f"permitted — it just is not there.")

    def close(self) -> None:
        """Nothing held between reads: each read starts its own process."""

    def _substitute(self, from_, until) -> list[str]:
        def iso(dt, fallback):
            if dt is None:
                return fallback
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lo = iso(from_, "1970-01-01T00:00:00Z")
        hi = iso(until, datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"))
        return [a.replace("{from}", lo).replace("{until}", hi)
                for a in self.argv]

    def list_files(self, from_: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> list[FileRef]:
        argv = self._substitute(from_, until)
        # mtime and size stay None: the prefilter then always keeps this
        # "file" and `seekable_tail` declines, which is what a stream needs.
        return [FileRef(key=Path(argv[0]).name, mtime=None, size=None,
                        opener=lambda: _ProcStream(argv), compressed=False)]

    def describe(self) -> str:
        return "exec " + " ".join(self.argv)


_TRANSPORTS = {
    "file": LocalGlobTransport,
    "sftp": SftpGlobTransport,
    "https": HttpTransport,
    "http": HttpPlainTransport,
    "exec": ExecTransport,
}


def open_transport(uri: str):
    """The transport for a log URI, by scheme."""
    uri = (uri or "").strip()
    if not uri:
        raise LogSourceError(
            "a log datasource needs a source_dsn naming where the log is, "
            "e.g. file:///var/log/nginx/access.log*")
    scheme = urlsplit(uri).scheme.lower()
    if not scheme:
        raise LogSourceError(
            f"{uri!r} names no transport -- prefix it with a scheme, "
            f"e.g. file://{uri if uri.startswith('/') else '/' + uri}")
    cls = _TRANSPORTS.get(scheme)
    if cls is None:
        raise LogSourceError(
            f"{scheme}:// is not a supported log transport; this build has "
            f"{', '.join(sorted(_TRANSPORTS))}")
    return cls(uri)


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@dataclass
class UnparsedLine:
    file_name: str
    line_no: int
    raw_line: str
    reason: str


class LogReader:
    """The three questions `core/etl.py` asks a source, answered from files.

    A class rather than free functions because the counters have to survive
    the iteration: the ETL writes `parsed_rows`, `unparsed_rows`, `match_pct`
    and `files_read` into `etl_run.stats` and the samples into
    `etl_unparsed`, all *after* `iter_chunks` has finished.
    """

    def __init__(self, config: LogConfig):
        self.config = config
        self.parser = LineParser(config)
        self.transport = open_transport(config.uri)
        self.parsed_rows = 0
        self.unparsed_rows = 0
        self.files_read = 0
        self.unparsed: list[UnparsedLine] = []

    # -- introspection -----------------------------------------------------

    @property
    def match_pct(self) -> Optional[float]:
        total = self.parsed_rows + self.unparsed_rows
        if not total:
            return None
        return 100.0 * self.parsed_rows / total

    def list_files(self, from_: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> list[FileRef]:
        """Every generation, oldest first.

        Sorted by mtime and then by name: mtime alone leaves ties, and the
        order has to be deterministic for the crash-resume skip count to mean
        anything. A transport that cannot report mtime sorts first by name.

        The window is handed down because `exec://` uses it — its `{from}` and
        `{until}` placeholders let the *command* do the narrowing, which is the
        difference between asking journald for two minutes and reading a week.
        The globbing transports ignore it.
        """
        files = self.transport.list_files(from_, until)
        return sorted(files, key=lambda f: (f.mtime or datetime.min.replace(
            tzinfo=timezone.utc), f.key))

    def close(self) -> None:
        """Release whatever the transport holds. Idempotent, never raises."""
        try:
            self.transport.close()
        except Exception:
            logger.debug("closing the log transport failed", exc_info=True)

    # -- the tip and the floor --------------------------------------------

    def probe_tip(self) -> tuple[Optional[datetime], datetime]:
        """``(max_wm, src_now)`` -- the data tip and the reader's clock.

        The tip is the newest parsable timestamp in the newest generation,
        found from its tail rather than by reading it. Under-reading is the
        harmless direction: the remainder simply loads next run. Over-reading
        would skip rows, because ``end_time <= max_wm`` is a hard clamp in
        ``import_fact``.
        """
        src_now = datetime.now(timezone.utc)
        if self.transport.streaming:
            # A stream has no tail to read and no mtime to ask, so the tip
            # cannot be *measured* — and reading the whole stream to find it
            # would double the cost of every run. The reader's clock stands in,
            # which means the window claims up to now and anything written into
            # it after the run started is missed.
            #
            # That is exactly what `watermark_lag_s` / `wallclock_lag_s` exist
            # for, so the answer is to use them rather than to invent a second
            # mechanism here; `preflight._check_log_source` warns when a
            # streaming transport has both at 0.
            return src_now, src_now
        files = self.list_files()
        if not files:
            return None, src_now
        # The **max over the last two generations**, not the tip of the
        # newest one. Right after a rotation the newest mtime belongs to the
        # file logrotate just *compressed*, whose last line is older than the
        # freshly-opened current file — so reading only `files[-1]` reports a
        # tip behind the watermark, the monotonicity clamp empties the window,
        # and one run does nothing. Self-correcting on the next pass, but two
        # tails cost a few kilobytes and remove the stall. Measured on a
        # fixture where the .gz was written second.
        tips = [t for t in (self._tip_of(f) for f in files[-2:])
                if t is not None]
        return (max(tips) if tips else None), src_now

    def _tip_of(self, fref: FileRef) -> Optional[datetime]:
        lines = fref.seekable_tail(self.config.encoding, TAIL_BYTES)
        if lines is not None:
            for raw in reversed(lines):
                if not raw.strip():
                    continue
                ts = self.parser.parse_time_only(raw, fref.mtime)
                if ts is not None:
                    return ts
            return None
        last = None
        with fref.open_text(self.config.encoding) as fh:
            for raw in fh:
                ts = self.parser.parse_time_only(raw, fref.mtime)
                if ts is not None:
                    last = ts
        return last

    def min_watermark(self) -> Optional[datetime]:
        """The oldest parsable timestamp, for ``initial_window_s``.

        Read from the head of the oldest generation, so this costs one file's
        first lines rather than the whole history.
        """
        if self.transport.streaming:
            # Reading a whole stream to find its oldest line is the one thing
            # `initial_window_s` was meant to avoid, so it is declined rather
            # than paid for. Returning None leaves the first window unbounded
            # below, which is the behaviour without `initial_window_s` set.
            logger.info(
                "log source: %s is a stream, so min(watermark) is not read — "
                "initial_window_s cannot narrow the first load here",
                self.transport.describe())
            return None
        for fref in self.list_files():
            with fref.open_text(self.config.encoding) as fh:
                for raw in fh:
                    ts = self.parser.parse_time_only(raw, fref.mtime)
                    if ts is not None:
                        return ts
        return None

    # -- the extract -------------------------------------------------------

    def iter_chunks(self, from_: Optional[datetime],
                    until: Optional[datetime], chunksize: int,
                    skip_at_start: int = 0,
                    ) -> Iterator[pd.DataFrame]:
        """DataFrame chunks for the half-open window ``[from_, until)``.

        ``skip_at_start`` drops that many records whose timestamp equals
        ``from_`` exactly. It is the crash-resume de-duplication and the only
        thing standing in for a primary key: after an interrupted run the
        watermark is reconstructed as ``max(ts)`` *in fact*, which is a row
        that was loaded, so the caller counts how many fact rows carry that
        timestamp and this skips exactly those. Correct because the read order
        is deterministic -- generations by mtime, lines in file order -- and
        because a log is kept in timestamp order.
        """
        names = [c.name for c in self.config.columns]
        rows: list[dict] = []
        skipped = 0
        for fref in self.list_files(from_, until):
            if not self._touches_window(fref, from_):
                logger.debug("log source: skipping %s (mtime %s before window)",
                             fref.key, fref.mtime)
                continue
            self.files_read += 1
            # An unparsed record has no timestamp, so the window filter below
            # cannot reach it — and counting it regardless is wrong in a way
            # that bites immediately. Measured on the first end-to-end run: a
            # quiet incremental pass re-read the two malformed lines in the
            # file, reported `0 parsed, 2 unparsed (0.00% match)`, appended
            # them to etl_unparsed again, and with `log_min_match_pct` set
            # would have failed every run that legitimately had nothing to do.
            #
            # A log is kept in timestamp order, so the last record parsed
            # before it dates an unparsed one well enough: below the window
            # start it belongs to a pass that already reported it. Where
            # nothing has been parsed yet in this file the line cannot be
            # dated at all, and then it is *not* counted — refusing a run over
            # a line we cannot place would be worse than missing it.
            last_ts = None
            for line_no, raw in self._iter_records(fref):
                row, reason = self.parser.parse(raw, fref.mtime)
                if row is None:
                    if from_ is None or (last_ts is not None
                                         and last_ts >= from_):
                        self._note_unparsed(fref, line_no, raw, reason)
                    continue
                ts = self._row_time(row)
                if ts is None:
                    self._note_unparsed(fref, line_no, raw, "no time column")
                    continue
                last_ts = ts
                if from_ is not None and ts < from_:
                    continue
                if until is not None and ts >= until:
                    continue
                if (skip_at_start and skipped < skip_at_start
                        and from_ is not None and ts == from_):
                    skipped += 1
                    continue
                self.parsed_rows += 1
                rows.append(row)
                if len(rows) >= chunksize:
                    yield pd.DataFrame(rows, columns=names)
                    rows = []
        if skip_at_start and skipped < skip_at_start:
            # Worth saying out loud: it means fact holds rows on that
            # timestamp the file no longer offers, so the resume could not be
            # made exact and a duplicate may have slipped through.
            logger.warning(
                "log source: crash-resume expected to skip %d record(s) at %s "
                "but only found %d -- the file may have been rewritten rather "
                "than appended to", skip_at_start, from_, skipped)
        if rows:
            yield pd.DataFrame(rows, columns=names)

    def _row_time(self, row: dict) -> Optional[datetime]:
        for col in self.config.columns:
            if col.role == "time" or col.group == self.parser.time_group:
                return row.get(col.name)
        return None

    def _touches_window(self, fref: FileRef,
                        from_: Optional[datetime]) -> bool:
        if from_ is None or fref.mtime is None:
            return True
        return fref.mtime >= from_ - timedelta(seconds=MTIME_SKEW_S)

    def _note_unparsed(self, fref: FileRef, line_no: int, raw: str,
                       reason: Optional[str]) -> None:
        self.unparsed_rows += 1
        if len(self.unparsed) < UNPARSED_MAX:
            self.unparsed.append(UnparsedLine(
                file_name=fref.key, line_no=line_no,
                raw_line=raw.rstrip("\n")[:UNPARSED_LINE_CHARS],
                reason=reason or "no match"))

    def _iter_records(self, fref: FileRef) -> Iterator[tuple[int, str]]:
        """Records, joining continuation lines when configured.

        Without ``log_record_start`` a line is a record, which is every
        access-log format. With it, a line that does not start a record is
        appended to the one before -- that is what keeps a multi-line stack
        trace as a single row instead of N unparsed lines.
        """
        start_re = self.parser.record_start_re
        with fref.open_text(self.config.encoding) as fh:
            if start_re is None:
                for i, raw in enumerate(fh, start=1):
                    if raw.strip():
                        yield i, raw.rstrip("\n")
                return
            buf: list[str] = []
            buf_line = 0
            for i, raw in enumerate(fh, start=1):
                line = raw.rstrip("\n")
                if start_re.search(line):
                    if buf:
                        yield buf_line, "\n".join(buf)
                    buf, buf_line = [line], i
                elif buf:
                    buf.append(line)
                elif line.strip():
                    # An orphan before the first record start: report it
                    # rather than swallow it, because a record_start that
                    # matches nothing looks exactly like this.
                    yield i, line
            if buf:
                yield buf_line, "\n".join(buf)


# ---------------------------------------------------------------------------
# The GUI's probe
# ---------------------------------------------------------------------------


@dataclass
class GroupReport:
    """One named group as the column generator offers it."""
    group: str
    samples: list[str]
    distinct: int
    distinct_after: Optional[int]     # after the transform, when one is set
    pg_type: str                      # the FACT column's type to create
    value_type: str                   # what the raw values look like
    role: str
    high_cardinality: bool
    # Sampled values that are not numbers although the group is numeric —
    # a format's "no value" token. Each one needs a transform or its line
    # fails the cast. 0 for a text group.
    non_numeric: int = 0
    # The fact column this group would become. Differs from `group` when the
    # group name is not usable as an unquoted Postgres column name.
    column_name: str = ""
    name_note: Optional[str] = None


@dataclass
class ProbeReport:
    files: list[dict]
    lines_read: int
    matched: int
    match_pct: Optional[float]
    groups: list[GroupReport]
    unmatched: list[dict]
    time_group: Optional[str]


# The share of read lines above which a group's distinct count makes it
# unsuitable as an auto-dimension without a transform. A dim table holds 32767
# values; a group that is nearly unique per line reaches that in minutes.
HIGH_CARDINALITY_SHARE = 0.5

# When a *numeric* group is proposed as a dimension rather than a measure.
# The useful signal is not the absolute count but the **share**: a code
# repeats, a quantity does not. An HTTP status takes a handful of values over
# any number of lines; a byte count is close to unique per line. Both tests
# have to hold — the share alone would call a 400-distinct field over 10 000
# lines a dimension, and the absolute cap alone misreads a small sample (a
# 20-line probe where every byte count differs has 20 distinct values, which
# any fixed cap of 20 would accept).
NUMERIC_DIMENSION_DISTINCT = int(
    os.environ.get("STARCUBES_LOG_NUMERIC_DIM_DISTINCT") or 50)
NUMERIC_DIMENSION_SHARE = 0.25

PROBE_LINES = int(os.environ.get("STARCUBES_LOG_PROBE_LINES") or 500)
# How far back the probe's window reaches on a transport that takes one
# (`exec://`). Wide enough that a quiet log still has lines in it, narrow
# enough that the command returns inside a request.
PROBE_WINDOW_S = int(os.environ.get("STARCUBES_LOG_PROBE_WINDOW_S") or 86400)
PROBE_BYTES = int(os.environ.get("STARCUBES_LOG_PROBE_BYTES") or 1_000_000)
PROBE_SECONDS = float(os.environ.get("STARCUBES_LOG_PROBE_SECONDS") or 10.0)


# Values a log format writes where it has no value. A group is still numeric
# with these in it — that is the whole reason `transform_pattern` exists, and
# a guesser that gives up on the first one calls nginx's `bytes` a
# high-cardinality *dimension*, which is the opposite of useful.
_NULL_TOKENS = {"", "-", "--", "n/a", "na", "null", "none", "nil", "?"}

# How much of a sample must parse as a number before the group is called
# numeric. Below it the field really is text with digits in it.
NUMERIC_SHARE = 0.9


# PostgreSQL reserved words that cannot be an unquoted column name. Not the
# full keyword list — only what a log field plausibly collides with, plus
# everything `pg_get_keywords()` marks reserved that is a single common word.
# `user` is the one that matters and the one that found this: nginx's combined
# format has a `user` field, and `core/schema.py:create_fact` interpolates
# column names unquoted, so the CREATE TABLE fails with "syntax error at or
# near user" *after* the datasource was configured.
_RESERVED_COLUMN_NAMES = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
    "asymmetric", "both", "case", "cast", "check", "collate", "column",
    "constraint", "create", "current_catalog", "current_date", "current_role",
    "current_time", "current_timestamp", "current_user", "default",
    "deferrable", "desc", "distinct", "do", "else", "end", "except", "false",
    "fetch", "for", "foreign", "from", "grant", "group", "having", "in",
    "initially", "intersect", "into", "lateral", "leading", "limit",
    "localtime", "localtimestamp", "not", "null", "offset", "on", "only",
    "or", "order", "placing", "primary", "references", "returning", "select",
    "session_user", "some", "symmetric", "table", "then", "to", "trailing",
    "true", "union", "unique", "user", "using", "variadic", "when", "where",
    "window", "with",
}

# The shape a fact column name must have. Same restriction and the same reason
# as `core/schema.py:validate_schema_name`: the name goes unquoted into the
# warehouse DDL and every query built around it, so it has to survive
# Postgres case-folding unchanged. Quoting ~70 f-string sites instead was
# evaluated and rejected — see docs/backlog.md.
_SAFE_COLUMN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def safe_column_name(group: str) -> tuple[str, Optional[str]]:
    """``(column_name, note)`` — a fact column name derived from a group name.

    A named group may legitimately be spelled in a way a Postgres column
    cannot: upper case (folded), reserved (`user`), or carrying a character an
    unquoted identifier does not allow. The fact column's *name* and its
    *source expression* are separate fields, so the fix is to adjust the name
    and leave the group untouched — nothing has to be renamed in the regex.

    `note` is None when the group name was already usable, and otherwise says
    what changed, so the probe can show it rather than silently substituting.
    """
    name = re.sub(r"[^a-z0-9_]", "_", group.lower())
    if not name or name[0].isdigit():
        name = "c_" + name
    name = name[:63]
    reason = None
    if name in _RESERVED_COLUMN_NAMES:
        name += "_"
        reason = (f"{group!r} is a reserved word in PostgreSQL and cannot be "
                  f"an unquoted column name")
    elif name != group:
        reason = (f"{group!r} is not usable as a column name unchanged "
                  f"(lower case, letters, digits and underscore only)")
    if reason:
        reason += f" — the column is called {name!r}, the group stays {group!r}"
    return name, reason


def _guess_type(values: list[str]) -> tuple[str, int]:
    """``(pg_type, non_numeric_count)`` for a group, from its values.

    Integers get the narrowest type that holds the sample, which is what
    makes `status` a smallint and `bytes` a bigint without anyone saying so.

    The second element is what makes the guess useful rather than merely
    right: a group that is numeric *except* for a handful of `-` placeholders
    is still a measure, and the count is how the probe tells the operator to
    set a transform — without one, every such line fails its cast and lands
    in `etl_unparsed`. Measured on a real nginx log, where one request in
    fifty sends no body.
    """
    if not values:
        return "text", 0
    lo, hi = 0, 0
    n_int = n_float = n_bad = 0
    for v in values:
        t = v.strip()
        if t.lower() in _NULL_TOKENS:
            n_bad += 1
            continue
        try:
            n = int(t)
            lo, hi = min(lo, n), max(hi, n)
            n_int += 1
            continue
        except ValueError:
            pass
        try:
            float(t)
            n_float += 1
        except ValueError:
            n_bad += 1
    numeric = n_int + n_float
    if not numeric or numeric < NUMERIC_SHARE * len(values):
        return "text", 0
    if n_float:
        return "double precision", n_bad
    if -32768 <= lo and hi <= 32767:
        return "smallint", n_bad
    if -2147483648 <= lo and hi <= 2147483647:
        return "integer", n_bad
    return "bigint", n_bad


def _looks_like_time(values: list[str], fmt: Optional[str],
                     tz: ZoneInfo) -> bool:
    for v in values[:5]:
        try:
            _parse_time(v, fmt, tz, None)
            return True
        except Exception:
            continue
    return False


def sample(config: LogConfig, n_lines: int = PROBE_LINES) -> ProbeReport:
    """Read a little, match it, and say what the columns would be.

    This is what stands in for the schema introspection a SQL source never
    got: the operator pastes a regex, and the answer is the match rate, every
    named group with example values, a guessed type and a proposed role, and
    the lines that did not match.

    **Bounded three ways** -- lines, bytes and wall clock -- because it runs
    inside a web request and uwsgi's `harakiri = 120` kills the whole worker,
    not just the request. It reads the tail of the newest generation, which is
    both the cheapest sample and the most representative one.
    """
    import time as _time
    deadline = _time.monotonic() + PROBE_SECONDS
    line_re = compile_line_regex(config.line_regex)

    if not line_re.groupindex:
        raise LogSourceError(
            "log_line_regex has no named groups. The fact columns are read "
            "from them, so the pattern needs at least a time group, e.g. "
            r"(?P<ts>\d{4}-\d\d-\d\dT\S+)")

    transport = open_transport(config.uri)
    try:
        return _sample_with(config, transport, line_re, n_lines, deadline)
    finally:
        # One exit rather than three: the two early returns below used to slip
        # past the close, which on `sftp://` leaks an SSH connection per probe.
        try:
            transport.close()
        except Exception:
            logger.debug("closing the probe transport failed", exc_info=True)


def _sample_with(config, transport, line_re, n_lines, deadline) -> ProbeReport:
    """The body of `sample`, so the transport has exactly one close."""
    import time as _time
    # A recent window, not an unbounded one: on `exec://` the placeholders are
    # what keep the probe from reading a week of journald inside a web request.
    # The globbing transports ignore it and the tail sampling below bounds
    # them instead.
    now = datetime.now(timezone.utc)
    files = sorted(transport.list_files(now - timedelta(seconds=PROBE_WINDOW_S),
                                        now),
                   key=lambda f: (f.mtime or datetime.min.replace(
                       tzinfo=timezone.utc), f.key))
    file_rows = [{"name": f.key, "size": f.size,
                  "mtime": f.mtime.isoformat() if f.mtime else None}
                 for f in files]
    if not files:
        return ProbeReport(files=[], lines_read=0, matched=0, match_pct=None,
                           groups=[], unmatched=[],
                           time_group=config.time_group or None)

    start_re = None
    if (config.record_start or "").strip():
        start_re = compile_line_regex(config.record_start, "log_record_start")

    values: dict[str, list[str]] = {g: [] for g in line_re.groupindex}
    seen: dict[str, set] = {g: set() for g in line_re.groupindex}
    unmatched: list[dict] = []
    lines_read = matched = nbytes = 0

    for fref in reversed(files):           # newest first
        lines = fref.seekable_tail(config.encoding, PROBE_BYTES)
        stream = iter(lines) if lines is not None else None
        fh = None
        if stream is None:
            fh = fref.open_text(config.encoding)
            stream = fh
        try:
            buf: list[str] = []
            for raw in stream:
                line = raw.rstrip("\n")
                if start_re is not None:
                    if start_re.search(line):
                        if buf:
                            lines_read, matched, nbytes = _probe_one(
                                "\n".join(buf), line_re, values, seen,
                                unmatched, fref, lines_read, matched, nbytes)
                        buf = [line]
                        continue
                    if buf:
                        buf.append(line)
                        continue
                if not line.strip():
                    continue
                lines_read, matched, nbytes = _probe_one(
                    line, line_re, values, seen, unmatched, fref,
                    lines_read, matched, nbytes)
                if (lines_read >= n_lines or nbytes >= PROBE_BYTES
                        or _time.monotonic() > deadline):
                    break
            if buf and lines_read < n_lines:
                lines_read, matched, nbytes = _probe_one(
                    "\n".join(buf), line_re, values, seen, unmatched, fref,
                    lines_read, matched, nbytes)
        finally:
            if fh is not None:
                fh.close()
        if (lines_read >= n_lines or nbytes >= PROBE_BYTES
                or _time.monotonic() > deadline):
            break

    tz = _resolve_zone(config.time_zone)
    by_name = {c.group: c for c in config.columns if c.group}
    groups: list[GroupReport] = []
    time_group = config.time_group or None
    for g in line_re.groupindex:
        vals = values[g]
        distinct = len(seen[g])
        spec = by_name.get(g)
        tf = None
        if spec is not None and (spec.transform_pattern or "").strip():
            tf = _Transform(spec.transform_pattern, spec.transform_replace)
        # Over `seen[g]`, the full distinct set — **not** over `vals`, which
        # is capped at 50 examples. Computing it from the samples made the two
        # numbers incomparable and the pane reported nonsense ("295 → 50 after
        # transform" on a transform that collapses nothing).
        distinct_after = (len({tf.apply(v) for v in seen[g]})
                          if tf else None)
        is_time = (g == time_group) if time_group else _looks_like_time(
            vals, config.time_format, tz)
        if is_time and time_group is None:
            time_group = g
        if is_time:
            value_type, non_numeric = "timestamptz", 0
        else:
            value_type, non_numeric = _guess_type(vals)
        effective_distinct = (distinct_after if distinct_after is not None
                              else distinct)
        if is_time:
            role = "time"
        elif value_type == "text":
            role = "auto-dimension"
        elif (effective_distinct <= NUMERIC_DIMENSION_DISTINCT
              and effective_distinct
                  <= NUMERIC_DIMENSION_SHARE * max(lines_read, 1)):
            # Numeric, but repeating: a code, not a quantity. "Numeric
            # therefore a measure" gets an HTTP status wrong in a way that
            # matters — nobody sums status codes, they group by them, and a
            # measure cannot be grouped by. The distinctness is already
            # counted, so this costs nothing.
            role = "auto-dimension"
        else:
            role = "measure"
        # The fact column's type is not the value's type, and it differs in
        # two opposite directions.
        #
        # A **dimension** holds the dim table's `smallserial` index, so it is
        # smallint whatever the source value looks like. Getting that wrong
        # produces a text dimension column, which then fails the dim count
        # UPDATE with `operator does not exist: smallint = text` after a load
        # has already succeeded — measured, on the first real end-to-end run.
        #
        # A **measure** is never proposed as smallint, even when the sample
        # fits one. The sample's maximum is only a lower bound on the real
        # maximum, and `core/etl.py:_transform_worker` *clips* an
        # out-of-range measure instead of failing — so an underestimate here
        # is silent corruption rather than a loud error. A 1 kB response body
        # in a 300-line sample says nothing about the 100 kB one next week.
        # Two bytes of storage against that is not a trade worth making; the
        # operator can narrow it deliberately, and the value's own shape is
        # still reported as `value_type`.
        if role == "auto-dimension":
            pg_type = "smallint"
        elif role == "measure" and value_type == "smallint":
            pg_type = "integer"
        else:
            pg_type = value_type
        col_name, name_note = safe_column_name(g)
        effective = distinct_after if distinct_after is not None else distinct
        groups.append(GroupReport(
            group=g, samples=vals[:5], distinct=distinct,
            distinct_after=distinct_after, pg_type=pg_type,
            value_type=value_type, role=role, non_numeric=non_numeric,
            column_name=col_name, name_note=name_note,
            high_cardinality=(role == "auto-dimension" and lines_read > 0
                              and effective >= HIGH_CARDINALITY_SHARE * lines_read),
        ))

    return ProbeReport(
        files=file_rows, lines_read=lines_read, matched=matched,
        match_pct=(100.0 * matched / lines_read) if lines_read else None,
        groups=groups, unmatched=unmatched[:20], time_group=time_group)


def _probe_one(line, line_re, values, seen, unmatched, fref,
               lines_read, matched, nbytes):
    lines_read += 1
    nbytes += len(line)
    m = line_re.search(line)
    if m is None:
        if len(unmatched) < 20:
            unmatched.append({"file": fref.key,
                              "line": line[:UNPARSED_LINE_CHARS]})
        return lines_read, matched, nbytes
    matched += 1
    for g, v in m.groupdict().items():
        if v is None:
            continue
        if len(values[g]) < 50:
            values[g].append(v)
        if len(seen[g]) <= 100000:
            seen[g].add(v)
    return lines_read, matched, nbytes
