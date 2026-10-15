-- 005_reviews: customer reviews after completed orders

CREATE TABLE reviews (
    id                   BIGSERIAL PRIMARY KEY,
    guild_id             BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    order_id             BIGINT NOT NULL UNIQUE REFERENCES orders (id) ON DELETE CASCADE,
    ticket_id            BIGINT REFERENCES tickets (id) ON DELETE SET NULL,
    user_id              BIGINT NOT NULL,
    designer_id          BIGINT,
    rating               SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    feedback             TEXT,
    stat_messages        INT NOT NULL DEFAULT 0,
    stat_participants    INT NOT NULL DEFAULT 0,
    stat_attachments     INT NOT NULL DEFAULT 0,
    channel_id           BIGINT,
    message_id           BIGINT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_reviews_guild_created ON reviews (guild_id, created_at DESC);
CREATE INDEX idx_reviews_designer ON reviews (designer_id) WHERE designer_id IS NOT NULL;
CREATE INDEX idx_reviews_user ON reviews (guild_id, user_id);
