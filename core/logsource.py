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
    """A log source is misconfigured or unreachable.

    Raised for conditions an operator has to fix -- an unreadable path, a
    regex that does not compile, a transport a guard refuses. Never raised
    for a line that does not match: that is counted, not fatal.
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
        fh = self.opener()
        try:
            start = max(0, self.size - nbytes)
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

    def __init__(self, uri: str):
        parts = urlsplit(uri)
        if parts.netloc and parts.netloc != "localhost":
            raise LogSourceError(
                f"file:// URI with a host ({parts.netloc!r}) -- a local path "
                f"is spelled file:///var/log/... with three slashes")
        self.pattern = unquote(parts.path)
        if not self.pattern:
            raise LogSourceError("file:// URI has no path")

    def list_files(self) -> list[FileRef]:
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


_TRANSPORTS = {"file": LocalGlobTransport}


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

    def list_files(self) -> list[FileRef]:
        """Every generation, oldest first.

        Sorted by mtime and then by name: mtime alone leaves ties, and the
        order has to be deterministic for the crash-resume skip count to mean
        anything. A transport that cannot report mtime sorts first by name.
        """
        files = self.transport.list_files()
        return sorted(files, key=lambda f: (f.mtime or datetime.min.replace(
            tzinfo=timezone.utc), f.key))

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
        files = self.list_files()
        if not files:
            return None, src_now
        newest = files[-1]
        tip = self._tip_of(newest)
        if tip is None and len(files) > 1:
            # An empty or unparsable newest generation is normal right after
            # a rotation: the new file exists and holds nothing yet.
            tip = self._tip_of(files[-2])
        return tip, src_now

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
        for fref in self.list_files():
            if not self._touches_window(fref, from_):
                logger.debug("log source: skipping %s (mtime %s before window)",
                             fref.key, fref.mtime)
                continue
            self.files_read += 1
            for line_no, raw in self._iter_records(fref):
                row, reason = self.parser.parse(raw, fref.mtime)
                if row is None:
                    self._note_unparsed(fref, line_no, raw, reason)
                    continue
                ts = self._row_time(row)
                if ts is None:
                    self._note_unparsed(fref, line_no, raw, "no time column")
                    continue
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
    pg_type: str
    role: str
    high_cardinality: bool


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

PROBE_LINES = int(os.environ.get("STARCUBES_LOG_PROBE_LINES") or 500)
PROBE_BYTES = int(os.environ.get("STARCUBES_LOG_PROBE_BYTES") or 1_000_000)
PROBE_SECONDS = float(os.environ.get("STARCUBES_LOG_PROBE_SECONDS") or 10.0)


def _guess_type(values: list[str]) -> str:
    """A pg_type for a group, from what its values look like.

    Integers get the narrowest type that holds the sample, which is what
    makes `status` a smallint and `bytes` a bigint without anyone saying so.
    """
    if not values:
        return "text"
    ints, floats = True, True
    lo, hi = 0, 0
    for v in values:
        s = v.strip()
        if not s:
            continue
        try:
            n = int(s)
            lo, hi = min(lo, n), max(hi, n)
        except ValueError:
            ints = False
            try:
                float(s)
            except ValueError:
                floats = False
                break
    if ints:
        if -32768 <= lo and hi <= 32767:
            return "smallint"
        if -2147483648 <= lo and hi <= 2147483647:
            return "integer"
        return "bigint"
    if floats:
        return "double precision"
    return "text"


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
    files = sorted(transport.list_files(),
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
        distinct_after = (len({tf.apply(v) for v in vals}) if tf else None)
        is_time = (g == time_group) if time_group else _looks_like_time(
            vals, config.time_format, tz)
        if is_time and time_group is None:
            time_group = g
        pg_type = "timestamptz" if is_time else _guess_type(vals)
        if is_time:
            role = "time"
        elif pg_type == "text":
            role = "auto-dimension"
        else:
            role = "measure"
        effective = distinct_after if distinct_after is not None else distinct
        groups.append(GroupReport(
            group=g, samples=vals[:5], distinct=distinct,
            distinct_after=distinct_after, pg_type=pg_type, role=role,
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
