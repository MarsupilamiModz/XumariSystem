-- 006_economy: balances, cooldowns, transactions (games use the same ledger)

CREATE TABLE economy_users (
    guild_id      BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL,
    balance       BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
    total_earned  BIGINT NOT NULL DEFAULT 0,
    total_lost    BIGINT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
CREATE TRIGGER trg_economy_users_updated BEFORE UPDATE ON economy_users FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_economy_users_balance ON economy_users (guild_id, balance DESC);

CREATE TABLE economy_cooldowns (
    guild_id      BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL,
    action        TEXT NOT NULL,
    available_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (guild_id, user_id, action)
);

CREATE TABLE economy_transactions (
    id             BIGSERIAL PRIMARY KEY,
    guild_id       BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id        BIGINT NOT NULL,
    amount         BIGINT NOT NULL,
    balance_after  BIGINT NOT NULL,
    kind           TEXT NOT NULL,
    reference      TEXT,
    actor_id       BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_economy_tx_user ON economy_transactions (guild_id, user_id, created_at DESC);
CREATE INDEX idx_economy_tx_kind ON economy_transactions (guild_id, kind, created_at DESC);
