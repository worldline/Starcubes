-- Named group sets: arbitrarily many grouped dimensions per native dimension
-- (migration 0020).
--
-- Before this migration a dimension had exactly ONE grouping: dim_group hung
-- directly off dimension_id and ux_dim_group_name (dimension_id, group_name)
-- cemented it. The chart addressed it with the token `<fact_column>:grouped`.
--
-- A "group dimension" is now a first-class object: starcubes_meta.dim_group_set
-- carries the user-given name plus a URL slug, and dim_group hangs off the set.
-- The chart token becomes `<fact_column>:grouped:<slug>`; the bare legacy token
-- `<fact_column>:grouped` still resolves, to the dimension's first set.
--
-- slug rather than the id in the token: chart URLs stay readable and portable
-- between installations. The tradeoff (accepted deliberately) is that renaming
-- a group dimension re-derives the slug and therefore breaks saved links and
-- the chart's localStorage state. Slug charset is [a-z0-9-] because the token
-- travels through viz/views.py:_parse_filter_spec, which splits filter params
-- on '=' and value lists on ','.
--
-- dim_group.dimension_id is dropped: it is derivable through the set, and
-- keeping both invites drift.
--
-- Backfill: every dimension that already has groups gets one set named
-- '<fact_column> (grouped)' -- the same label the chart used to render -- and
-- its existing dim_group rows are repointed at it.
--
-- Idempotent: re-running this file is a no-op.

CREATE TABLE IF NOT EXISTS starcubes_meta.dim_group_set (
    id              serial PRIMARY KEY,
    dimension_id    int  NOT NULL REFERENCES starcubes_meta.dimension(id)
                             ON DELETE CASCADE,
    name            text NOT NULL,
    slug            text NOT NULL,
    position        int  NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_group_set_slug
    ON starcubes_meta.dim_group_set (dimension_id, slug);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_group_set_name
    ON starcubes_meta.dim_group_set (dimension_id, name);

ALTER TABLE starcubes_meta.dim_group
    ADD COLUMN IF NOT EXISTS group_set_id int
        REFERENCES starcubes_meta.dim_group_set(id) ON DELETE CASCADE;

-- Backfill only runs while the old dimension_id column is still present.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'starcubes_meta'
                  AND table_name   = 'dim_group'
                  AND column_name  = 'dimension_id') THEN

        INSERT INTO starcubes_meta.dim_group_set (dimension_id, name, slug, position)
        SELECT d.id,
               d.fact_column || ' (grouped)',
               trim(both '-' from regexp_replace(
                        lower(d.fact_column || ' (grouped)'),
                        '[^a-z0-9]+', '-', 'g')),
               0
          FROM starcubes_meta.dimension d
         WHERE EXISTS (SELECT 1 FROM starcubes_meta.dim_group dg
                        WHERE dg.dimension_id = d.id)
           AND NOT EXISTS (SELECT 1 FROM starcubes_meta.dim_group_set gs
                            WHERE gs.dimension_id = d.id);

        UPDATE starcubes_meta.dim_group dg
           SET group_set_id = gs.id
          FROM starcubes_meta.dim_group_set gs
         WHERE gs.dimension_id = dg.dimension_id
           AND dg.group_set_id IS NULL;

        ALTER TABLE starcubes_meta.dim_group DROP COLUMN dimension_id;
    END IF;
END $$;

ALTER TABLE starcubes_meta.dim_group ALTER COLUMN group_set_id SET NOT NULL;

-- Group names are unique per group set, not per dimension: two group
-- dimensions on the same column may both define an "OK" group.
DROP INDEX IF EXISTS starcubes_meta.ux_dim_group_name;

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_group_set_group_name
    ON starcubes_meta.dim_group (group_set_id, group_name);

COMMENT ON TABLE starcubes_meta.dim_group_set IS
  'A named group dimension: one bucketing of a native dimension. Arbitrarily many per dimension.';
COMMENT ON COLUMN starcubes_meta.dim_group_set.slug IS
  'URL-safe [a-z0-9-] form of name, derived on save. Carries the chart token <fact_column>:grouped:<slug>.';
COMMENT ON COLUMN starcubes_meta.dim_group_set.position IS
  'Display order within the dimension; ties broken by id.';
COMMENT ON TABLE starcubes_meta.dim_group IS
  'Named groups that bucket dimension values for chart aggregation, within one dim_group_set. color is a hex string like #e6194b.';
COMMENT ON TABLE starcubes_meta.dim_group_member IS
  'Maps a dim table row (by index PK) to its group. A dim value belongs to at most one group *per group set* (enforced at application level, not by a cross-group unique constraint).';
