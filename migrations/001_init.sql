-- 001_init: core tables (guilds, settings, logs, event outbox, API keys)

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE guilds (
    guild_id    BIGINT PRIMARY KEY,
    name        TEXT,
    locale      TEXT NOT NULL DEFAULT 'de' CHECK (locale IN ('de', 'en')),
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_guilds_updated BEFORE UPDATE ON guilds FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE guild_settings (
    guild_id       BIGINT PRIMARY KEY REFERENCES guilds (guild_id) ON DELETE CASCADE,
    features       JSONB NOT NULL DEFAULT '{}'::jsonb,
    opening_hours  JSONB,
    settings       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_guild_settings_updated BEFORE UPDATE ON guild_settings FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE logs (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor_id    BIGINT,
    target_id   BIGINT,
    channel_id  BIGINT,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_logs_guild_created ON logs (guild_id, created_at DESC);
CREATE INDEX idx_logs_category ON logs (guild_id, category, created_at DESC);
CREATE INDEX idx_logs_actor ON logs (actor_id) WHERE actor_id IS NOT NULL;

-- Transactional outbox: API → bot events (LISTEN/NOTIFY + polling)
CREATE TABLE bot_events (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT,
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts      INT NOT NULL DEFAULT 0,
    last_error    TEXT,
    claimed_at    TIMESTAMPTZ,
    processed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_bot_events_pending ON bot_events (id) WHERE processed_at IS NULL;

-- API keys (only the SHA-256 hash is stored)
CREATE TABLE api_keys (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT REFERENCES guilds (guild_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    key_prefix    TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    scopes        TEXT[] NOT NULL DEFAULT ARRAY['orders:read', 'orders:write', 'customers:read', 'customers:write'],
    created_by    BIGINT,
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_api_keys_guild ON api_keys (guild_id);
