-- Provenance: status (final | prelim) of the Silver behind each day and source of Gold, and the
-- final/provisional day counts per epidemiological week (Sunday-Saturday, MMWR/INS convention).
CREATE VIEW product_status AS
SELECT date, source, status
FROM ingest_manifest
WHERE layer = 'silver' AND status <> 'missing' AND source NOT LIKE '\_gold\_\_%';

CREATE VIEW product_status_epiweekly AS
WITH days AS (
    SELECT source, status, date - extract(dow FROM date)::integer AS week_start
    FROM product_status
)
SELECT extract(year FROM week_start + 3)::smallint                    AS epi_year,
       ((extract(doy FROM week_start + 3)::integer - 1) / 7 + 1)::smallint AS epi_week,
       source,
       count(*) FILTER (WHERE status = 'final')::smallint             AS final_days,
       count(*) FILTER (WHERE status = 'prelim')::smallint            AS prelim_days
FROM days
GROUP BY 1, 2, 3;
