-- 009_downloads: downloads (mods, files, releases) and click tracking

CREATE TABLE downloads (
    id                 BIGSERIAL PRIMARY KEY,
    guild_id           BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    title              TEXT NOT NULL,
    description_de     TEXT,
    description_en     TEXT,
    download_url       TEXT NOT NULL,
    password           TEXT,
    youtube_url        TEXT,
    youtube_id         TEXT,
    preview_image_url  TEXT,
    category           TEXT,
    tags               TEXT[] NOT NULL DEFAULT '{}',
    version            TEXT,
    file_size          TEXT,
    channel_id         BIGINT,
    required_role_id   BIGINT,
    message_id         BIGINT,
    embed_message_id   BIGINT,
    click_count        INT NOT NULL DEFAULT 0,
    published_at       TIMESTAMPTZ,
    created_by         BIGINT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_downloads_updated BEFORE UPDATE ON downloads FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_downloads_guild ON downloads (guild_id, created_at DESC);
CREATE INDEX idx_downloads_message ON downloads (message_id) WHERE message_id IS NOT NULL;

CREATE TABLE download_clicks (
    id           BIGSERIAL PRIMARY KEY,
    download_id  BIGINT NOT NULL REFERENCES downloads (id) ON DELETE CASCADE,
    guild_id     BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_download_clicks_download ON download_clicks (download_id, created_at DESC);
CREATE INDEX idx_download_clicks_guild ON download_clicks (guild_id, created_at DESC);
