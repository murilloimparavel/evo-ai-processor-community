# Synthetic cross-language contract fixture. No database or real credentials.
require 'bundler/setup'
require 'json'
require 'active_support/all'
require File.join(ENV.fetch('KNOWLEDGE_CRM_ROOT'), 'app/services/knowledge_runtime/grant_issuer')

input = JSON.parse(STDIN.read)
ENV['KNOWLEDGE_RUNTIME_GRANTS_ENABLED'] = 'true'
ENV['KNOWLEDGE_RUNTIME_GRANT_ISSUER'] = 'crm'
ENV['KNOWLEDGE_RUNTIME_GRANT_AUDIENCE'] = 'knowledge-retrieval'
ENV['KNOWLEDGE_RUNTIME_GRANT_KID'] = input.fetch('kid')
ENV['KNOWLEDGE_RUNTIME_GRANT_PRIVATE_KEY'] = input.fetch('private_key')
ENV['KNOWLEDGE_RUNTIME_ACCOUNT_MODE'] = 'community-single'
ENV['KNOWLEDGE_RUNTIME_ACCOUNT_SCOPE'] = 'account-1'

module EvoExtensionPoints
  def self.impl_for(_key) = nil
end
class RuntimeConfig
  def self.account = { 'id' => 'account-1' }
end

ConversationRecord = Struct.new(:id, :inbox_id, :contact_id)
MessageRecord = Struct.new(:id) do
  def incoming? = true
  def created_at = Time.current
  def private? = false
end
BotRecord = Struct.new(:id, :outgoing_url, :bot_config)
AssignmentRecord = Struct.new(:agent_bot_id)
module AgentBots
  class SessionSyncAgentResolver
    def initialize(bot) = @bot = bot
    def call = @bot.bot_config.fetch('agent_id')
  end
end
class Conversation
  def self.find_by(id:) = ConversationRecord.new(id, $input.fetch('inbox_id'), $input.fetch('contact_id'))
end
class Message
  def self.find_by(id:, conversation_id:, inbox_id:) = MessageRecord.new(id)
end
class AgentBotInbox
  def self.where(inbox_id:, status:) = self
  def self.limit(_limit) = self
  def self.to_a = [AssignmentRecord.new($input.fetch('bot_id'))]
end
class AgentBot
  def self.find_by(id:)
    root = $input.fetch('root_agent_id')
    BotRecord.new(id, "https://processor.example/api/v1/a2a/#{root}", { 'agent_id' => root })
  end
end
$input = input
print KnowledgeRuntime::GrantIssuer.issue(conversation_id: input.fetch('conversation_id'),
                                          message_id: input.fetch('event_id'), bot_id: input.fetch('bot_id'),
                                          request_sha256: input['request_sha256'])
