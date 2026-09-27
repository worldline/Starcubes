"""Measure the log reader against fixtures whose right answer is known.

The counterpart of ``check_stream_stats``, and it exists for the same reason:
**every failure mode in here is silent.** A timezone applied in the wrong
direction, a year taken from the wrong end of a December/January boundary, a
sub-second watermark rounded up, a transform that never fires -- none of them
raises, none of them shows up as an error in a run log, and all of them
produce a chart that looks entirely plausible and is wrong. Reading the code
cannot tell you which way round an offset went; only a fixture whose correct
answer was computed independently can.

Needs no source database and no warehouse: it writes log files into a
temporary directory and drives ``core/logsource.py`` over them directly.

Run it after touching ``core/logsource.py``, and extend it rather than
trusting a manual check when adding a transport or a time format.
"""
from __future__ import annotations

import gzip
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.logsource import (ColumnSpec, LogConfig, LogReader, sample)

UTC = timezone.utc


def _utc(text: str) -> datetime:
    """A hand-written expected value, e.g. '2026-09-27 06:12:01'."""
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(text, fmt).replace(tzinfo=UTC)


def _write(path: Path, lines, mtime: datetime | None = None,
           gz: bool = False) -> Path:
    body = "".join(l + "\n" for l in lines)
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(body)
    else:
        path.write_text(body, encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


# ---------------------------------------------------------------------------
# Shared fixture shapes
# ---------------------------------------------------------------------------

NGINX_RE = (r'^(?P<host>\S+) \S+ (?P<user>\S+) \[(?P<ts>[^\]]+)\] '
            r'"(?P<method>\S+) (?P<path>\S+) [^"]*" '
            r'(?P<status>\d{3}) (?P<bytes>\S+)')
NGINX_TS = "%d/%b/%Y:%H:%M:%S %z"


def _nginx_cols(with_transform: bool = False) -> list[ColumnSpec]:
    # Note the dimension columns' pg_type: **smallint**, not text. A
    # dimension fact column holds the dim table's smallserial index, not the
    # source value — the same shape the shipped oracle_payments profile uses
    # (`pg_type: smallint` with a text-producing `source_expr`). The fixtures
    # declare it that way on purpose, because declaring `text` is what hid a
    # real bug: the reader used to cast every column to its fact type, which
    # sends a hostname against a smallint straight to etl_unparsed.
    cols = [
        ColumnSpec("ts", "ts", "timestamptz", "time"),
        ColumnSpec("host", "host", "smallint", "auto-dimension"),
        ColumnSpec("path", "path", "smallint", "auto-dimension"),
        ColumnSpec("status", "status", "smallint", "measure"),
        ColumnSpec("bytes", "bytes", "bigint", "measure"),
    ]
    if with_transform:
        # The two transforms a real nginx profile needs, and nothing else:
        # the path collapsed to its first segment so it can be a dimension
        # at all, and '-' for bytes mapped onto a number so the cast works.
        cols[2] = ColumnSpec("path", "path", "smallint", "auto-dimension",
                             r"^(/[^/?]*).*$", r"\1")
        cols[4] = ColumnSpec("bytes", "bytes", "bigint", "measure",
                             r"^-$", "0")
    return cols


def _nginx_line(ts: str, host="10.0.0.4", path="/api/v1", status=200,
                nbytes="1043") -> str:
    return (f'{host} - - [{ts}] "GET {path} HTTP/1.1" '
            f'{status} {nbytes} "-" "curl/8"')


# ---------------------------------------------------------------------------
# Scenarios. Each returns a list of (label, ok, detail).
# ---------------------------------------------------------------------------


def s_offset_in_line(tmp: Path):
    """An offset in the line wins over log_time_zone, in both DST states."""
    d = tmp / "offset"
    d.mkdir()
    _write(d / "access.log", [
        _nginx_line("27/Sep/2026:08:12:01 +0200"),
        _nginx_line("15/Jan/2026:08:12:01 +0100"),
        _nginx_line("27/Sep/2026:06:12:01 +0000"),
    ], mtime=_utc("2026-09-27 07:00:00"))
    cfg = LogConfig(uri=f"file://{d}/access.log*", line_regex=NGINX_RE,
                    time_group="ts", time_format=NGINX_TS,
                    # deliberately a *different* zone: the line must win
                    time_zone="America/New_York", columns=_nginx_cols())
    rows = _collect(cfg)
    want = [_utc("2026-09-27 06:12:01"), _utc("2026-01-15 07:12:01"),
            _utc("2026-09-27 06:12:01")]
    got = sorted(r["ts"] for r in rows)
    return [("3 rows parsed", len(rows) == 3, f"{len(rows)}"),
            ("offset in line wins over log_time_zone",
             got == sorted(want), f"{got} vs {sorted(want)}")]


def s_zone_from_config(tmp: Path):
    """No offset in the line: log_time_zone applies, including over DST.

    The expected values were computed independently with zoneinfo and are
    written out here rather than derived, so the test cannot agree with a
    broken implementation by construction.
    """
    d = tmp / "zone"
    d.mkdir()
    fmt = "%Y-%m-%d %H:%M:%S"
    local = ["2026-09-27 08:12:01",   # CEST +2
             "2026-01-15 08:12:01",   # CET  +1
             "2026-03-29 01:30:00",   # minutes before the spring jump
             "2026-03-29 03:30:00",   # minutes after it
             "2026-10-25 01:30:00",   # still CEST
             "2026-10-25 04:30:00"]   # back on CET
    want = [_utc("2026-09-27 06:12:01"), _utc("2026-01-15 07:12:01"),
            _utc("2026-03-29 00:30:00"), _utc("2026-03-29 01:30:00"),
            _utc("2026-10-24 23:30:00"), _utc("2026-10-25 03:30:00")]
    _write(d / "app.log", [f"{t} INFO ok" for t in local],
           mtime=_utc("2026-10-26 00:00:00"))
    cfg = LogConfig(uri=f"file://{d}/app.log",
                    line_regex=r"^(?P<ts>\S+ \S+) (?P<lvl>\w+) (?P<msg>.*)$",
                    time_group="ts", time_format=fmt, time_zone="Europe/Berlin",
                    columns=[ColumnSpec("ts", "ts", "timestamptz", "time"),
                             ColumnSpec("lvl", "lvl", "text", "auto-dimension")])
    got = [r["ts"] for r in _collect(cfg)]
    out = [("6 rows parsed", len(got) == 6, f"{len(got)}")]
    for loc, exp, act in zip(local, want, got + [None] * 6):
        out.append((f"{loc} Berlin -> {exp:%H:%M} UTC", act == exp,
                    f"got {act}"))
    return out


def s_dst_ambiguous(tmp: Path):
    """The one hour that happens twice resolves to the first (fold=0).

    Not a correctness claim about the data -- a naive local timestamp in that
    hour is genuinely ambiguous and no parser can fix it. The check is that
    the behaviour is defined and documented rather than accidental.
    """
    d = tmp / "fold"
    d.mkdir()
    _write(d / "app.log", ["2026-10-25 02:30:00 INFO ok"],
           mtime=_utc("2026-10-25 06:00:00"))
    cfg = LogConfig(uri=f"file://{d}/app.log",
                    line_regex=r"^(?P<ts>\S+ \S+) (?P<lvl>\w+) (?P<msg>.*)$",
                    time_group="ts", time_format="%Y-%m-%d %H:%M:%S",
                    time_zone="Europe/Berlin",
                    columns=[ColumnSpec("ts", "ts", "timestamptz", "time"),
                             ColumnSpec("lvl", "lvl", "text", "auto-dimension")])
    got = [r["ts"] for r in _collect(cfg)]
    return [("ambiguous 02:30 -> 00:30 UTC (first pass, fold=0)",
             got == [_utc("2026-10-25 00:30:00")], f"{got}")]


def s_year_from_mtime(tmp: Path):
    """syslog RFC3164 has no year; December lines in a January file.

    The trap this exists for: taking the mtime's year unconditionally dates
    `Dec 31` eleven months into the *future*.
    """
    d = tmp / "syslog"
    d.mkdir()
    _write(d / "messages", [
        "Dec 31 23:59:00 host1 sshd[1]: accepted",
        "Jan 01 00:01:00 host1 sshd[2]: accepted",
        "Jan 02 09:00:00 host1 sshd[3]: accepted",
    ], mtime=_utc("2026-01-02 10:00:00"))
    cfg = LogConfig(
        uri=f"file://{d}/messages",
        line_regex=(r"^(?P<ts>\w{3}\s+\d+ \d\d:\d\d:\d\d) (?P<host>\S+) "
                    r"(?P<tag>[^\[\s:]+)"),
        time_group="ts", time_format="%b %d %H:%M:%S",
        time_zone="Europe/Berlin",
        columns=[ColumnSpec("ts", "ts", "timestamptz", "time"),
                 ColumnSpec("host", "host", "text", "auto-dimension"),
                 ColumnSpec("tag", "tag", "text", "auto-dimension")])
    got = [r["ts"] for r in _collect(cfg)]
    want = [_utc("2025-12-31 22:59:00"), _utc("2025-12-31 23:01:00"),
            _utc("2026-01-02 08:00:00")]
    return [("3 rows parsed", len(got) == 3, f"{len(got)}"),
            ("Dec 31 in a Jan file -> previous year",
             got[:1] == want[:1], f"{got[:1]} vs {want[:1]}"),
            ("Jan 01 in a Jan file -> mtime's year",
             got[1:2] == want[1:2], f"{got[1:2]} vs {want[1:2]}"),
            ("Jan 02 likewise", got[2:3] == want[2:3],
             f"{got[2:3]} vs {want[2:3]}")]


def s_subsecond_boundary(tmp: Path):
    """Two runs over a *growing* file must together load every line once.

    This is the test that has teeth about `_ceil_second`, and it only has
    them because the file grows between the runs: with the watermark rounded
    up to the whole second, run 1 stores a high watermark *past* the tip it
    measured, and the lines appended into that sliver are never loaded. On a
    static fixture both variants are green, which is why the append is the
    point of the scenario.

    The loss is demonstrated below as well as the correct behaviour asserted
    -- a test that cannot fail proves nothing.
    """
    d = tmp / "subsec"
    d.mkdir()
    log = d / "haproxy.log"
    base = "06/Feb/2026:12:14:14"
    first = [f"{base}.{ms:03d} req {i}" for i, ms in
             enumerate((100, 300, 500, 700, 900))]
    _write(log, first, mtime=_utc("2026-02-06 12:14:15"))
    cfg = LogConfig(uri=f"file://{log}",
                    line_regex=r"^(?P<ts>\S+) req (?P<n>\d+)$",
                    time_group="ts", time_format="%d/%b/%Y:%H:%M:%S.%f",
                    columns=[ColumnSpec("ts", "ts", "timestamptz", "time"),
                             ColumnSpec("n", "n", "integer", "measure")])

    # --- run 1: window ends at the measured tip, exclusive
    r1 = LogReader(cfg)
    tip1, _ = r1.probe_tip()
    rows1 = [r for c in r1.iter_chunks(None, tip1, 1000)
             for r in c.to_dict("records")]

    # --- the file grows: one more line inside the same second, one after
    _write(log, first + [f"{base}.950 req 5", "06/Feb/2026:12:14:15.100 req 6"],
           mtime=_utc("2026-02-06 12:14:16"))

    # --- run 2 resumes at the stored watermark
    r2 = LogReader(cfg)
    tip2, _ = r2.probe_tip()
    rows2 = [r for c in r2.iter_chunks(tip1, tip2, 1000)
             for r in c.to_dict("records")]

    got = [r["n"] for r in rows1] + [r["n"] for r in rows2]
    # 0..4 from run 1, then 5 (the appended .950) — 6 is the new tip and is
    # excluded by the half-open bound, so it is pending, not lost.
    ok_no_gap = sorted(got) == [0, 1, 2, 3, 4, 5]
    ok_no_dup = len(got) == len(set(got))

    # --- the same thing with a ceiled watermark, to show the test bites
    ceiled = tip1.replace(microsecond=0) + timedelta(seconds=1)
    r3 = LogReader(cfg)
    lost = [r["n"] for c in r3.iter_chunks(ceiled, tip2, 1000)
            for r in c.to_dict("records")]
    ceiling_loses = 5 not in lost

    return [("run 1 loads the 4 rows *below* the tip, not the tip itself",
             len(rows1) == 4, f"{len(rows1)}, tip={tip1}"),
            ("run 2 picks up the tip row and the appended one",
             len(rows2) == 2, f"{len(rows2)}"),
            ("tip carries milliseconds", tip1 == _utc("2026-02-06 12:14:14.900"),
             f"{tip1}"),
            ("two runs cover every line exactly once", ok_no_gap,
             f"{sorted(got)}"),
            ("no duplicates across the boundary", ok_no_dup, f"{got}"),
            ("a ceiled watermark would lose the .950 line "
             "(the test has teeth)", ceiling_loses, f"ceiled={ceiled} got={lost}")]


def s_transform(tmp: Path):
    """The transform collapses cardinality and rescues the cast."""
    d = tmp / "transform"
    d.mkdir()
    lines = [_nginx_line("27/Sep/2026:08:00:0%d +0000" % (i % 10),
                         path=f"/tiles/16/{34000 + i}/{22000 + i}.png",
                         nbytes="-" if i % 5 == 0 else str(1000 + i))
             for i in range(10)]
    _write(d / "access.log", lines, mtime=_utc("2026-09-27 09:00:00"))
    uri = f"file://{d}/access.log"

    plain = LogConfig(uri=uri, line_regex=NGINX_RE, time_group="ts",
                      time_format=NGINX_TS, columns=_nginx_cols(False))
    tfd = LogConfig(uri=uri, line_regex=NGINX_RE, time_group="ts",
                    time_format=NGINX_TS, columns=_nginx_cols(True))

    r_plain = LogReader(plain)
    rows_plain = [r for c in r_plain.iter_chunks(None, None, 1000)
                  for r in c.to_dict("records")]
    r_tf = LogReader(tfd)
    rows_tf = [r for c in r_tf.iter_chunks(None, None, 1000)
               for r in c.to_dict("records")]

    # Without the bytes transform, '-' cannot become a bigint: those lines
    # are unparsed, which is the failure the transform exists to prevent.
    return [("untransformed: '-' bytes fail the cast",
             r_plain.unparsed_rows == 2, f"{r_plain.unparsed_rows} unparsed"),
            ("untransformed: reason names the column",
             bool(r_plain.unparsed) and r_plain.unparsed[0].reason.startswith("bytes:"),
             r_plain.unparsed[0].reason if r_plain.unparsed else "-"),
            ("transformed: every line parses", r_tf.unparsed_rows == 0
             and len(rows_tf) == 10, f"{len(rows_tf)} rows, "
             f"{r_tf.unparsed_rows} unparsed"),
            ("transformed: '-' became 0",
             sorted(r["bytes"] for r in rows_tf)[:2] == [0, 0],
             f"{sorted(r['bytes'] for r in rows_tf)[:3]}"),
            ("transformed: 10 tile paths collapse to 1 value",
             len({r["path"] for r in rows_tf}) == 1,
             f"{sorted({r['path'] for r in rows_tf})}"),
            ("untransformed: 10 distinct paths (the problem)",
             len({r["path"] for r in rows_plain}) == 8,
             f"{len({r['path'] for r in rows_plain})} distinct over "
             f"{len(rows_plain)} rows")]


def s_rotation_and_gzip(tmp: Path):
    """The glob spans generations, .gz is read, and mtime prefilters."""
    d = tmp / "rot"
    d.mkdir()
    _write(d / "access.log", [_nginx_line("27/Sep/2026:08:00:00 +0000")],
           mtime=_utc("2026-09-27 08:30:00"))
    _write(d / "access.log.1", [_nginx_line("26/Sep/2026:08:00:00 +0000")],
           mtime=_utc("2026-09-27 00:00:00"))
    _write(d / "access.log.2.gz", [_nginx_line("25/Sep/2026:08:00:00 +0000")],
           mtime=_utc("2026-09-26 00:00:00"), gz=True)
    _write(d / "access.log.3.gz", [_nginx_line("20/Sep/2026:08:00:00 +0000")],
           mtime=_utc("2026-09-21 00:00:00"), gz=True)
    cfg = LogConfig(uri=f"file://{d}/access.log*", line_regex=NGINX_RE,
                    time_group="ts", time_format=NGINX_TS,
                    columns=_nginx_cols())

    r_all = LogReader(cfg)
    all_rows = [r for c in r_all.iter_chunks(None, None, 1000)
                for r in c.to_dict("records")]

    # A window starting 2026-09-26 12:00 must not open the .3.gz generation
    # (mtime 09-21) at all. MTIME_SKEW_S of slack still excludes it.
    r_win = LogReader(cfg)
    win_rows = [r for c in r_win.iter_chunks(_utc("2026-09-26 12:00:00"),
                                             None, 1000)
                for r in c.to_dict("records")]

    return [("all four generations read, .gz included",
             len(all_rows) == 4 and r_all.files_read == 4,
             f"{len(all_rows)} rows from {r_all.files_read} files"),
            ("oldest timestamp came out of a .gz",
             min(r["ts"] for r in all_rows) == _utc("2026-09-20 08:00:00"),
             f"{min(r['ts'] for r in all_rows)}"),
            ("mtime prefilter skips generations before the window",
             r_win.files_read == 2, f"opened {r_win.files_read} files"),
            ("and loses nothing inside it", len(win_rows) == 1,
             f"{len(win_rows)} rows"),
            ("min_watermark reads the oldest generation's head",
             LogReader(cfg).min_watermark() == _utc("2026-09-20 08:00:00"),
             f"{LogReader(cfg).min_watermark()}")]


def s_multiline(tmp: Path):
    """A stack trace stays one record instead of N unparsed lines."""
    d = tmp / "multi"
    d.mkdir()
    _write(d / "app.log", [
        "2026-09-27 08:00:00 INFO started",
        "2026-09-27 08:00:01 ERROR boom",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: nope",
        "2026-09-27 08:00:02 INFO carried on",
    ], mtime=_utc("2026-09-27 09:00:00"))
    line_re = r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (?P<lvl>\w+) (?P<msg>.*)$"
    cols = [ColumnSpec("ts", "ts", "timestamptz", "time"),
            ColumnSpec("lvl", "lvl", "text", "auto-dimension")]

    without = LogConfig(uri=f"file://{d}/app.log", line_regex=line_re,
                        time_group="ts", time_format="%Y-%m-%d %H:%M:%S",
                        columns=cols)
    withrs = LogConfig(uri=f"file://{d}/app.log", line_regex=line_re,
                       time_group="ts", time_format="%Y-%m-%d %H:%M:%S",
                       record_start=r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ",
                       columns=cols)
    r_w = LogReader(without)
    rows_w = _drain(r_w)
    r_r = LogReader(withrs)
    rows_r = _drain(r_r)
    return [("without record_start: 3 rows, 3 unparsed trace lines",
             len(rows_w) == 3 and r_w.unparsed_rows == 3,
             f"{len(rows_w)} rows / {r_w.unparsed_rows} unparsed"),
            ("with record_start: 3 rows, nothing unparsed",
             len(rows_r) == 3 and r_r.unparsed_rows == 0,
             f"{len(rows_r)} rows / {r_r.unparsed_rows} unparsed"),
            ("match_pct reflects it", r_w.match_pct == 50.0
             and r_r.match_pct == 100.0,
             f"{r_w.match_pct} / {r_r.match_pct}")]


def s_unparsed_and_rate(tmp: Path):
    """Broken lines are counted, sampled and capped."""
    d = tmp / "bad"
    d.mkdir()
    good = [_nginx_line(f"27/Sep/2026:08:00:{i:02d} +0000") for i in range(9)]
    _write(d / "access.log", good + ['"-" - - [garbage'],
           mtime=_utc("2026-09-27 09:00:00"))
    cfg = LogConfig(uri=f"file://{d}/access.log", line_regex=NGINX_RE,
                    time_group="ts", time_format=NGINX_TS,
                    columns=_nginx_cols())
    r = LogReader(cfg)
    rows = _drain(r)
    u = r.unparsed[0] if r.unparsed else None
    return [("9 of 10 lines parsed", len(rows) == 9 and r.unparsed_rows == 1,
             f"{len(rows)} / {r.unparsed_rows}"),
            ("match_pct = 90.0", r.match_pct == 90.0, f"{r.match_pct}"),
            ("the sample names file and line",
             u is not None and u.file_name == "access.log" and u.line_no == 10,
             f"{u.file_name}:{u.line_no}" if u else "-"),
            ("and the reason", u is not None and u.reason == "no match",
             u.reason if u else "-")]


def s_unparsed_not_recounted(tmp: Path):
    """A quiet incremental pass must not re-count old malformed lines.

    Found on the first end-to-end run, and it is worse than it looks. An
    unparsed record has no timestamp, so the window filter cannot reach it —
    and counting it anyway made a pass with nothing to do report
    `0 parsed, 2 unparsed (0.00% match)`, append the same two lines to
    etl_unparsed again, and, with `log_min_match_pct` set, fail every run
    that legitimately had no new data.
    """
    d = tmp / "recount"
    d.mkdir()
    lines = [_nginx_line(f"27/Sep/2026:08:00:{i:02d} +0000") for i in range(10)]
    lines.insert(3, "broken early")
    lines.insert(9, "broken late")
    _write(d / "access.log", lines, mtime=_utc("2026-09-27 09:00:00"))
    cfg = LogConfig(uri=f"file://{d}/access.log", line_regex=NGINX_RE,
                    time_group="ts", time_format=NGINX_TS,
                    columns=_nginx_cols())

    first = LogReader(cfg)
    rows1 = _drain(first)

    # A pass whose window starts past everything in the file: nothing to do.
    quiet = LogReader(cfg)
    rows2 = [r for c in quiet.iter_chunks(_utc("2026-09-27 09:00:00"), None,
                                          1000) for r in c.to_dict("records")]

    # And one resuming mid-file: only the malformed line inside the window.
    mid = LogReader(cfg)
    [c for c in mid.iter_chunks(_utc("2026-09-27 08:00:06"), None, 1000)]

    return [("first pass sees both malformed lines",
             len(rows1) == 10 and first.unparsed_rows == 2,
             f"{len(rows1)} rows / {first.unparsed_rows} unparsed"),
            ("a quiet pass counts neither", len(rows2) == 0
             and quiet.unparsed_rows == 0 and quiet.match_pct is None,
             f"{quiet.unparsed_rows} unparsed, match_pct={quiet.match_pct}"),
            ("match_pct is None, not 0.0, when nothing was read",
             quiet.match_pct is None, f"{quiet.match_pct}"),
            ("a mid-file resume counts only the one inside its window",
             mid.unparsed_rows == 1,
             f"{mid.unparsed_rows}: "
             f"{[u.raw_line for u in mid.unparsed]}")]


def s_skip_at_start(tmp: Path):
    """The crash-resume count-dedupe, which stands in for a primary key."""
    d = tmp / "resume"
    d.mkdir()
    # Three records share one timestamp: exactly the case a PK would cover.
    _write(d / "access.log", [
        _nginx_line("27/Sep/2026:08:00:00 +0000", path="/a"),
        _nginx_line("27/Sep/2026:08:00:01 +0000", path="/b"),
        _nginx_line("27/Sep/2026:08:00:01 +0000", path="/c"),
        _nginx_line("27/Sep/2026:08:00:01 +0000", path="/d"),
        _nginx_line("27/Sep/2026:08:00:02 +0000", path="/e"),
    ], mtime=_utc("2026-09-27 09:00:00"))
    cfg = LogConfig(uri=f"file://{d}/access.log", line_regex=NGINX_RE,
                    time_group="ts", time_format=NGINX_TS,
                    columns=_nginx_cols())
    at = _utc("2026-09-27 08:00:01")
    # A crash left /b and /c in fact; the watermark rebuilt from fact is
    # 08:00:01 and fact holds 2 rows on it, so 2 records must be skipped.
    rows = [r["path"] for c in LogReader(cfg).iter_chunks(at, None, 1000, 2)
            for r in c.to_dict("records")]
    none_skipped = [r["path"] for c in LogReader(cfg).iter_chunks(at, None, 1000)
                    for r in c.to_dict("records")]
    return [("without skip: the duplicate siblings come back",
             none_skipped == ["/b", "/c", "/d", "/e"], f"{none_skipped}"),
            ("skip_at_start=2 resumes exactly after them",
             rows == ["/d", "/e"], f"{rows}"),
            ("later timestamps are never skipped", "/e" in rows, f"{rows}")]


def s_dimension_stays_raw(tmp: Path):
    """A dimension column reaches the DataFrame uncast, though it is smallint.

    The trap, found on the first real end-to-end run. A dimension fact column
    is `smallint` because it stores the dim table's surrogate index — but the
    value the extract produces is still the *source* value, and
    `core/etl.py:_resolve_auto_dim_chunk` is what turns it into the index,
    in the parent, on the chunk. A reader that casts to the fact column's
    type sends every hostname and URL path to etl_unparsed and leaves the
    dimension empty.

    Declaring the fixture columns `text` hid this completely, which is why
    every fixture here now declares them the way a real profile does.
    """
    d = tmp / "rawdim"
    d.mkdir()
    _write(d / "access.log",
           [_nginx_line(f"27/Sep/2026:08:00:{i:02d} +0000",
                        host=f"10.0.0.{i}", path=f"/api/v{i}")
            for i in range(5)],
           mtime=_utc("2026-09-27 09:00:00"))
    cfg = LogConfig(uri=f"file://{d}/access.log", line_regex=NGINX_RE,
                    time_group="ts", time_format=NGINX_TS,
                    columns=_nginx_cols())
    r = LogReader(cfg)
    rows = _drain(r)
    hosts = [x["host"] for x in rows]
    return [("nothing unparsed despite smallint dimension columns",
             r.unparsed_rows == 0 and len(rows) == 5,
             f"{len(rows)} rows / {r.unparsed_rows} unparsed"
             + (f" first reason: {r.unparsed[0].reason}" if r.unparsed else "")),
            ("the dimension carries its raw string, not an int",
             hosts == [f"10.0.0.{i}" for i in range(5)], f"{hosts}"),
            ("measures are still cast to numbers",
             all(isinstance(x["status"], int) for x in rows),
             f"{ {type(x['status']).__name__ for x in rows} }")]


def s_probe(tmp: Path):
    """The GUI probe: match rate, types, roles and the cardinality flag."""
    d = tmp / "probe"
    d.mkdir()
    lines = [_nginx_line(f"27/Sep/2026:08:00:{i:02d} +0000",
                         host=f"10.0.0.{i % 3}",
                         path=f"/tiles/16/{34000 + i}/{22000 + i}.png",
                         status=200 if i % 4 else 404,
                         # deliberately spanning the type tiers: most fit a
                         # smallint, one needs an integer and one a bigint,
                         # so the guess has to widen rather than take the
                         # first value it sees
                         nbytes=str({7: 5_000_000,
                                     13: 3_000_000_000}.get(i, 1000 + i)))
             for i in range(20)]
    _write(d / "access.log", lines + ["junk"],
           mtime=_utc("2026-09-27 09:00:00"))
    rep = sample(LogConfig(uri=f"file://{d}/access.log", line_regex=NGINX_RE,
                           time_group="ts", time_format=NGINX_TS))
    g = {x.group: x for x in rep.groups}
    return [("21 lines read, 20 matched", rep.lines_read == 21
             and rep.matched == 20, f"{rep.matched}/{rep.lines_read}"),
            ("match_pct ~95.2", round(rep.match_pct, 1) == 95.2,
             f"{rep.match_pct}"),
            ("the unmatched line is reported", len(rep.unmatched) == 1,
             f"{rep.unmatched}"),
            ("status guessed smallint/measure",
             g["status"].pg_type == "smallint" and g["status"].role == "measure",
             f"{g['status'].pg_type}/{g['status'].role}"),
            ("bytes widened to bigint by one large value",
             g["bytes"].pg_type == "bigint" and g["bytes"].role == "measure",
             f"{g['bytes'].pg_type}/{g['bytes'].role}"),
            ("ts recognised as the time column",
             g["ts"].role == "time" and rep.time_group == "ts",
             f"{g['ts'].role}/{rep.time_group}"),
            ("host proposed as smallint, not text (it holds the dim index)",
             g["host"].pg_type == "smallint" and g["host"].value_type == "text",
             f"pg_type={g['host'].pg_type} value_type={g['host'].value_type}"),
            ("host is a low-cardinality auto-dimension",
             g["host"].role == "auto-dimension"
             and not g["host"].high_cardinality,
             f"{g['host'].distinct} distinct, flagged="
             f"{g['host'].high_cardinality}"),
            ("path is flagged high-cardinality",
             g["path"].high_cardinality,
             f"{g['path'].distinct} distinct over {rep.lines_read} lines")]


def s_probe_transform(tmp: Path):
    """The probe shows distinctness before and after the transform."""
    d = tmp / "probe_tf"
    d.mkdir()
    _write(d / "access.log",
           [_nginx_line(f"27/Sep/2026:08:00:{i:02d} +0000",
                        path=f"/tiles/16/{34000 + i}/{22000 + i}.png")
            for i in range(20)],
           mtime=_utc("2026-09-27 09:00:00"))
    rep = sample(LogConfig(uri=f"file://{d}/access.log", line_regex=NGINX_RE,
                           time_group="ts", time_format=NGINX_TS,
                           columns=_nginx_cols(True)))
    p = {x.group: x for x in rep.groups}["path"]
    return [("20 distinct paths before the transform", p.distinct == 20,
             f"{p.distinct}"),
            ("1 after it", p.distinct_after == 1, f"{p.distinct_after}"),
            ("and it is no longer flagged", not p.high_cardinality,
             f"flagged={p.high_cardinality}")]


def s_roots_guard(tmp: Path):
    """STARCUBES_LOG_ROOTS refuses a path outside it, by name."""
    from core.logsource import LogSourceError
    d = tmp / "guard"
    d.mkdir()
    _write(d / "access.log", [_nginx_line("27/Sep/2026:08:00:00 +0000")],
           mtime=_utc("2026-09-27 09:00:00"))
    cfg = LogConfig(uri=f"file://{d}/access.log", line_regex=NGINX_RE,
                    time_group="ts", time_format=NGINX_TS,
                    columns=_nginx_cols())
    prev = os.environ.get("STARCUBES_LOG_ROOTS")
    try:
        # The guard is read at call time, and the module-level default is
        # "no restriction", so this also proves it is not cached.
        os.environ["STARCUBES_LOG_ROOTS"] = "/nonexistent-root"
        refused, msg = False, ""
        try:
            _drain(LogReader(cfg))
        except LogSourceError as exc:
            refused, msg = True, str(exc)
        os.environ["STARCUBES_LOG_ROOTS"] = str(tmp)
        allowed = len(_drain(LogReader(cfg))) == 1
    finally:
        if prev is None:
            os.environ.pop("STARCUBES_LOG_ROOTS", None)
        else:
            os.environ["STARCUBES_LOG_ROOTS"] = prev
    return [("a path outside the roots is refused", refused, msg[:60]),
            ("and the message names the variable",
             "STARCUBES_LOG_ROOTS" in msg, msg[:60]),
            ("inside the roots it reads normally", allowed, "")]


SCENARIOS = [
    ("offset in the line", s_offset_in_line),
    ("zone from configuration, across DST", s_zone_from_config),
    ("the ambiguous DST hour", s_dst_ambiguous),
    ("year from mtime (syslog RFC3164)", s_year_from_mtime),
    ("sub-second window boundary, growing file", s_subsecond_boundary),
    ("value transform", s_transform),
    ("rotation, gzip and the mtime prefilter", s_rotation_and_gzip),
    ("multi-line records", s_multiline),
    ("unparsed lines and the match rate", s_unparsed_and_rate),
    ("unparsed lines are not re-counted", s_unparsed_not_recounted),
    ("crash-resume count de-duplication", s_skip_at_start),
    ("a dimension column stays raw", s_dimension_stays_raw),
    ("the GUI probe", s_probe),
    ("the probe's transform preview", s_probe_transform),
    ("STARCUBES_LOG_ROOTS", s_roots_guard),
]


def _collect(cfg: LogConfig) -> list[dict]:
    return _drain(LogReader(cfg))


def _drain(reader: LogReader) -> list[dict]:
    return [r for c in reader.iter_chunks(None, None, 1000)
            for r in c.to_dict("records")]


class Command(BaseCommand):
    help = ("Measure core/logsource.py against fixtures whose correct answer "
            "is known independently. Needs no database.")

    def add_arguments(self, parser):
        parser.add_argument("--only", metavar="SUBSTRING",
                            help="run only scenarios whose name contains this")
        parser.add_argument("--quiet", action="store_true",
                            help="print only failures and the summary")

    def handle(self, *args, **options):
        only = (options.get("only") or "").lower()
        quiet = options["quiet"]
        failures, checks = [], 0
        with tempfile.TemporaryDirectory(prefix="starcubes-logsrc-") as td:
            tmp = Path(td)
            for name, fn in SCENARIOS:
                if only and only not in name.lower():
                    continue
                if not quiet:
                    self.stdout.write(self.style.MIGRATE_HEADING(name))
                try:
                    results = fn(tmp)
                except Exception as exc:          # a scenario that blew up
                    failures.append(f"{name}: raised {exc!r}")
                    self.stdout.write(self.style.ERROR(
                        f"  raised {type(exc).__name__}: {exc}"))
                    continue
                for label, ok, detail in results:
                    checks += 1
                    if ok:
                        if not quiet:
                            self.stdout.write(f"  ok    {label}")
                    else:
                        failures.append(f"{name}: {label} ({detail})")
                        self.stdout.write(self.style.ERROR(
                            f"  FAIL  {label}  --  {detail}"))

        self.stdout.write("")
        if failures:
            raise CommandError(
                f"{len(failures)} of {checks} checks failed:\n  "
                + "\n  ".join(failures))
        self.stdout.write(self.style.SUCCESS(
            f"{checks} checks passed across "
            f"{len([1 for n, _ in SCENARIOS if not only or only in n.lower()])} "
            f"scenarios."))
