# Bitmap indexes for PostgreSQL — feasibility and space measurements

Research note, 2026-09-09. Measured on PostgreSQL 18.6 (Fedora 43,
`postgresql-server-18.6`), 10 M rows, scratch cluster.

Question behind it: could a persistent on-disk bitmap index be implemented
for PostgreSQL, and would it pay for itself in space on star-schema fact
tables?

**Short answer:** implementable as an extension, but the space win only
materialises when the data is *physically clustered*. Against a modern
deduplicating B-tree, random data yields ~3×, which does not justify the
MVCC cost. Clustered data yields 141–183×.

---

## 1. Terminology — two different things

PostgreSQL already has **bitmap index *scans***: transient in-memory
`TIDBitmap`s the executor builds from any index in order to combine them
with `BitmapAnd` / `BitmapOr`. What does not exist is a **persistent
on-disk bitmap index**. These are routinely confused; only the latter is
the subject here.

## 2. Is it implementable?

Yes, and without patching core. Since 9.6 the index access method API is
extensible via `CREATE ACCESS METHOD ... TYPE INDEX`.

Required pieces:

- **`IndexAmRoutine` callbacks.** A bitmap AM needs only a reduced set:
  `ambuild`, `ambuildempty`, `aminsert`, `amgetbitmap`, `ambulkdelete`,
  `amvacuumcleanup`, `amcostestimate`, `amoptions`, `amvalidate`.
  `amgetbitmap` is the natural scan path — it fills a `TIDBitmap`
  directly. `amgettuple` may be `NULL` (no ORDER BY, no index-only scans,
  no unique constraints — all appropriate for a bitmap index).
  Flags: `amcanreturn = NULL`, `amcanorder = false`, `amclusterable = false`.
- **Page layout** (Greenplum's design as the template): meta page, LOV
  pages ("list of values": the distinct values, indexed by an internal
  B-tree), and bitmap vector pages.
- **Compression.** Greenplum uses a WAH variant. Roaring bitmaps are the
  better modern choice — faster set operations, usually smaller. The
  `pg_roaringbitmap` extension already ships a usable C core.
- **WAL logging.** `generic_xlog.h` is the simple route (this is what
  `contrib/bloom` does); from PG 16 a custom resource manager
  (`RegisterCustomRmgr`) gives more compact WAL.
- **Operator classes** for the supported types.

### The hard part: MVCC

This is why the 2006–2008 core patch was abandoned:

- **TID sparsity.** A TID is `(BlockNumber 32, OffsetNumber 16)` — a 2⁴⁸
  address space, very thinly populated. Quantified in §4.
- **UPDATE creates a new TID.** Every row version needs a new index entry;
  the vector grows monotonically. HOT updates only help partially.
- **VACUUM.** `ambulkdelete` must clear bits, but there is no efficient
  reverse mapping (CTID → bit position), and compressed runs have to be
  decompressed and recompressed. A rebuild is often faster.
- **Locking.** The structure is dense enough that an MVCC-compatible
  approach effectively locks many TIDs at once → deadlocks under light
  concurrency that a B-tree would never produce.

Consequence: realistic only as a **read-mostly / append-only** structure
for DWH workloads — exactly the niche Greenplum targets.

## 3. Method

Table of 10 M rows, 731 MB heap, **107 rows per page**. Ten `int4`
columns: `r*` uniformly random with the given cardinality, `s*`
physically clustered (values ascend with the row number).

For each column two B-tree indexes were built — with and without
`deduplicate_items` — and their real on-disk size read from
`pg_relation_size`.

Bitmap sizes are **not** estimates. The exact Roaring container size was
computed over the same rows in SQL: group positions by 2¹⁶-chunk, then per
`(value, chunk)` pick the cheapest container —
`min(2·card, 8192, 2 + 4·runs)` plus an 8-byte directory entry. Two
position mappings were evaluated:

- **dense** — `id - 1`, the theoretical optimum.
- **tid** — `page · 291 + offset`, i.e. what a real AM would have to
  address (`MaxHeapTuplesPerPage` = 291, of which only 107 are occupied).

Sanity checks: at D=2 the formula returns exactly 2 × 10 M bits = 2.5 MB;
at D=100 exactly 2 bytes/row; D=16 lands precisely on the Roaring
array↔bitmap switchover. The computation tracks theory.

## 4. Results

All sizes in MB. "Factor" compares the deduplicating B-tree against the
bitmap under the realistic TID mapping.

| Column | Distinct | B-tree, no dedup | B-tree, dedup | Bitmap (dense) | Bitmap (TID) | Factor |
|---|---:|---:|---:|---:|---:|---:|
| `r2`      | 2       | 214.5 | 66.1 | 2.4  | 6.5  | **10.2×** |
| `r8`      | 8       | 214.5 | 66.1 | 9.6  | 19.1 | **3.5×** |
| `r16`     | 16      | 214.5 | 66.1 | 19.0 | 19.1 | **3.5×** |
| `r100`    | 100     | 214.5 | 66.2 | 19.2 | 19.4 | **3.4×** |
| `r1000`   | 1 000   | 214.5 | 66.5 | 20.2 | 22.2 | **3.0×** |
| `r10000`  | 10 000  | 214.5 | 69.2 | 30.7 | 47.9 | **1.4×** |
| `r100000` | 100 000 | 214.5 | 67.7 | 75.1 | 86.9 | **0.8× ⚠** |
| `s2` *(clustered)*     | 2      | 214.5 | 66.1 | 0.002 | 0.36 | **183×** |
| `s100` *(clustered)*   | 100    | 214.5 | 66.2 | 0.003 | 0.36 | **183×** |
| `s10000` *(clustered)* | 10 000 | 214.5 | 69.8 | 0.14  | 0.49 | **141×** |

Per row: B-tree 22.5 bytes without dedup, 6.9 bytes with. Bitmap 0.68
bytes at D=2, ~2.0 bytes across D=8…1000, 0.04 bytes when clustered.

## 5. Interpretation

**The real competitor is B-tree deduplication, not the B-tree.** Since
PG 13 equal keys are packed into posting lists: 22.5 → 6.9 bytes/row, a
factor of 3.2 for free, and *independent of cardinality* — even at
100 000 distinct values. Against the old non-deduplicated B-tree a bitmap
index would be 11–33× smaller; against today's it is 3–3.5× on random
data.

**Random data has a hard floor at 2 bytes/row.** For D between 8 and 1000
Roaring always lands at ~19–22 MB, because array containers dominate:
2 bytes per row regardless of the number of distinct values. A bitmap
index therefore does *not* keep improving as cardinality drops — except at
very small D, where bitmap containers take over (D=2 → 0.68 bytes/row).

**TID sparsity costs a factor of 2.7.** `r2` needs 2.4 MB under dense
numbering but 6.5 MB under the real TID mapping. Only 107 of 291 possible
offsets per page are used — 36.8 % density, i.e. 2.72× waste. This hits
bitmap containers exactly; array containers are affected less, since they
store values rather than holes.

**Beyond ~10 000 distinct values it turns negative.** At D=100 000 the
bitmap index is *larger* than the B-tree: each value covers only ~100 rows
spread over ~150 chunks, so nearly every container holds a single entry
and the 8-byte header dominates. This matches Greenplum's own documented
limit of ~100 000.

**The win comes from clustering, not from low cardinality.** 141–183× on
sorted data, because run containers collapse the runs into 4 bytes each.
That is the DWH case: time series, `CLUSTER`, append-only loading in sort
order. Here the bitmap index is not 3× better but two orders of magnitude.

### Caveat

The Roaring figures are payload only. A real index additionally carries
page fragmentation, the LOV structure and a meta page — realistically
**+10–20 %**. This becomes critical at high cardinality with small
vectors: if each of `s10000`'s 10 000 vectors got its own 8 KB page, that
would be 80 MB instead of 0.49 MB, destroying the advantage entirely.
**The page layout must pack many small vectors per page.** Greenplum's LOV
indirection exists for this reason; it is a precondition, not an
optimisation.

## 6. Relevance to starcubes

Fact tables with dimension FKs look like the favourable case on paper: low
cardinality, append-only ETL, frequently loaded in date order. But no bitmap
AM exists for PostgreSQL, so the factors in §4 are **not savings that can be
realised** — they are the budget for deciding whether an implementation would
be worth it. On random data that budget is 3×, which it is not.

What the repo actually does today (verified against `core/schema.py` and
`core/profile.py`):

1. **Deduplication is already active — passively.** It is a PostgreSQL
   default since 13, not a starcubes setting. Dimension FK columns are
   integer types (`smallint`/`integer`/`bigint`, see `_SAFE_CASTS`), so none
   of the exclusions apply — deduplication is unsafe only for `numeric`,
   `float4`/`float8`, `jsonb`, non-deterministic collations, container types
   and `INCLUDE` indexes. The 3.2× (22.5 → 6.9 bytes/row) is therefore
   already banked. Nothing to tune.
   *Precondition:* PostgreSQL ≥ 13. The project pins Django 5.2 LTS for
   exactly that floor, and `docs/deployment-config.md` records a deployment
   on 14.10 — so the condition holds.

2. **Dimension FK indexes are already partial.** `core/schema.py:317` emits
   `CREATE INDEX ... USING btree(<col>) WHERE <col> != 1`, excluding the
   `__unknown__` sentinel row. The *intent* is to remove the single most
   frequent value from the index — precisely the case a bitmap index handles
   best (one very frequent value collapses into one long run).
   Caveat: the predicate hardcodes `1`, but the sentinel only lands on
   `index = 1` when the dim table is created empty. With `inline_values`
   (`core/schema.py:268`) or on import of an already-populated dim
   (`core/schema.py:632`) the sentinel is inserted *after* the real rows and
   gets a higher `SMALLSERIAL`. In those cases the index excludes a genuine
   dimension value and indexes the sentinel instead. Correctness is
   unaffected — a partial index can never hide rows — but the intended space
   saving is not realised there.

3. **BRIN is already supported.** `_VALID_INDEX_METHODS = {"brin", "btree",
   "hash"}` (`core/profile.py:76`) — the fact-index method is selectable per
   profile and emitted as `USING {method}` at `core/schema.py:308`. So this
   is a configuration question, not a development task.

4. **Physical clustering is the only real lever.** The 141× gap between `r*`
   and `s*` is a property of the *data layout*, not of the index type.
   Loading a fact table in sort order — or `CLUSTER` — is what produces long
   runs, and it benefits BRIN and heap locality alike. For a clustered,
   append-only fact table, switching a fact index to `brin` targets the same
   workload a bitmap index would, at a fraction of the size, in core, today.

## 7. Conclusion

The space gain justifies the effort only when **both** conditions hold:
cardinality below ~1 000 **and** physically clustered data. Then it is
100×+. On randomly distributed data it is 3× against a modern B-tree —
not worth the MVCC and VACUUM problems from §2.

Should an implementation ever be attempted, the path would be:
`contrib/bloom` as the scaffold for a minimal no-op AM, then LOV pages,
then `amgetbitmap` with uncompressed vectors, then Roaring compression —
and `ambulkdelete` deliberately last, because that is where the design
either holds or fails.

## 8. Reproducing the measurement

Requires `postgresql-server-devel` only for building an actual AM; the
measurement below needs just a running PostgreSQL 13+ instance.

```sql
-- Test table: 10 M rows, random and clustered columns
SELECT setseed(0.42);
CREATE TABLE facts AS
SELECT i AS id,
  floor(random()*2)::int      AS r2,
  floor(random()*8)::int      AS r8,
  floor(random()*16)::int     AS r16,
  floor(random()*100)::int    AS r100,
  floor(random()*1000)::int   AS r1000,
  floor(random()*10000)::int  AS r10000,
  floor(random()*100000)::int AS r100000,
  (i::bigint*2     / 10000001)::int AS s2,
  (i::bigint*100   / 10000001)::int AS s100,
  (i::bigint*10000 / 10000001)::int AS s10000
FROM generate_series(1,10000000) i;
VACUUM ANALYZE facts;

-- B-tree sizes, with and without deduplication
CREATE INDEX bt_r100 ON facts USING btree(r100);
CREATE INDEX nd_r100 ON facts USING btree(r100) WITH (deduplicate_items=off);

-- Exact Roaring container size.  mapping: 'dense' = id-1,
-- 'tid' = page*291+offset (MaxHeapTuplesPerPage=291, 107 rows/page here)
CREATE OR REPLACE FUNCTION roaring_bytes(col text, mapping text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE res bigint; posexpr text;
BEGIN
  IF mapping = 'dense' THEN posexpr := '(id-1)::bigint';
  ELSE posexpr := '(((id-1)/107)::bigint*291 + ((id-1)%107))';
  END IF;
  EXECUTE format($q$
    WITH b AS (
      SELECT v, p >> 16 AS chunk,
             CASE WHEN p - lag(p) OVER (PARTITION BY v, p>>16 ORDER BY p) = 1
                  THEN 0 ELSE 1 END AS nr
      FROM (SELECT %I AS v, %s AS p FROM facts) x
    ), c AS (
      SELECT v, chunk, count(*) AS card, sum(nr) AS runs
      FROM b GROUP BY v, chunk
    )
    SELECT sum(8 + least(2*card, 8192, 2 + 4*runs))::bigint FROM c
  $q$, col, posexpr) INTO res;
  RETURN res;
END $$;

SET work_mem='2GB';
SELECT roaring_bytes('r100','tid');
```

## References

- [PostgreSQL 18: Index Access Method Functions](https://www.postgresql.org/docs/current/index-functions.html)
- [PostgreSQL 18: Index Scanning](https://www.postgresql.org/docs/current/index-scanning.html)
- [PostgreSQL Wiki: Bitmap Indexes](https://wiki.postgresql.org/wiki/Bitmap_Indexes)
- [jiayuasu/bitmap-postgresql](https://github.com/jiayuasu/bitmap-postgresql) — port to PG 8.3.23, 6 commits, self-declared unstable
- [ChenHuajun/pg_roaringbitmap](https://github.com/ChenHuajun/pg_roaringbitmap)
- [Greenplum: Using Indexes](https://techdocs.broadcom.com/us/en/vmware-tanzu/data-solutions/tanzu-greenplum/7/greenplum-database/admin_guide-ddl-ddl-index.html)
