-- Optional PostGIS: cell polygons (EPSG:4326) for QGIS / spatial joins. Managed databases
-- without the extension (or without the privilege to create it) keep working without geom.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'postgis') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'postgis is available but cannot be created: cells keep no geometry';
        END;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
        ALTER TABLE cell ADD COLUMN IF NOT EXISTS geom public.geometry(Polygon, 4326);
        CREATE INDEX IF NOT EXISTS cell_geom ON cell USING gist (geom);
    END IF;
END
$$;
