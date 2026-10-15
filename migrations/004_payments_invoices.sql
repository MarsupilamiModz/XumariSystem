-- 004_payments_invoices: Stripe payments, webhook idempotency, invoices

CREATE TABLE payments (
    id                BIGSERIAL PRIMARY KEY,
    order_id          BIGINT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    guild_id          BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    provider          TEXT NOT NULL DEFAULT 'stripe',
    kind              TEXT NOT NULL CHECK (kind IN ('checkout', 'payment_link', 'manual')),
    external_id       TEXT,
    url               TEXT,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'paid', 'expired', 'failed', 'refunded', 'cancelled')),
    amount_cents      INT NOT NULL,
    currency          TEXT NOT NULL,
    payment_intent    TEXT,
    customer_email    TEXT,
    customer_name     TEXT,
    customer_address  JSONB,
    expires_at        TIMESTAMPTZ,
    paid_at           TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_payments_updated BEFORE UPDATE ON payments FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE UNIQUE INDEX uq_payments_external ON payments (provider, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX idx_payments_order ON payments (order_id, created_at DESC);
CREATE INDEX idx_payments_intent ON payments (payment_intent) WHERE payment_intent IS NOT NULL;

-- every processed Stripe event id (webhook idempotency)
CREATE TABLE stripe_events (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE invoice_counters (
    guild_id  BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    year      INT NOT NULL,
    last      INT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, year)
);

-- invoices are retained for the statutory retention period (not removed by GDPR deletion)
CREATE TABLE invoices (
    id                BIGSERIAL PRIMARY KEY,
    order_id          BIGINT NOT NULL UNIQUE REFERENCES orders (id) ON DELETE RESTRICT,
    guild_id          BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    number            TEXT NOT NULL,
    issued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    customer_name     TEXT,
    customer_email    TEXT,
    customer_address  JSONB,
    net_cents         INT NOT NULL,
    tax_cents         INT NOT NULL,
    gross_cents       INT NOT NULL,
    tax_rate          NUMERIC(5, 2) NOT NULL DEFAULT 0,
    currency          TEXT NOT NULL,
    locale            TEXT NOT NULL DEFAULT 'de',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, number)
);
