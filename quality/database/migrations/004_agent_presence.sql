-- Presence observations and membership periods use a separate restricted pool.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '004_agent_presence') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '004_agent_presence' \gset
    \if :checksum_matches
        \echo '004_agent_presence already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archive_presence') THEN
        CREATE ROLE archive_presence LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 4;
    END IF;
END $$;
SET LOCAL ROLE archive_owner;
SELECT format('GRANT CONNECT ON DATABASE %I TO archive_presence', current_database()) \gexec
GRANT USAGE ON SCHEMA archive,ops TO archive_presence;

CREATE TABLE archive.agent_presence_samples (
    source_key TEXT NOT NULL CHECK (length(source_key) BETWEEN 1 AND 128 AND source_key=btrim(source_key)),
    account_id BIGINT NOT NULL CHECK (account_id>0),
    agent_id TEXT NOT NULL CHECK (agent_id ~ '^[1-9][0-9]{0,18}$' AND agent_id::numeric<=9223372036854775807),
    sampled_minute TIMESTAMPTZ NOT NULL,
    sampled_at TIMESTAMPTZ NOT NULL,
    agent_name TEXT CHECK (length(agent_name)<=200),
    agent_name_source TEXT NOT NULL CHECK (agent_name_source IN ('chatwoot','unobserved')),
    availability_status TEXT NOT NULL CHECK (availability_status IN ('online','busy','offline','unknown')),
    PRIMARY KEY (source_key,account_id,agent_id,sampled_minute),
    CHECK (sampled_minute=date_trunc('minute',sampled_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
);
CREATE INDEX agent_presence_samples_day_idx ON archive.agent_presence_samples (source_key,account_id,sampled_minute,agent_id);

CREATE FUNCTION archive.reject_presence_sample_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'Presence samples are append-only' USING ERRCODE='55000';
END $$;
CREATE TRIGGER agent_presence_samples_immutable_rows BEFORE UPDATE OR DELETE ON archive.agent_presence_samples
    FOR EACH ROW EXECUTE FUNCTION archive.reject_presence_sample_mutation();
CREATE TRIGGER agent_presence_samples_immutable_truncate BEFORE TRUNCATE ON archive.agent_presence_samples
    FOR EACH STATEMENT EXECUTE FUNCTION archive.reject_presence_sample_mutation();

CREATE TABLE ops.agent_presence_registry (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL CHECK (account_id>0),
    agent_id TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    agent_name TEXT CHECK (length(agent_name)<=200),
    agent_name_source TEXT NOT NULL CHECK (agent_name_source IN ('chatwoot','unobserved')),
    removed_at TIMESTAMPTZ,
    PRIMARY KEY (source_key,account_id,agent_id),
    CHECK (last_seen_at>=first_seen_at),
    CHECK (removed_at IS NULL OR removed_at>=last_seen_at)
);
CREATE TABLE ops.agent_presence_monitor (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL CHECK (account_id>0),
    started_at TIMESTAMPTZ NOT NULL,
    last_sampled_at TIMESTAMPTZ,
    last_sampled_minute TIMESTAMPTZ,
    PRIMARY KEY (source_key,account_id),
    CHECK ((last_sampled_at IS NULL)=(last_sampled_minute IS NULL)),
    CHECK (last_sampled_at IS NULL OR last_sampled_at>=started_at),
    CHECK (last_sampled_minute IS NULL OR last_sampled_minute=
        date_trunc('minute',last_sampled_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
);
CREATE TABLE ops.agent_presence_periods (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    agent_id TEXT NOT NULL,
    started_minute TIMESTAMPTZ NOT NULL,
    ended_minute TIMESTAMPTZ,
    PRIMARY KEY (source_key,account_id,agent_id,started_minute),
    FOREIGN KEY (source_key,account_id,agent_id)
        REFERENCES ops.agent_presence_registry(source_key,account_id,agent_id),
    CHECK (started_minute=date_trunc('minute',started_minute AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),
    CHECK (ended_minute IS NULL OR (ended_minute>=started_minute AND
        ended_minute=date_trunc('minute',ended_minute AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'))
);
CREATE UNIQUE INDEX agent_presence_periods_open_idx ON ops.agent_presence_periods(source_key,account_id,agent_id)
    WHERE ended_minute IS NULL;

-- Override inherited archive defaults: ingestion cannot write presence samples.
REVOKE ALL ON archive.agent_presence_samples FROM PUBLIC,archive_ingest,archive_reader,archive_quality,archive_assignment,archive_presence;
REVOKE ALL ON ops.agent_presence_registry,ops.agent_presence_monitor,ops.agent_presence_periods
    FROM PUBLIC,archive_ingest,archive_reader,archive_quality,archive_assignment,archive_presence;
GRANT SELECT,INSERT ON archive.agent_presence_samples TO archive_presence;
GRANT SELECT,INSERT ON ops.agent_presence_registry,ops.agent_presence_monitor,ops.agent_presence_periods TO archive_presence;
GRANT UPDATE(last_seen_at,agent_name,agent_name_source,removed_at) ON ops.agent_presence_registry TO archive_presence;
GRANT UPDATE(last_sampled_at,last_sampled_minute) ON ops.agent_presence_monitor TO archive_presence;
GRANT UPDATE(ended_minute) ON ops.agent_presence_periods TO archive_presence;
GRANT SELECT ON archive.agent_presence_samples,ops.agent_presence_registry,ops.agent_presence_monitor,ops.agent_presence_periods
    TO archive_reader;
REVOKE ALL ON ops.schema_migrations FROM archive_presence;

COMMENT ON TABLE archive.agent_presence_samples IS 'Immutable successful Chatwoot agent observations, once per UTC minute; absence is not offline.';
COMMENT ON TABLE ops.agent_presence_periods IS 'Observed roster membership intervals; start included, removal minute excluded.';
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('004_agent_presence',:'migration_checksum','Independent immutable presence samples and historical roster membership periods');
\endif
COMMIT;
