-- Blacklist 2-hour duration (2026-11-11).
-- The seat client adds a short "2h" block for temporary cool-downs (spam bursts,
-- heated customers). Migration 012 created archive.contact_block_events with an
-- anonymous column check allowing only 24h / 72h / permanent; replace it with a
-- named check that also allows 2h. Existing rows are unaffected.
-- Apply this before (or together with) the ICS release that offers 2h, otherwise
-- the blacklist sync cannot archive 2h events until it is applied.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '017_blacklist_duration_2h') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '017_blacklist_duration_2h' \gset
    \if :checksum_matches
        \echo '017_blacklist_duration_2h already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT conname FROM pg_constraint
        WHERE conrelid = 'archive.contact_block_events'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%duration_code%'
    LOOP
        EXECUTE format('ALTER TABLE archive.contact_block_events DROP CONSTRAINT %I', item.conname);
    END LOOP;
END $$;

ALTER TABLE archive.contact_block_events ADD CONSTRAINT contact_block_events_duration_check
    CHECK (duration_code IN ('2h', '24h', '72h', 'permanent'));

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('017_blacklist_duration_2h', :'migration_checksum',
    'Allow the 2-hour blacklist duration in archive.contact_block_events');
\endif
COMMIT;
