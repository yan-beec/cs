# Mount in BOTH Chatwoot Rails and Sidekiq:
# /app/config/initializers/ics_csat_rounds.rb
# Only messages created by ICS with an ics_round_id use these extensions.
module IcsCsatRoundMessages
  def create
    attributes = params[:content_attributes]
    return super unless params[:content_type] == 'input_csat'
    return super unless attributes.is_a?(ActionController::Parameters) || attributes.is_a?(Hash)
    return super if attributes[:ics_round_id].blank?

    # Reuse the existing authenticated/authorized message endpoint and renderer.
    # A timed-out request and its retry cannot create two cards for one round.
    @conversation.with_lock do
      @message = @conversation.messages.input_csat
                              .where("content_attributes ->> 'ics_round_id' = ?", attributes[:ics_round_id]).first
      super if @message.nil?
    end
  end
end

module IcsCsatRoundResponses
  private

  def process_csat_response(conversation, rating, feedback_message)
    attributes = message.content_attributes
    return super unless attributes['ics_round_id'].present? && attributes.key?('ics_assigned_agent_id')

    # The widget can only submit rating/feedback; it cannot change this snapshot.
    agent = conversation.account.users.find_by(id: attributes['ics_assigned_agent_id'])
    response = message.csat_survey_response || CsatSurveyResponse.new(
      message_id: message.id, account_id: message.account_id,
      conversation_id: message.conversation_id, contact_id: conversation.contact_id,
      assigned_agent: agent
    )
    response.update!(rating: rating, feedback_message: feedback_message)
    response
  end
end

Rails.application.config.to_prepare do
  controller = Api::V1::Accounts::Conversations::MessagesController
  controller.prepend(IcsCsatRoundMessages) unless controller < IcsCsatRoundMessages
  builder = CsatSurveys::ResponseBuilder
  builder.prepend(IcsCsatRoundResponses) unless builder < IcsCsatRoundResponses
end
