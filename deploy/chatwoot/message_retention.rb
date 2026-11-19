# Run with Chatwoot's `bundle exec rails runner -`; never load as an initializer.
# Each message commits separately so locks and the cleanup budget stay bounded.
module IcsChatwootMessageRetention
  LOCK_KEY = [1_756_080, 60].freeze

  def self.run
    days = Integer(ENV.fetch('CHATWOOT_RETENTION_DAYS', '60'), 10)
    limit = Integer(ENV.fetch('CHATWOOT_RETENTION_BATCH_SIZE', '500'), 10)
    mode = ENV.fetch('CHATWOOT_RETENTION_DRY_RUN', '1')
    raw_ids = ENV.fetch('CHATWOOT_RETENTION_ACCOUNT_IDS', '')
    raise ArgumentError, 'Provide comma-separated positive account IDs' unless raw_ids.match?(/\A[1-9]\d*(,[1-9]\d*)*\z/)
    raise ArgumentError, 'Retention must be at least 60 days' unless days.between?(60, 36_500)
    raise ArgumentError, 'Batch size must be between 1 and 5000' unless limit.between?(1, 5_000)
    raise ArgumentError, 'DRY_RUN must be 1 (preview) or 0 (delete)' unless %w[0 1].include?(mode)

    account_ids = raw_ids.split(',').map { |value| Integer(value, 10) }.uniq
    missing = account_ids - Account.where(id: account_ids).pluck(:id)
    raise ArgumentError, "Unknown account IDs: #{missing.join(',')}" unless missing.empty?

    dry_run = mode == '1'
    attachment_relation = Message.reflect_on_association(:attachments)
    file_relation = Attachment.reflect_on_attachment(:file)
    unless attachment_relation&.options&.[](:dependent) == :destroy &&
           file_relation&.options&.[](:dependent) == :purge_later
      raise 'Unsupported attachment deletion callbacks; review the installed Chatwoot version'
    end
    unless ActiveJob::Base.queue_adapter.is_a?(ActiveJob::QueueAdapters::SidekiqAdapter)
      raise 'Sidekiq must handle attachment purge jobs'
    end
    # Chatwoot only reindexes create/update by default. Do not leave searchable
    # copies of deleted bodies in an external search index.
    if !dry_run && ChatwootApp.advanced_search_allowed?
      raise 'External search is enabled; add coordinated search-index deletion before applying retention'
    end

    cutoff = Time.current - days.days
    puts "mode=#{dry_run ? 'preview' : 'delete'} accounts=#{account_ids.join(',')} cutoff_utc=#{cutoff.utc.iso8601} limit=#{limit}"
    puts 'Policy: message creation and last edit must both be older than the cutoff; recent attachments defer the whole message.'

    Message.connection_pool.with_connection do |connection|
      raise 'This script requires PostgreSQL' unless connection.adapter_name == 'PostgreSQL'
      locked = connection.select_value("SELECT pg_try_advisory_lock(#{LOCK_KEY.join(',')})")
      raise 'Another message-retention run is already active' unless locked

      old_statement_timeout = connection.select_value('SHOW statement_timeout')
      begin
        connection.execute("SET statement_timeout = '15s'")
        # Recent edits/uploads are content too: give them a full retention window.
        recent_attachments = Attachment.where(account_id: account_ids)
                                       .where('created_at >= ? OR updated_at >= ?', cutoff, cutoff).select(:message_id)
        scope = Message.where(account_id: account_ids)
                       .where('messages.created_at < ? AND messages.updated_at < ?', cutoff, cutoff)
                       .where.not(id: recent_attachments)
        candidates = scope.count
        rows = scope.reorder(:created_at, :id).limit(limit).pluck(:id, :account_id, :conversation_id, :created_at)
        attachment_count = Attachment.where(message_id: rows.map(&:first)).count
        puts "eligible_messages=#{candidates} selected_messages=#{rows.length} selected_attachments=#{attachment_count}"

        deleted = 0
        skipped = 0
        started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        rows.each do |id, account_id, conversation_id, created_at|
          if Process.clock_gettime(Process::CLOCK_MONOTONIC) - started >= 120
            puts 'Runtime limit reached; remaining messages will be reconsidered next time.'
            break
          end
          if dry_run
            puts "preview account_id=#{account_id} message_id=#{id} conversation_db_id=#{conversation_id} created_at=#{created_at.utc.iso8601}"
            next
          end

          # Recheck age and account under a row lock. A concurrently edited,
          # moved or already deleted message must not use the stale preview.
          removed = Message.transaction do
            connection.execute("SET LOCAL lock_timeout = '2s'")
            message = scope.where(id: id).lock('FOR UPDATE SKIP LOCKED').first
            next false unless message

            # destroy! runs attachment/notification/CSAT association callbacks.
            # Do not use delete_all, raw DELETE, or the UI's soft-delete update.
            message.destroy!
            true
          end
          if removed
            deleted += 1
            puts "deleted account_id=#{account_id} message_id=#{id}"
          else
            skipped += 1
          end
        end
        remaining = scope.count
        puts "completed mode=#{dry_run ? 'preview' : 'delete'} deleted_messages=#{deleted} skipped_messages=#{skipped} remaining_eligible=#{remaining}"
        puts 'Attachment file deletion is asynchronous; check Sidekiq before considering disk space reclaimed.'
      ensure
        connection.execute("SET statement_timeout = #{connection.quote(old_statement_timeout)}")
        connection.execute("SELECT pg_advisory_unlock(#{LOCK_KEY.join(',')})")
      end
    end
  end
end

begin
  IcsChatwootMessageRetention.run
rescue StandardError => error
  # SQL exceptions can contain customer message bodies; keep them out of cron logs.
  warn "Retention stopped: #{error.class}. Previously committed deletions remain committed."
  warn error.message if error.is_a?(ArgumentError) || error.instance_of?(RuntimeError)
  exit 1
end
