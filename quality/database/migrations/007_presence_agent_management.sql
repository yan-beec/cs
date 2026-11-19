-- Local roster visibility is independent of immutable samples and membership.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '007_presence_agent_management') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '007_presence_agent_management' \gset
    \if :checksum_matches
        \echo '007_presence_agent_management already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
SET LOCAL ROLE archive_owner;
ALTER TABLE ops.agent_presence_registry ADD COLUMN hidden_at TIMESTAMPTZ;
GRANT UPDATE(hidden_at) ON ops.agent_presence_registry TO archive_presence;
COMMENT ON COLUMN ops.agent_presence_registry.hidden_at IS
    'Local roster removal only; samples and membership remain intact. A newer real observation restores visibility.';
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('007_presence_agent_management',:'migration_checksum','Local presence roster management without deleting historical observations');
\endif
COMMIT;
