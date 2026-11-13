-- Responsible seat per round for statistics: the ICS-assigned seat, else the
-- first seat with a valid public reply after the transfer. Colleagues who step
-- in later are only helping and are not assessed for the round. Rebuildable:
-- quality/processing/service_facts.py (FACT_VERSION 3) recomputes old rows.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '013_responsible_seat') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '013_responsible_seat' \gset
    \if :checksum_matches
        \echo '013_responsible_seat already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

-- Nullable columns without defaults: a catalog-only change, no table rewrite.
ALTER TABLE ops.round_service_facts
    ADD COLUMN responsible_agent_id TEXT,
    ADD COLUMN responsible_agent_name TEXT;

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('013_responsible_seat', :'migration_checksum', 'Responsible seat per round in stored statistics facts');
\endif
COMMIT;
