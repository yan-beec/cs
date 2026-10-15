# Run standalone in Chatwoot's Ruby environment; no Rails boot, DB or network.
require 'minitest/autorun'
require 'active_support/all'
require 'thread'

module ActionController
  class Parameters < Hash; end
end

module Rails
  def self.application = self
  def self.config = self
  def self.to_prepare = yield
end

module Api
  module V1
    module Accounts
      module Conversations
        class MessagesController
          attr_accessor :params
          attr_reader :message, :normal_calls

          def initialize(conversation, attributes)
            @conversation = conversation
            @params = attributes
            @normal_calls = 0
          end

          def create
            @normal_calls += 1
            @message = @conversation.messages.add(params[:content_attributes] || {})
          end
        end
      end
    end
  end
end

class FakeMessages
  attr_reader :records
  def initialize = @records = []
  def input_csat = self

  def where(_sql, round_id)
    records.select { |record| record[:ics_round_id] == round_id }
  end

  def add(attributes)
    records << attributes.dup
    records.last
  end
end

class FakeUsers
  def find_by(id:) = id
end

class FakeConversation
  attr_reader :messages, :account
  attr_accessor :assignee, :contact_id

  def initialize
    @messages = FakeMessages.new
    @mutex = Mutex.new
    @account = Struct.new(:users).new(FakeUsers.new)
    @assignee = 22
    @contact_id = 42
  end

  def with_lock(&block) = @mutex.synchronize(&block)
end

class CsatSurveyResponse
  attr_reader :attributes
  def initialize(**attributes) = @attributes = attributes
  def update!(**attributes) = @attributes.merge!(attributes)
end

module CsatSurveys
  class ResponseBuilder
    attr_reader :message, :normal_calls

    def initialize(message)
      @message = message
      @normal_calls = 0
    end

    def process_csat_response(*args)
      @normal_calls += 1
      :original_behavior
    end
  end
end

# The verification runner inserts the initializer here before the tests below.
require_relative '../deploy/chatwoot/ics_csat_rounds' unless defined?(IcsCsatRoundMessages)

class IcsCsatPatchTest < Minitest::Test
  def controller(conversation, round_id)
    Api::V1::Accounts::Conversations::MessagesController.new(conversation, {
      content_type: 'input_csat', content_attributes: { ics_round_id: round_id }
    })
  end

  def test_retries_return_the_same_card_but_another_round_gets_a_new_card
    conversation = FakeConversation.new
    first, retry_request, second = %w[a a b].map { |round| controller(conversation, round) }
    [first, retry_request, second].each(&:create)
    assert_equal 2, conversation.messages.records.size
    assert_equal 0, retry_request.normal_calls
    assert_same first.message, retry_request.message
  end

  def test_concurrent_requests_create_only_one_card
    conversation = FakeConversation.new
    requests = Array.new(8) { controller(conversation, 'one-round') }
    requests.map { |request| Thread.new { request.create } }.each(&:join)
    assert_equal 1, conversation.messages.records.size
    assert_equal 1, requests.sum(&:normal_calls)
  end

  def test_ordinary_messages_and_existing_csat_without_round_are_unchanged
    conversation = FakeConversation.new
    ordinary = Api::V1::Accounts::Conversations::MessagesController.new(conversation, {
      content_type: 'text', content_attributes: { ics_round_id: 'ignored' }
    })
    existing = controller(conversation, nil)
    [ordinary, existing].each(&:create)
    assert_equal 1, ordinary.normal_calls
    assert_equal 1, existing.normal_calls
  end

  def test_rating_uses_closed_round_agent_even_after_new_assignment
    conversation = FakeConversation.new
    message = Struct.new(:id, :account_id, :conversation_id, :content_attributes, :csat_survey_response).new(
      101, 1, 2, { 'ics_round_id' => 'a', 'ics_assigned_agent_id' => 11 }, nil
    )
    builder = CsatSurveys::ResponseBuilder.new(message)
    first = builder.send(:process_csat_response, conversation, 5, 'first round')
    assert_equal 11, first.attributes[:assigned_agent]
    assert_equal 101, first.attributes[:message_id]
    message.csat_survey_response = first
    second = builder.send(:process_csat_response, conversation, 4, 'updated feedback')
    assert_same first, second
    assert_equal 4, second.attributes[:rating]
    assert_equal 0, builder.normal_calls
  end

  def test_existing_card_uses_original_response_builder
    message = Struct.new(:content_attributes).new({})
    builder = CsatSurveys::ResponseBuilder.new(message)
    assert_equal :original_behavior, builder.send(:process_csat_response, FakeConversation.new, 5, '')
    assert_equal 1, builder.normal_calls
  end
end
