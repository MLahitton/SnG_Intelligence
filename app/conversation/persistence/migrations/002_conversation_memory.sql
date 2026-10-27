CREATE TABLE intelligence_conversations.conversation_memory (
    conversation_id text PRIMARY KEY REFERENCES intelligence_conversations.conversations(conversation_id),
    compacted_through bigint NOT NULL CHECK (compacted_through >= 0),
    revision bigint NOT NULL CHECK (revision >= 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);
