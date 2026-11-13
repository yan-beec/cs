-- 同一只读快照内按主键区间登记候选，完整契约仍由投影校验。
SELECT e.source_key, e.account_id, e.conversation_id,
       array_agg(DISTINCT e.event_type ORDER BY e.event_type) AS evidence_types,
       max(e.id) AS last_evidence_id
FROM archive.events e
WHERE e.id > %(lower_id)s AND e.id <= %(upper_id)s
  AND e.source_key = %(source_key)s AND e.account_id = %(account_id)s
  AND e.conversation_id IS NOT NULL AND e.ingestion_kind = 'backfill'
  AND ((e.event_type = 'ics_assignment_recorded'
        AND e.payload #>> '{assignment,origin}' = 'ics'
        AND e.payload #>> '{assignment,schema_version}' = '1'
        AND e.payload #>> '{assignment,evidence_kind}' = 'assignment_applied'
        AND NULLIF(e.payload #>> '{assignment,assigned_at}', '') IS NOT NULL)
       OR e.event_type = 'ics_handoff_queue_decided')
GROUP BY e.source_key, e.account_id, e.conversation_id;
