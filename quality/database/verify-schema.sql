BEGIN;
SELECT set_config('quality.verify_source', '__verify_' || gen_random_uuid()::TEXT, TRUE) AS verify_source \gset

SET LOCAL ROLE archive_owner;
CREATE TABLE archive._future_evidence_probe (id BIGINT GENERATED ALWAYS AS IDENTITY, body TEXT);
CREATE TABLE ops._future_permissions_probe (id INTEGER);

DO $check$
DECLARE
    target_table TEXT;
    runtime_role TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '001_capture') THEN
        RAISE EXCEPTION 'FAIL: capture migration is not recorded';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '005_presence_retention') THEN
        RAISE EXCEPTION 'FAIL: presence retention migration is not recorded';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '006_presence_retention_state') THEN
        RAISE EXCEPTION 'FAIL: durable presence cleanup migration is not recorded';
    END IF;
    IF has_table_privilege('archive_presence', 'ops.agent_presence_cleanup_state', 'INSERT,UPDATE,DELETE,TRUNCATE')
        OR NOT has_function_privilege('archive_presence', 'ops.run_presence_cleanup(text,bigint)', 'EXECUTE') THEN
        RAISE EXCEPTION 'FAIL: durable cleanup permissions are incorrect';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
        WHERE p.oid='ops.run_presence_cleanup(text,bigint)'::regprocedure
            AND p.prosecdef AND r.rolname='archive_owner'
            AND 'search_path=pg_catalog, pg_temp'=ANY(p.proconfig))
        OR EXISTS (SELECT 1 FROM pg_proc p,
            LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
            WHERE p.oid='ops.run_presence_cleanup(text,bigint)'::regprocedure
                AND a.grantee=0 AND a.privilege_type='EXECUTE') THEN
        RAISE EXCEPTION 'FAIL: durable cleanup owner, search path or public permissions are incorrect';
    END IF;
    FOREACH runtime_role IN ARRAY ARRAY['archive_ingest', 'archive_reader', 'archive_quality'] LOOP
        IF pg_has_role(runtime_role, 'archive_owner', 'MEMBER') THEN
            RAISE EXCEPTION 'FAIL: runtime role can become archive_owner: %', runtime_role;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = runtime_role AND (rolsuper OR rolcreatedb OR rolcreaterole)) THEN
            RAISE EXCEPTION 'FAIL: runtime role has administrative privileges: %', runtime_role;
        END IF;
        IF has_database_privilege(runtime_role, current_database(), 'CREATE,TEMP') THEN
            RAISE EXCEPTION 'FAIL: runtime role can create schemas or temporary objects: %', runtime_role;
        END IF;
        IF has_table_privilege(runtime_role, 'ops.schema_migrations', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') THEN
            RAISE EXCEPTION 'FAIL: runtime role can access maintenance history: %', runtime_role;
        END IF;
        IF has_table_privilege(runtime_role, 'ops._future_permissions_probe', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') THEN
            RAISE EXCEPTION 'FAIL: future ops table has implicit runtime permissions: %', runtime_role;
        END IF;
        IF has_schema_privilege(runtime_role, 'archive', 'CREATE')
            OR has_schema_privilege(runtime_role, 'ops', 'CREATE')
            OR has_schema_privilege(runtime_role, 'quality', 'CREATE')
            OR has_schema_privilege(runtime_role, 'public', 'CREATE') THEN
            RAISE EXCEPTION 'FAIL: runtime role can create tables: %', runtime_role;
        END IF;
    END LOOP;

    -- Minute presence observations use a separate writer, never the chat ingest role.
    FOR target_table IN SELECT tablename FROM pg_tables
        WHERE schemaname = 'archive' AND tablename <> 'agent_presence_samples' LOOP
        IF NOT has_table_privilege('archive_ingest', 'archive.' || target_table, 'SELECT')
            OR NOT has_table_privilege('archive_ingest', 'archive.' || target_table, 'INSERT') THEN
            RAISE EXCEPTION 'FAIL: ingest cannot append evidence: %', target_table;
        END IF;
        FOREACH runtime_role IN ARRAY ARRAY['archive_ingest', 'archive_reader', 'archive_quality'] LOOP
            IF has_table_privilege(runtime_role, 'archive.' || target_table, 'UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES') THEN
                RAISE EXCEPTION 'FAIL: evidence has mutation privileges: % %', runtime_role, target_table;
            END IF;
        END LOOP;
        FOREACH runtime_role IN ARRAY ARRAY['archive_reader', 'archive_quality'] LOOP
            IF NOT has_table_privilege(runtime_role, 'archive.' || target_table, 'SELECT')
                OR has_table_privilege(runtime_role, 'archive.' || target_table, 'INSERT') THEN
                RAISE EXCEPTION 'FAIL: evidence reader is not read-only: % %', runtime_role, target_table;
            END IF;
        END LOOP;
    END LOOP;

    FOREACH target_table IN ARRAY ARRAY['event_jobs', 'attachment_jobs', 'conversation_index', 'backfill_jobs'] LOOP
        IF NOT has_table_privilege('archive_ingest', 'ops.' || target_table, 'SELECT')
            OR NOT has_table_privilege('archive_ingest', 'ops.' || target_table, 'INSERT')
            OR NOT has_table_privilege('archive_ingest', 'ops.' || target_table, 'UPDATE')
            OR has_table_privilege('archive_ingest', 'ops.' || target_table, 'DELETE,TRUNCATE') THEN
            RAISE EXCEPTION 'FAIL: incorrect ingestion task permissions: %', target_table;
        END IF;
        FOREACH runtime_role IN ARRAY ARRAY['archive_reader', 'archive_quality'] LOOP
            IF NOT has_table_privilege(runtime_role, 'ops.' || target_table, 'SELECT')
                OR has_table_privilege(runtime_role, 'ops.' || target_table, 'INSERT,UPDATE,DELETE,TRUNCATE') THEN
                RAISE EXCEPTION 'FAIL: query/quality role can mutate ingestion state: % %', runtime_role, target_table;
            END IF;
        END LOOP;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname IN ('archive','ops') AND tableowner <> 'archive_owner') THEN
        RAISE EXCEPTION 'FAIL: archive objects have an unexpected owner';
    END IF;
END
$check$;

SET LOCAL ROLE archive_ingest;
INSERT INTO archive.events (source_key, account_id, event_key, event_type, ingestion_kind,
    conversation_id, message_id, raw_body, payload)
VALUES (:'verify_source', 1, 'created:4001', 'message_created', 'webhook', 10, 4001,
    convert_to('{"event":"message_created","id":4001,"content":"original"}', 'UTF8'),
    '{"event":"message_created","id":4001,"content":"original"}')
RETURNING id AS created_event_id \gset
INSERT INTO ops.event_jobs (event_id) VALUES (:created_event_id);

WITH duplicate AS (
    INSERT INTO archive.events (source_key, account_id, event_key, event_type, ingestion_kind, raw_body, payload)
    VALUES (:'verify_source', 1, 'created:4001', 'message_created', 'webhook', convert_to('{}','UTF8'), '{}')
    ON CONFLICT (source_key, account_id, event_key) DO NOTHING RETURNING id
)
SELECT COUNT(*) = 0 AS duplicate_was_ignored FROM duplicate \gset
\if :duplicate_was_ignored
\else
    \echo 'FAIL: duplicate event was inserted'
    DO $check$ BEGIN RAISE EXCEPTION 'FAIL: duplicate event was not ignored'; END $check$;
\endif

INSERT INTO archive.message_snapshots (event_id, source_key, account_id, conversation_id, message_id,
    channel_key, business_user_id, snapshot_kind, direction, sender_role, content, sent_at)
VALUES (:created_event_id, :'verify_source', 1, 10, 4001, 'test', 'user-test',
    'created', 'incoming', 'contact', 'original', CURRENT_TIMESTAMP)
RETURNING id AS original_snapshot_id \gset
INSERT INTO archive.conversation_snapshots (event_id, source_key, account_id, conversation_id, event_type, data)
VALUES (:created_event_id, :'verify_source', 1, 10, 'message_created', '{"status":"open"}');
INSERT INTO ops.conversation_index (source_key, account_id, conversation_id, channel_key,
    business_user_id, current_status, last_message_at, last_event_id)
VALUES (:'verify_source', 1, 10, 'test', 'user-test', 'open', CURRENT_TIMESTAMP, :created_event_id);

INSERT INTO archive.events (source_key, account_id, event_key, event_type, ingestion_kind,
    conversation_id, message_id, raw_body, payload)
VALUES (:'verify_source', 1, 'deleted:4001', 'message_updated', 'webhook', 10, 4001,
    convert_to('{"event":"message_updated","id":4001,"deleted":true}', 'UTF8'),
    '{"event":"message_updated","id":4001,"deleted":true}')
RETURNING id AS deleted_event_id \gset
INSERT INTO archive.message_snapshots (event_id, source_key, account_id, conversation_id, message_id,
    snapshot_kind, direction, sender_role, content)
VALUES (:deleted_event_id, :'verify_source', 1, 10, 4001, 'deleted', 'incoming', 'contact', NULL);

-- Two outgoing messages, one bot and one human, use the same evidence pipeline.
WITH outgoing_events AS (
    INSERT INTO archive.events (source_key, account_id, event_key, event_type, ingestion_kind,
        conversation_id, message_id, raw_body, payload)
    SELECT :'verify_source', 1, 'created:' || message_id, 'message_created', 'webhook', 10,
        message_id, convert_to(jsonb_build_object('id', message_id)::TEXT, 'UTF8'),
        jsonb_build_object('id', message_id)
    FROM (VALUES (4002::BIGINT), (4003::BIGINT)) AS messages(message_id)
    RETURNING id, source_key, account_id, conversation_id, message_id
)
INSERT INTO archive.message_snapshots (event_id, source_key, account_id, conversation_id, message_id,
    snapshot_kind, direction, sender_role, content)
SELECT id, source_key, account_id, conversation_id, message_id, 'created', 'outgoing',
    CASE WHEN message_id = 4002 THEN 'bot' ELSE 'agent' END, 'outgoing reply'
FROM outgoing_events;

INSERT INTO archive.attachments (snapshot_id, attachment_key, filename, media_type, source_url)
VALUES (:original_snapshot_id, 'attachment:1', 'test.txt', 'text/plain', 'https://example.invalid/test.txt')
RETURNING id AS attachment_id \gset
INSERT INTO archive.attachment_copies (attachment_id, storage_backend, storage_namespace, object_key,
    byte_count, checksum_sha256)
VALUES (:attachment_id, 'verification', :'verify_source', 'test.txt', 0,
    encode(sha256(convert_to('', 'UTF8')), 'hex'));
INSERT INTO ops.attachment_jobs (attachment_id) VALUES (:attachment_id);
UPDATE ops.attachment_jobs SET status = 'completed', completed_at = CURRENT_TIMESTAMP
WHERE attachment_id = :attachment_id;

UPDATE ops.event_jobs SET status = 'processing', locked_by = 'verification',
    locked_until = CURRENT_TIMESTAMP + INTERVAL '1 minute', attempts = attempts + 1
WHERE event_id = :created_event_id;
UPDATE ops.event_jobs SET status = 'completed', locked_by = NULL, locked_until = NULL,
    completed_at = CURRENT_TIMESTAMP WHERE event_id = :created_event_id;
UPDATE ops.conversation_index SET current_status = 'resolved', last_event_id = :deleted_event_id
WHERE source_key = :'verify_source' AND account_id = 1 AND conversation_id = 10;
INSERT INTO ops.backfill_jobs (source_key, account_id, scope) VALUES (:'verify_source', 1, '{"conversation_id":10}');
UPDATE ops.backfill_jobs SET checkpoint = '{"page":2}', processed_count = 1 WHERE source_key = :'verify_source';

DO $check$
DECLARE target_table TEXT;
BEGIN
    IF (SELECT COUNT(*) FROM archive.message_snapshots
        WHERE source_key = current_setting('quality.verify_source') AND message_id = 4001) <> 2
        OR NOT EXISTS (SELECT 1 FROM archive.message_snapshots
            WHERE source_key = current_setting('quality.verify_source') AND content = 'original' AND snapshot_kind = 'created') THEN
        RAISE EXCEPTION 'FAIL: deletion observation overwrote the original';
    END IF;
    FOR target_table IN SELECT tablename FROM pg_tables
        WHERE schemaname = 'archive' AND tablename <> 'agent_presence_samples' LOOP
        BEGIN
            EXECUTE format('UPDATE archive.%I SET id = DEFAULT WHERE FALSE', target_table);
            RAISE EXCEPTION 'FAIL: evidence update was allowed: %', target_table;
        EXCEPTION WHEN insufficient_privilege THEN NULL;
        END;
        BEGIN
            EXECUTE format('DELETE FROM archive.%I WHERE FALSE', target_table);
            RAISE EXCEPTION 'FAIL: evidence deletion was allowed: %', target_table;
        EXCEPTION WHEN insufficient_privilege THEN NULL;
        END;
        BEGIN
            EXECUTE format('TRUNCATE archive.%I', target_table);
            RAISE EXCEPTION 'FAIL: evidence truncate was allowed: %', target_table;
        EXCEPTION WHEN insufficient_privilege THEN NULL;
        END;
    END LOOP;
    BEGIN
        INSERT INTO archive.message_snapshots (event_id, source_key, account_id, conversation_id, message_id,
            snapshot_kind, direction, sender_role)
        SELECT id, source_key || '-wrong-source', account_id, 10, 4009, 'created', 'incoming', 'contact'
        FROM archive.events WHERE source_key = current_setting('quality.verify_source') LIMIT 1;
        RAISE EXCEPTION 'FAIL: snapshot references an event from another source';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO archive.events (source_key, account_id, event_key, event_type, ingestion_kind, raw_body, payload)
        VALUES (current_setting('quality.verify_source'), 1, 'invalid-body', 'message_created', 'webhook',
            convert_to('{"content":"original"}', 'UTF8'), '{"content":"different"}');
        RAISE EXCEPTION 'FAIL: payload differs from preserved raw bytes';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        UPDATE ops.event_jobs SET status = 'processing'
        WHERE event_id IN (SELECT id FROM archive.events WHERE source_key = current_setting('quality.verify_source'));
        RAISE EXCEPTION 'FAIL: a processing job was allowed without a valid lease';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
END
$check$;

SET LOCAL ROLE archive_reader;
SELECT sender_role, COUNT(*) AS snapshots FROM archive.message_snapshots
WHERE source_key = :'verify_source' GROUP BY sender_role ORDER BY sender_role;
DO $check$
BEGIN
    BEGIN
        UPDATE ops.conversation_index SET current_status = 'overwritten' WHERE FALSE;
        RAISE EXCEPTION 'FAIL: backend reader can write query projections';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        INSERT INTO archive._future_evidence_probe (body) VALUES ('unauthorized');
        RAISE EXCEPTION 'FAIL: backend reader can append evidence';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END
$check$;

SET LOCAL ROLE archive_quality;
SELECT COUNT(*) AS quality_can_read FROM archive.message_snapshots WHERE source_key = :'verify_source';
DO $check$
BEGIN
    BEGIN
        UPDATE ops.event_jobs SET attempts = attempts + 1 WHERE FALSE;
        RAISE EXCEPTION 'FAIL: quality worker can change ingestion queue';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END
$check$;
SET LOCAL ROLE archive_assignment;
DO $check$
BEGIN
    IF NOT has_table_privilege(current_user, 'archive.events', 'SELECT')
       OR NOT has_table_privilege(current_user, 'archive.events', 'INSERT')
       OR NOT has_table_privilege(current_user, 'ops.event_jobs', 'INSERT') THEN
        RAISE EXCEPTION 'FAIL: assignment delivery cannot append durable events';
    END IF;
    BEGIN
        UPDATE archive.events SET event_type = 'overwritten' WHERE FALSE;
        RAISE EXCEPTION 'FAIL: assignment delivery can overwrite original events';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        UPDATE ops.event_jobs SET attempts = attempts + 1 WHERE FALSE;
        RAISE EXCEPTION 'FAIL: assignment delivery can mutate processing jobs';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        DELETE FROM ops.round_index WHERE FALSE;
        RAISE EXCEPTION 'FAIL: assignment delivery can mutate query projections';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END
$check$;
SET LOCAL ROLE archive_presence;
DO $check$
DECLARE target_table TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_roles r ON r.oid=p.proowner
        WHERE n.nspname='ops' AND p.proname='cleanup_presence_history'
            AND p.oid='ops.cleanup_presence_history(text,bigint)'::regprocedure
            AND p.prosecdef AND r.rolname='archive_owner'
            AND 'search_path=pg_catalog, pg_temp'=ANY(p.proconfig)) THEN
        RAISE EXCEPTION 'FAIL: presence cleanup lacks its fixed owner and safe search path';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p,
        LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid='ops.cleanup_presence_history(text,bigint)'::regprocedure
            AND a.grantee=0 AND a.privilege_type='EXECUTE') THEN
        RAISE EXCEPTION 'FAIL: PUBLIC may execute presence cleanup';
    END IF;
    IF NOT has_function_privilege(current_user,'ops.cleanup_presence_history(text,bigint)','EXECUTE')
        OR has_table_privilege(current_user,'ops.agent_presence_periods','DELETE,TRUNCATE') THEN
        RAISE EXCEPTION 'FAIL: presence cleanup requires its function without table deletion privileges';
    END IF;
    FOREACH target_table IN ARRAY ARRAY['archive_ingest','archive_reader','archive_quality','archive_assignment'] LOOP
        IF has_function_privilege(target_table,'ops.cleanup_presence_history(text,bigint)','EXECUTE') THEN
            RAISE EXCEPTION 'FAIL: unrelated runtime role may execute presence cleanup: %', target_table;
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM ops.cleanup_presence_history(current_setting('quality.verify_source'),1)
        WHERE deleted_samples=0 AND deleted_periods=0) THEN
        RAISE EXCEPTION 'FAIL: empty scoped presence cleanup returned unexpected counts';
    END IF;
    IF pg_has_role(current_user, 'archive_owner', 'MEMBER')
       OR has_database_privilege(current_user, current_database(), 'CREATE,TEMP')
       OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname=current_user AND (rolsuper OR rolcreatedb OR rolcreaterole)) THEN
        RAISE EXCEPTION 'FAIL: presence writer has administrative privileges';
    END IF;
    IF NOT has_table_privilege(current_user, 'archive.agent_presence_samples', 'SELECT')
       OR NOT has_table_privilege(current_user, 'archive.agent_presence_samples', 'INSERT')
       OR has_table_privilege(current_user, 'archive.agent_presence_samples', 'UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
       OR has_table_privilege('archive_ingest', 'archive.agent_presence_samples', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR NOT has_table_privilege('archive_reader', 'archive.agent_presence_samples', 'SELECT')
       OR has_table_privilege('archive_reader', 'archive.agent_presence_samples', 'INSERT,UPDATE,DELETE,TRUNCATE') THEN
        RAISE EXCEPTION 'FAIL: presence observations have incorrect writer/reader permissions';
    END IF;
    FOREACH target_table IN ARRAY ARRAY['events','message_snapshots','conversation_snapshots','rating_snapshots'] LOOP
        IF has_table_privilege(current_user, 'archive.' || target_table, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') THEN
            RAISE EXCEPTION 'FAIL: presence writer can access chat evidence: %', target_table;
        END IF;
    END LOOP;
    FOREACH target_table IN ARRAY ARRAY['event_jobs','round_index','schema_migrations','_future_permissions_probe'] LOOP
        IF has_table_privilege(current_user, 'ops.' || target_table, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') THEN
            RAISE EXCEPTION 'FAIL: presence writer can access unrelated operations: %', target_table;
        END IF;
    END LOOP;
END
$check$;
ROLLBACK;
SELECT 'PASS: transactional schema and privilege checks; sample rows rolled back' AS verification;
