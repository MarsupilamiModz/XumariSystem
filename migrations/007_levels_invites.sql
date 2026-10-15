-- 007_levels_invites: XP/levels, level role rewards, invite tracking

CREATE TABLE levels (
    guild_id     BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL,
    xp           BIGINT NOT NULL DEFAULT 0 CHECK (xp >= 0),
    level        INT NOT NULL DEFAULT 0,
    messages     INT NOT NULL DEFAULT 0,
    last_xp_at   TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX idx_levels_xp ON levels (guild_id, xp DESC);

CREATE TABLE level_rewards (
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    level       INT NOT NULL CHECK (level > 0),
    role_id     BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, level, role_id)
);

CREATE TABLE invites (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    invitee_id  BIGINT NOT NULL,
    inviter_id  BIGINT,
    code        TEXT,
    fake        BOOLEAN NOT NULL DEFAULT FALSE,
    rejoin      BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at     TIMESTAMPTZ
);
CREATE INDEX idx_invites_inviter ON invites (guild_id, inviter_id);
CREATE INDEX idx_invites_invitee ON invites (guild_id, invitee_id, joined_at DESC);
