"""Retention and immutable-history safeguards in an explicitly disposable DB.

Apply migrations 001-005 first. Uses the same opt-in and disposable database
name checks as test_query_database; never runs against the live archive.
"""

from datetime import timedelta
import os
import unittest
import uuid

import psycopg

from quality.tests import test_query_database as fixture


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires an isolated presence test database")
class PresenceRetentionDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)
        cls.admin.execute("ALTER ROLE archive_presence LOGIN PASSWORD 'isolated-presence'")

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        self.source = "presence-retention-test-" + uuid.uuid4().hex
        self.cutoff = self.admin.execute("""SELECT
            (((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date-44)::timestamp
            AT TIME ZONE 'Asia/Shanghai') AS cutoff""").fetchone()["cutoff"]
        self.writer = psycopg.connect(**{**self.settings.connection_kwargs,
            "user": "archive_presence", "password": "isolated-presence",
            "row_factory": psycopg.rows.dict_row,
            "options": "-c statement_timeout=3000 -c lock_timeout=500"}, autocommit=True)
        self.addCleanup(self.writer.close)

    def cleanup(self, source=None, account=1):
        return self.writer.execute("SELECT * FROM ops.cleanup_presence_history(%s,%s)",
            (source or self.source, account)).fetchone()

    def sample(self, minute, *, source=None, account=1, agent="1"):
        self.admin.execute("""INSERT INTO archive.agent_presence_samples
            (source_key,account_id,agent_id,sampled_minute,sampled_at,agent_name,agent_name_source,availability_status)
            VALUES(%s,%s,%s,%s,%s,'Agent','chatwoot','online')""",
            (source or self.source, account, agent, minute, minute+timedelta(seconds=10)))

    def registry(self, *, source=None, account=1, agent="1"):
        self.admin.execute("""INSERT INTO ops.agent_presence_registry
            (source_key,account_id,agent_id,first_seen_at,last_seen_at,agent_name,agent_name_source)
            VALUES(%s,%s,%s,%s,%s,'Agent','chatwoot') ON CONFLICT DO NOTHING""",
            (source or self.source, account, agent, self.cutoff-timedelta(days=10), self.cutoff))

    def period(self, start, end, *, source=None, account=1, agent="1"):
        self.registry(source=source, account=account, agent=agent)
        self.admin.execute("""INSERT INTO ops.agent_presence_periods
            (source_key,account_id,agent_id,started_minute,ended_minute) VALUES(%s,%s,%s,%s,%s)""",
            (source or self.source, account, agent, start, end))

    def sample_minutes(self, *, source=None, account=1):
        return [row["sampled_minute"] for row in self.admin.execute("""SELECT sampled_minute
            FROM archive.agent_presence_samples WHERE source_key=%s AND account_id=%s ORDER BY sampled_minute""",
            (source or self.source, account)).fetchall()]

    def test_cutoff_preserves_45_beijing_dates_and_crossing_or_open_periods(self):
        old = self.cutoff-timedelta(minutes=1)
        retained = self.cutoff+timedelta(minutes=1)
        for minute in (old, self.cutoff, retained):
            self.sample(minute)
        self.period(old-timedelta(minutes=2), old)
        self.period(old-timedelta(minutes=1), self.cutoff)
        self.period(old, retained)
        self.period(self.cutoff, None)
        # Caller timezone must not change the Beijing calendar boundary.
        self.writer.execute("SET TIME ZONE 'America/Los_Angeles'")
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 2})
        self.assertEqual(self.sample_minutes(), [self.cutoff, retained])
        periods = self.admin.execute("""SELECT started_minute,ended_minute FROM ops.agent_presence_periods
            WHERE source_key=%s ORDER BY started_minute""", (self.source,)).fetchall()
        self.assertEqual(periods, [{"started_minute": old, "ended_minute": retained},
            {"started_minute": self.cutoff, "ended_minute": None}])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_registry WHERE source_key=%s",
            (self.source,)).fetchone()["n"], 1)
        self.assertEqual(self.cleanup(), {"deleted_samples": 0, "deleted_periods": 0})

    def test_scope_isolation_preserves_other_accounts_sources_and_chat_evidence(self):
        old = self.cutoff-timedelta(minutes=1)
        for source, account in ((self.source, 1), (self.source, 2), (self.source+"-other", 1)):
            self.sample(old, source=source, account=account)
            self.period(old-timedelta(minutes=1), old, source=source, account=account)
        event = self.admin.execute("""INSERT INTO archive.events
            (source_key,account_id,event_key,event_type,ingestion_kind,raw_body,payload)
            VALUES(%s,1,'retention-evidence','message_created','webhook',convert_to('{}','UTF8'),'{}') RETURNING id""",
            (self.source,)).fetchone()["id"]
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 1})
        self.assertEqual(self.sample_minutes(account=2), [old])
        self.assertEqual(self.sample_minutes(source=self.source+"-other"), [old])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s AND account_id=2",
            (self.source,)).fetchone()["n"], 1)
        self.assertEqual(self.admin.execute("SELECT raw_body FROM archive.events WHERE id=%s", (event,)).fetchone()["raw_body"], b"{}")

    def test_each_call_is_bounded_to_2000_rows_per_table_and_can_drain_repeatedly(self):
        self.registry()
        self.admin.execute("""INSERT INTO archive.agent_presence_samples
            (source_key,account_id,agent_id,sampled_minute,sampled_at,agent_name_source,availability_status)
            SELECT %s,1,'1',%s-(3000-n)*interval '1 minute',
                %s-(3000-n)*interval '1 minute'+interval '10 seconds','unobserved','unknown'
            FROM generate_series(1,2001) n""", (self.source, self.cutoff, self.cutoff))
        self.admin.execute("""INSERT INTO ops.agent_presence_periods
            (source_key,account_id,agent_id,started_minute,ended_minute)
            SELECT %s,1,'1',%s-(6000-2*n)*interval '1 minute',
                %s-(5999-2*n)*interval '1 minute' FROM generate_series(1,2001) n""",
            (self.source, self.cutoff, self.cutoff))
        self.assertEqual(self.cleanup(), {"deleted_samples": 2000, "deleted_periods": 2000})
        self.assertEqual(len(self.sample_minutes()), 1)
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 1})
        self.assertEqual(self.cleanup(), {"deleted_samples": 0, "deleted_periods": 0})

    def test_runtime_still_cannot_mutate_samples_or_become_owner(self):
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        # A caller-defined GUC cannot confer retention or owner privileges.
        self.writer.execute("SELECT set_config('quality.presence_retention','on',false)")
        for statement in ("DELETE FROM archive.agent_presence_samples WHERE source_key=%s",
            "UPDATE archive.agent_presence_samples SET agent_name='rewritten' WHERE source_key=%s",
            "TRUNCATE archive.agent_presence_samples", "DELETE FROM ops.agent_presence_periods WHERE FALSE",
            "SET ROLE archive_owner", "SELECT * FROM archive.events LIMIT 0"):
            with self.subTest(statement=statement):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    self.writer.execute(statement, (self.source,) if "%s" in statement else None)
        self.assertEqual(self.sample_minutes(), [old])
        self.assertEqual(self.cleanup()["deleted_samples"], 1)

    def test_owner_trigger_rejects_retained_delete_every_update_and_truncate(self):
        self.sample(self.cutoff-timedelta(minutes=1))
        self.sample(self.cutoff)
        for statement in ("DELETE FROM archive.agent_presence_samples WHERE source_key=%s",
            "UPDATE archive.agent_presence_samples SET agent_name='rewritten' WHERE source_key=%s",
            "TRUNCATE archive.agent_presence_samples"):
            with self.subTest(statement=statement):
                with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                    with self.admin.transaction():
                        self.admin.execute("SET LOCAL ROLE archive_owner")
                        self.admin.execute(statement, (self.source,) if "%s" in statement else None)
        self.assertEqual(self.sample_minutes(), [self.cutoff-timedelta(minutes=1), self.cutoff])
        # An ordinary admin statement (current_user != owner) also cannot bypass
        # the trigger merely by setting the same kind of arbitrary flag.
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with self.admin.transaction():
                self.admin.execute("SELECT set_config('quality.presence_retention','on',true)")
                self.admin.execute("DELETE FROM archive.agent_presence_samples WHERE source_key=%s AND sampled_minute<%s",
                    (self.source, self.cutoff))

    def test_cleanup_is_owner_defined_with_safe_search_path_and_no_public_execute(self):
        function = self.admin.execute("""SELECT p.prosecdef,r.rolname,p.proconfig,
            pg_get_function_identity_arguments(p.oid) AS arguments,
            EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_execute
            FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
            WHERE p.oid='ops.cleanup_presence_history(text,bigint)'::regprocedure""").fetchone()
        self.assertTrue(function["prosecdef"])
        self.assertEqual(function["rolname"], "archive_owner")
        self.assertIn("search_path=pg_catalog, pg_temp", function["proconfig"])
        self.assertEqual(function["arguments"], "p_source_key text, p_account_id bigint")
        self.assertFalse(function["public_execute"])
        for role in ("archive_reader", "archive_ingest", "archive_quality", "archive_assignment"):
            with self.subTest(role=role):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.admin.transaction():
                        self.admin.execute("SET LOCAL ROLE " + role)
                        self.admin.execute("SELECT * FROM ops.cleanup_presence_history(%s,1)", (self.source,))

    def test_cleanup_rejects_invalid_scope_without_deleting_any_rows(self):
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        for source, account in ((None, 1), ("", 1), (" padded ", 1), ("a"*129, 1),
            (self.source, None), (self.source, 0), (self.source, -1)):
            with self.subTest(source=source, account=account):
                with self.assertRaises(psycopg.errors.InvalidParameterValue):
                    self.writer.execute("SELECT * FROM ops.cleanup_presence_history(%s,%s)", (source, account))
        self.assertEqual(self.sample_minutes(), [old])

    def test_competing_scope_lock_requests_retry_instead_of_false_completion(self):
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        with self.admin.transaction():
            self.admin.execute("SELECT pg_advisory_xact_lock(175608,hashtext(%s||':'||1::text))", (self.source,))
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                self.cleanup()
            self.assertEqual(self.sample_minutes(), [old])
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 0})

    def test_locked_expired_row_rolls_back_entire_batch_and_allows_later_retry(self):
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        self.period(old-timedelta(minutes=1), old)
        for table in ("archive.agent_presence_samples", "ops.agent_presence_periods"):
            with self.subTest(locked_table=table):
                with self.admin.transaction():
                    self.admin.execute("SELECT * FROM " + table + " WHERE source_key=%s FOR UPDATE", (self.source,))
                    with self.assertRaises(psycopg.errors.LockNotAvailable):
                        self.cleanup()
                    # A period lock occurs after sample deletion; its timeout
                    # must roll that deletion back as part of the same call.
                    self.assertEqual(self.sample_minutes(), [old])
                    self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s",
                        (self.source,)).fetchone()["n"], 1)
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 1})

    def test_cleanup_transaction_rolls_back_both_sample_and_period_deletion(self):
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        self.period(old-timedelta(minutes=1), old)
        with self.assertRaisesRegex(RuntimeError, "injected rollback"):
            with self.writer.transaction():
                self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 1})
                raise RuntimeError("injected rollback")
        self.assertEqual(self.sample_minutes(), [old])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s",
            (self.source,)).fetchone()["n"], 1)
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 1})


if __name__ == "__main__":
    unittest.main()
