-- 002_tickets: ticket system (categories, tickets, members, stats, transcripts, blacklist, holidays)

CREATE TABLE ticket_categories (
    id                   BIGSERIAL PRIMARY KEY,
    guild_id             BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    key                  TEXT NOT NULL,
    name_de              TEXT NOT NULL,
    name_en              TEXT NOT NULL,
    emoji                TEXT,
    discord_category_id  BIGINT,
    is_order             BOOLEAN NOT NULL DEFAULT FALSE,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order           INT NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, key)
);
CREATE TRIGGER trg_ticket_categories_updated BEFORE UPDATE ON ticket_categories FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE tickets (
    id                  BIGSERIAL PRIMARY KEY,
    code                CHAR(8) NOT NULL UNIQUE,
    guild_id            BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    category_id         BIGINT NOT NULL REFERENCES ticket_categories (id) ON DELETE RESTRICT,
    channel_id          BIGINT UNIQUE,
    owner_id            BIGINT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'deleted')),
    locale              TEXT NOT NULL DEFAULT 'de',
    answers             JSONB NOT NULL DEFAULT '[]'::jsonb,
    welcome_message_id  BIGINT,
    claimed_by          BIGINT,
    last_activity_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    reminder_sent_at    TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    closed_by           BIGINT,
    close_reason        TEXT,
    deleted_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_tickets_updated BEFORE UPDATE ON tickets FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_tickets_guild_status ON tickets (guild_id, status);
CREATE INDEX idx_tickets_owner_status ON tickets (owner_id, status);
CREATE INDEX idx_tickets_activity ON tickets (status, last_activity_at);
CREATE INDEX idx_tickets_created ON tickets (guild_id, created_at DESC);

CREATE TABLE ticket_members (
    ticket_id  BIGINT NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL,
    added_by   BIGINT,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticket_id, user_id)
);

-- Per participant message statistics (aggregated for reviews / stats)
CREATE TABLE ticket_messages_stats (
    ticket_id         BIGINT NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    user_id           BIGINT NOT NULL,
    is_staff          BOOLEAN NOT NULL DEFAULT FALSE,
    message_count     INT NOT NULL DEFAULT 0,
    attachment_count  INT NOT NULL DEFAULT 0,
    first_message_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticket_id, user_id)
);

CREATE TABLE transcripts (
    id             BIGSERIAL PRIMARY KEY,
    ticket_id      BIGINT NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    storage        TEXT NOT NULL CHECK (storage IN ('r2', 'local', 'discord')),
    url            TEXT,
    file_name      TEXT NOT NULL,
    size_bytes     INT NOT NULL DEFAULT 0,
    message_count  INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_transcripts_ticket ON transcripts (ticket_id);

CREATE TABLE blacklist (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    reason      TEXT NOT NULL,
    created_by  BIGINT,
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    revoked_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_blacklist_active ON blacklist (guild_id, user_id) WHERE revoked_at IS NULL;
CREATE INDEX idx_blacklist_guild ON blacklist (guild_id, created_at DESC);

CREATE TABLE holidays (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    name        TEXT NOT NULL,
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, day)
);
