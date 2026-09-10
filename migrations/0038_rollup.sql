-- Pre-aggregation: hourly rollups and the state that governs them
-- (migration 0038).
--
-- Every chart request aggregates the raw rows again. `query_cache` freezes
-- finished `plot_json` payloads, but only for an exactly repeated request --
-- the first look at any window pays the full scan. At the densities this is
-- built for (20M rows/day) that is what limits the resolutions looking across
-- weeks and months.
--
-- Materialising every combination with `CUBE` is not feasible, and it is also
-- not necessary. The base aggregation at hour granularity over a *set* of
-- dimension columns is enough: every marginal, every group_by and every
-- filter falls out of it at query time through a second, cheap aggregation.
-- What cannot be avoided is choosing that set, and that choice is an operator
-- decision -- see `manage.py rollup_advise` for the numbers behind it.
--
--   rollup_interval_s   bucket width in seconds. 0 = off, the default, and
--                       what every installation does today. A column rather
--                       than a constant so a source whose charts start at day
--                       resolution can roll up to days directly.
--
--   rollup_columns      the core rollup's dimension columns. A query is served
--                       from it when its group_by *and* all of its filter
--                       columns are in this set.
--
--   rollup_marginals    dimension columns that were too expensive for the core
--                       set but still get their own single-column rollup, so
--                       an unfiltered breakdown on them stays fast. Mechanically
--                       identical to the core -- a marginal is just a rollup
--                       with one dimension column.
--
--   rollup_measures     aggregation forms materialised alongside `count(*)`,
--                       which is always present. Measures are cheap: they do
--                       not change the row count, only the row width, so this
--                       is a storage decision and not a cardinality one.
--                       Percentiles are the exception and can never appear
--                       here -- they do not sum, so every ladder style stays
--                       on `fact` permanently.
--
-- The one thing that makes a wrong dimension choice cheap to undo: coarsening
-- is a GROUP BY over the remaining columns of the small rollup table, while
-- refining needs a rebuild from `fact`, because the information is no longer
-- there. Start wide, narrow later.

ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS rollup_interval_s int    NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rollup_columns    text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS rollup_marginals  text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS rollup_measures   text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN starcubes_meta.datasource.rollup_interval_s IS
  'Bucket width of the pre-aggregation in seconds; 0 = off (the default). Must divide every chart interval it is meant to serve, so 3600 covers hour and day bins while 86400 covers only day bins. Changing it invalidates every rollup of this datasource (it is part of the spec hash) and needs a rebuild.';
COMMENT ON COLUMN starcubes_meta.datasource.rollup_columns IS
  'Dimension fact columns held in the core rollup. A chart query is served from it only when its group_by and *all* of its filter columns are in this set -- anything else falls back to fact. Sized with `manage.py rollup_advise`: one high-cardinality column can cost more than all the others together.';
COMMENT ON COLUMN starcubes_meta.datasource.rollup_marginals IS
  'Dimension fact columns that get their own single-column rollup instead of a place in rollup_columns. Serves an unfiltered breakdown on a column too expensive for the core set. A marginal is not a second mechanism -- it is a rollup whose column set has one member, and pick_rollup chooses the narrowest spec that covers the query.';
COMMENT ON COLUMN starcubes_meta.datasource.rollup_measures IS
  'Aggregation forms materialised beside the mandatory count(*), as they appear in starcubes_meta.measure.agg (sum(col), avg(col), min(col), max(col)). quantile(col) is rejected: percentiles do not sum, so ladder styles can never be served from a rollup. Cheap to add in storage terms, but adding one needs a rebuild because the raw values are gone.';

-- rollup_state: one row per rollup table. The three columns that carry the
-- whole lifecycle -- an operator configures late, backfills a large history
-- once, and reconfigures whenever the advisor's numbers change.
--
--   spec_hash      hash over column list, measure list and bucket width. When
--                  it disagrees with the current configuration the rollup is
--                  simply not used: pick_rollup returns None and the read path
--                  goes to fact. That is what makes reconfiguration safe at any
--                  moment -- the operator gets a slower chart until the rebuild
--                  finishes, never a wrong one. Same role cache_epoch plays for
--                  query_cache, except this one is derived from the config
--                  rather than bumped by an event.
--
--   built_from     lower and upper bound of the finished buckets. Two marks,
--   built_through  not one, because the initial backfill of a year's history
--                  at these densities runs for hours: it works *backwards*
--                  from the present and pushes built_from down, so the most
--                  recently viewed windows get fast first and a partially
--                  built rollup is already useful. pick_rollup requires
--                  built_from <= t_from AND t_to <= built_through, so a
--                  half-finished backfill can never answer for a range it
--                  does not hold.
--
-- Deliberately not a per-datasource row: a datasource has one core rollup plus
-- one per marginal, and they are built and invalidated independently.

CREATE TABLE IF NOT EXISTS starcubes_meta.rollup_state (
    datasource_id int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                       ON DELETE CASCADE,
    rollup_name   text NOT NULL,
    spec_hash     text NOT NULL,
    built_from    timestamptz,
    built_through timestamptz,
    rows_built    bigint      NOT NULL DEFAULT 0,
    last_built_at timestamptz,
    PRIMARY KEY (datasource_id, rollup_name)
);

COMMENT ON TABLE starcubes_meta.rollup_state IS
  'Build state of one pre-aggregation table. Rows are created by manage.py rollup_config and maintained by rollup_build and the ETL hook. Dropping them is harmless as long as fact still covers the same span -- the rollup is purely additive in the read path, so without state every query simply reads fact again. Below datasource.retention_floor it is not: fact no longer holds those rows and no rebuild can restore them.';
COMMENT ON COLUMN starcubes_meta.rollup_state.rollup_name IS
  'Table name inside the datasource schema, e.g. rollup_1h for the core set or rollup_1h_merch_cat for a marginal.';
COMMENT ON COLUMN starcubes_meta.rollup_state.spec_hash IS
  'sha256 over the sorted column list, measure list and bucket width. A mismatch against the live datasource configuration marks the rollup unusable, which is how a configuration change degrades to fact instead of returning wrong numbers.';
COMMENT ON COLUMN starcubes_meta.rollup_state.built_from IS
  'Oldest bucket held. The backfill works backwards and pushes this down, so the newest windows become fast first. NULL = nothing built. Retention deliberately does not raise it: rollup rows were computed while the raw data existed and stay correct after disposal, so aggregates outlive the rows they came from -- which is the point, not an accident. Below datasource.retention_floor the rollup is therefore the *only* copy, so rollup_drop and rollup_build --rebuild refuse that span without --force and the tables belong in the backup.';
COMMENT ON COLUMN starcubes_meta.rollup_state.built_through IS
  'Exclusive upper bound of the finished buckets, advanced by the ETL hook once a bucket falls below hwm - late_arrival_overlap_s. Rewound in place when a repair rewinds the watermark, which is what makes historical mutation a rewind rather than an invalidation.';
