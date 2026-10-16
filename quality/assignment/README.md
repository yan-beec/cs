# ICS assignment evidence delivery

ICS saves its selected agent only after the assignment API succeeds and the local
success mark commits. The mark verifies the business round and selected agent,
so a late response from a previous round cannot record ownership for a new round.
The journal key is `(account_id, conversation_id, business_round_id)`; retries
never overwrite the first snapshot. Chatwoot changes and reply senders do not
change this evidence.

`chatwoot_assignment_outbox` is created by the existing ICS state initialization.
Startup may snapshot a currently active, successful assignment with a valid
trigger and session. These upgrade snapshots use
`evidence_kind=current_control_backfill`, `assigned_at=null`, and an explicit
`recorded_at`; ended historical assignments are not inferred.
The persisted `handoff_round_id` must equal the current business round. Processing
timestamps cannot establish this relationship. Older controls without that proof
remain unknown, and an idle rollover retains the original handoff round, so older
handoff flags cannot create a new assignment snapshot.
If the initial source-schema initialization fails, a temporary background worker
retries with its own pool of at most one connection and bounded timeouts. Existing
webhook workers start normally and retry failed source transactions. Recovery
releases its pool and thread after success; a normal startup allocates neither.

`ARCHIVE_ASSIGNMENT_SYNC_ENABLED` defaults to false. When enabled, configure a
stable `ARCHIVE_SOURCE_KEY`, explicit `ARCHIVE_ALLOWED_ACCOUNT_IDS`,
`ARCHIVE_ASSIGNMENT_USER` (default `archive_assignment`),
`ARCHIVE_ASSIGNMENT_PASSWORD`, and the existing `ARCHIVE_POSTGRES_*` settings.
The source connection reuses the ICS application's existing database settings,
through a separate pool. No ICS database credential is added to archive workers.
The synchronization credential must have the restricted permissions provided by
the assignment migration; it must not own the archive or modify projections.
The ingestion password is not used by this module.

One background thread polls every five seconds, with bounded failure backoff.
Source and target each have a separate pool with at most one connection and one
waiter. Connection attempts have a three-second timeout, statements two seconds,
and lock acquisition one second. A pass delivers at most twenty records, checking
shutdown between records. Configuration errors and archive failures do not block
ICS startup or handoff.

Delivery appends a deterministic `ics_assignment_recorded` backfill event and its
processing job in one archive transaction. Only after that commit does the source
receive `delivered_at`. A crash or failed source acknowledgement retries the same
event key and raw JSON. All serialized assignment timestamps are normalized to
UTC, including the source timestamp, so a database session timezone change does
not alter the bytes on retry. Unknown historical assignment times remain null.
Original conversation messages are not rewritten.

## Isolated evidence and recovery

Only an explicitly invalid source observation or different immutable evidence
under the same event key is isolated. The source row receives
`delivery_error_code=invalid_evidence` or `evidence_conflict` and
`delivery_failed_at`; its original observation fields and `delivered_at=null`
remain unchanged. Existing archive evidence is never overwritten. Later valid
rows continue in the same bounded pass, and subsequent passes skip isolated rows
without increasing the twenty-record limit or using an expanding offset. The
isolation metadata survives process restarts. Connection errors, timeouts,
transaction failures and failed source metadata writes are retried with backoff;
they are not classified as bad evidence.

The status endpoint reports `assignment_sync_status=degraded` whenever isolated
rows exist. It includes `assignment_sync_pending_count`,
`assignment_sync_blocked_count` and `assignment_sync_counts_sampled_at` (UTC).
Counts are sampled from the configured accounts at most once per sixty seconds
and adjusted for this worker's committed deliveries and isolations between
samples. Newly arriving source records and changes made by another worker appear
at the next sample. A database failure reports `unavailable`, retaining the last
known counts; archive queries can still remain available independently. No
credentials, source content or exception detail is written into error metadata.

An operator must review both the original source observation and the existing
archive event, using the source account, conversation and business round to find
the exact row. Retain that evidence and establish whether the problem was a
validation defect, configuration error, serialization difference or a real
assignment conflict before retrying. Repair the processing or configuration
cause first. Do not edit the archived raw bytes or the source observation fields
to make conflicting evidence agree. A real unresolved conflict remains isolated
for explicit reconciliation.

Once that review proves a retry can safely succeed, clear only
`delivery_error_code` and `delivery_failed_at` for the specific still-undelivered
source row in a controlled source-database transaction. This requeues the same
original observation; it does not acknowledge it or create a replacement
observation. Verify the following source acknowledgement and status counts. A
remaining validation error or evidence conflict will isolate the row again.
