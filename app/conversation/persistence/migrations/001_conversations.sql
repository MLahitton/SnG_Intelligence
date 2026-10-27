CREATE TABLE intelligence_conversations.conversations (
    conversation_id text PRIMARY KEY CHECK (conversation_id ~ '^[A-Za-z0-9_-]{43}$'),
    subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_activity_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0)
);
CREATE UNIQUE INDEX one_open_conversation_per_subject
    ON intelligence_conversations.conversations(subject) WHERE closed_at IS NULL;
CREATE TABLE intelligence_conversations.turns (
    conversation_id text NOT NULL REFERENCES intelligence_conversations.conversations(conversation_id),
    ordinal bigint NOT NULL CHECK (ordinal > 0),
    modality text NOT NULL CHECK (modality IN ('text', 'voice')),
    user_content text NOT NULL,
    assistant_content text NOT NULL,
    response_type text NOT NULL,
    "references" jsonb NOT NULL CHECK (jsonb_typeof("references") = 'array'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (conversation_id, ordinal)
);
