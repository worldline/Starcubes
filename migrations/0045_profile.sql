-- Saved source profiles live in the database (migration 0045).
--
-- A profile saved from the GUI or uploaded used to be a YAML file under
-- `profiles/user/`, which in a container meant a volume -- and with several
-- containers on one database, a volume every one of them could write, or a
-- profile saved on one container that the next request (served by another)
-- could not find. The shipped examples stay files in the image: they are part
-- of a release, not of an installation.
--
--   name        the same rule core/profile.py:_NAME_RE enforces, repeated as a
--               CHECK so a row written past the Python layer cannot become a
--               name the download header or the URL route would choke on.
--   yaml        the text as saved or uploaded, byte for byte -- a download
--               returns exactly what an upload stored.
--   updated_by  the Django username, for the catalogue.
--
-- A saved profile with an example's name shadows the example, as a file in
-- `profiles/user/` did. `manage.py bootstrap` imports the files an existing
-- installation still has in `STARCUBES_PROFILE_DIR` (or `profiles/user/`) once,
-- skipping names that already have a row, and leaves the files in place.

CREATE TABLE IF NOT EXISTS starcubes_meta.profile (
    name        text PRIMARY KEY
                CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'),
    yaml        text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);

COMMENT ON TABLE starcubes_meta.profile IS
  'Saved source profiles (YAML), shared by every container. Shipped examples stay files under profiles/examples/ in the image; a row with the same name shadows one.';
