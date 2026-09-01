-- 0027_dimension_filter_group.sql — fan-out filter groups for shared dimensions.
--
-- Several fact columns can reference one dim table (so_peer_1/2/3 → peer)
-- because a transaction passes through several peers. A chart filter on such a
-- dimension should hit when ANY of the fact columns matches, but a plain
-- dim_table-based rule cannot express that: a sibling on the same dim table
-- (src_peer) must stay filterable on its own. So the grouping is explicit:
-- dimension rows whose filter_group holds the same non-null value form one
-- chart filter that ORs over every member fact column. NULL keeps the
-- single-column behaviour.
--
-- See docs/star-schema.md ("Filter group") for the semantics and invariants
-- (members share one dim_table; the value must not contain ':' so it cannot
-- collide with the <col>:grouped:<slug> filter grammar).
--
-- Idempotent — re-running on a populated DB is a no-op. Mirror this block in
-- starcubes_meta.sql (which is what `core/schema.py` runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.

ALTER TABLE starcubes_meta.dimension
    ADD COLUMN IF NOT EXISTS filter_group text;

COMMENT ON COLUMN starcubes_meta.dimension.filter_group IS
    'Fan-out filter group: rows sharing a non-null value form one chart filter that ORs over every member fact column (e.g. filter_group=so_peer over so_peer_1/2/3). NULL = ordinary single-column filter.';