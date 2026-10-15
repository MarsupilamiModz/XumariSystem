-- 008_community: giveaways, reaction roles, reminders, role sync, captcha, modmail

CREATE TABLE giveaways (
    id                BIGSERIAL PRIMARY KEY,
    guild_id          BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    channel_id        BIGINT NOT NULL,
    message_id        BIGINT,
    host_id           BIGINT,
    prize             TEXT NOT NULL,
    description       TEXT,
    winners           INT NOT NULL DEFAULT 1 CHECK (winners BETWEEN 1 AND 50),
    required_role_id  BIGINT,
    locale            TEXT NOT NULL DEFAULT 'de',
    ends_at           TIMESTAMPTZ NOT NULL,
    ended             BOOLEAN NOT NULL DEFAULT FALSE,
    retry_at          TIMESTAMPTZ,
    winner_ids        BIGINT[] NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_giveaways_due ON giveaways (ends_at) WHERE NOT ended;
CREATE INDEX idx_giveaways_guild ON giveaways (guild_id, created_at DESC);

CREATE TABLE giveaway_entries (
    giveaway_id  BIGINT NOT NULL REFERENCES giveaways (id) ON DELETE CASCADE,
    guild_id     BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (giveaway_id, user_id)
);

CREATE TABLE reaction_role_panels (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    channel_id   BIGINT,
    message_id   BIGINT,
    title        TEXT NOT NULL,
    description  TEXT,
    mode         TEXT NOT NULL DEFAULT 'buttons' CHECK (mode IN ('buttons', 'select')),
    created_by   BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reaction_roles (
    id           BIGSERIAL PRIMARY KEY,
    panel_id     BIGINT NOT NULL REFERENCES reaction_role_panels (id) ON DELETE CASCADE,
    guild_id     BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    role_id      BIGINT NOT NULL,
    label        TEXT NOT NULL,
    emoji        TEXT,
    description  TEXT,
    position     INT NOT NULL DEFAULT 0,
    UNIQUE (panel_id, role_id)
);

CREATE TABLE reminders (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL,
    channel_id    BIGINT,
    text          TEXT NOT NULL,
    dm            BOOLEAN NOT NULL DEFAULT TRUE,
    locale        TEXT NOT NULL DEFAULT 'de',
    remind_at     TIMESTAMPTZ NOT NULL,
    delivered_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_reminders_due ON reminders (remind_at) WHERE delivered_at IS NULL;
CREATE INDEX idx_reminders_user ON reminders (guild_id, user_id) WHERE delivered_at IS NULL;

CREATE TABLE role_sync (
    id               BIGSERIAL PRIMARY KEY,
    source_guild_id  BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    source_role_id   BIGINT NOT NULL,
    target_guild_id  BIGINT NOT NULL,
    target_role_id   BIGINT NOT NULL,
    created_by       BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_role_id, target_guild_id, target_role_id)
);

CREATE TABLE captcha_challenges (
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    code        TEXT NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE modmail_threads (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    thread_id   BIGINT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ,
    closed_by   BIGINT
);
CREATE UNIQUE INDEX uq_modmail_open ON modmail_threads (guild_id, user_id) WHERE status = 'open';
CREATE INDEX idx_modmail_thread ON modmail_threads (thread_id);
