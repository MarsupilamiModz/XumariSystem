-- 010_cv2: Components V2 builder templates and the messages sent from them

CREATE TABLE cv2_templates (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{"accent": null, "blocks": []}'::jsonb,
    created_by  BIGINT,
    updated_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_cv2_templates_name ON cv2_templates (guild_id, lower(name));
CREATE TRIGGER trg_cv2_templates_updated BEFORE UPDATE ON cv2_templates FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE cv2_messages (
    id           BIGSERIAL PRIMARY KEY,
    template_id  BIGINT NOT NULL REFERENCES cv2_templates (id) ON DELETE CASCADE,
    guild_id     BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    channel_id   BIGINT NOT NULL,
    message_id   BIGINT NOT NULL UNIQUE,
    sent_by      BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cv2_messages_template ON cv2_messages (template_id);
