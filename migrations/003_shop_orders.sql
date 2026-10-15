-- 003_shop_orders: products, bundles, coupons, orders, todos, queue, showcases

CREATE TABLE product_categories (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    name_de     TEXT NOT NULL,
    name_en     TEXT NOT NULL,
    emoji       TEXT,
    sort_order  INT NOT NULL DEFAULT 0,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, key)
);
CREATE TRIGGER trg_product_categories_updated BEFORE UPDATE ON product_categories FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE products (
    id              BIGSERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    category_id     BIGINT REFERENCES product_categories (id) ON DELETE SET NULL,
    sku             TEXT NOT NULL,
    name_de         TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    description_de  TEXT NOT NULL DEFAULT '',
    description_en  TEXT NOT NULL DEFAULT '',
    emoji           TEXT,
    price_cents     INT NOT NULL CHECK (price_cents >= 0),
    delivery_days   INT,
    image_url       TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, sku)
);
CREATE TRIGGER trg_products_updated BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_products_guild_active ON products (guild_id, active, sort_order);

CREATE TABLE bundles (
    id              BIGSERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    sku             TEXT NOT NULL,
    name_de         TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    description_de  TEXT NOT NULL DEFAULT '',
    description_en  TEXT NOT NULL DEFAULT '',
    emoji           TEXT,
    price_cents     INT NOT NULL CHECK (price_cents >= 0),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, sku)
);
CREATE TRIGGER trg_bundles_updated BEFORE UPDATE ON bundles FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE bundle_items (
    bundle_id   BIGINT NOT NULL REFERENCES bundles (id) ON DELETE CASCADE,
    product_id  BIGINT NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    quantity    INT NOT NULL DEFAULT 1 CHECK (quantity > 0),
    PRIMARY KEY (bundle_id, product_id)
);

CREATE TABLE coupons (
    id               BIGSERIAL PRIMARY KEY,
    guild_id         BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    code             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('percent', 'fixed')),
    value            INT NOT NULL CHECK (value > 0),
    min_order_cents  INT NOT NULL DEFAULT 0,
    max_uses         INT,
    uses             INT NOT NULL DEFAULT 0,
    expires_at       TIMESTAMPTZ,
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_by       BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (kind <> 'percent' OR value <= 100)
);
CREATE UNIQUE INDEX uq_coupons_code ON coupons (guild_id, upper(code));
CREATE TRIGGER trg_coupons_updated BEFORE UPDATE ON coupons FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- product bound coupons (no rows = valid for every product)
CREATE TABLE coupon_products (
    coupon_id   BIGINT NOT NULL REFERENCES coupons (id) ON DELETE CASCADE,
    product_id  BIGINT NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    PRIMARY KEY (coupon_id, product_id)
);

CREATE TABLE orders (
    id                  BIGSERIAL PRIMARY KEY,
    guild_id            BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    ticket_id           BIGINT REFERENCES tickets (id) ON DELETE SET NULL,
    customer_id         BIGINT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                            'draft', 'awaiting_payment', 'paid', 'queued', 'in_progress',
                            'pending_answer', 'completed', 'cancelled', 'refunded')),
    source              TEXT NOT NULL DEFAULT 'discord' CHECK (source IN ('discord', 'api')),
    locale              TEXT NOT NULL DEFAULT 'de',
    currency            TEXT NOT NULL DEFAULT 'EUR',
    delivery            TEXT NOT NULL DEFAULT 'standard' CHECK (delivery IN ('standard', 'express')),
    seasonal_key        TEXT,
    coupon_id           BIGINT REFERENCES coupons (id) ON DELETE SET NULL,
    subtotal_cents      INT NOT NULL DEFAULT 0,
    express_cents       INT NOT NULL DEFAULT 0,
    seasonal_cents      INT NOT NULL DEFAULT 0,
    discount_cents      INT NOT NULL DEFAULT 0,
    total_cents         INT NOT NULL DEFAULT 0,
    designer_id         BIGINT,
    notes               TEXT,
    due_at              TIMESTAMPTZ,
    wizard_channel_id   BIGINT,
    wizard_message_id   BIGINT,
    todo_message_id     BIGINT,
    submitted_at        TIMESTAMPTZ,
    paid_at             TIMESTAMPTZ,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_orders_updated BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_orders_guild_status ON orders (guild_id, status);
CREATE INDEX idx_orders_customer ON orders (customer_id, created_at DESC);
CREATE INDEX idx_orders_ticket ON orders (ticket_id);
CREATE INDEX idx_orders_designer ON orders (designer_id) WHERE designer_id IS NOT NULL;
CREATE INDEX idx_orders_paid ON orders (guild_id, paid_at) WHERE paid_at IS NOT NULL;

CREATE TABLE order_items (
    id                BIGSERIAL PRIMARY KEY,
    order_id          BIGINT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id        BIGINT REFERENCES products (id) ON DELETE SET NULL,
    bundle_id         BIGINT REFERENCES bundles (id) ON DELETE SET NULL,
    name              TEXT NOT NULL,
    quantity          INT NOT NULL DEFAULT 1 CHECK (quantity > 0),
    unit_price_cents  INT NOT NULL CHECK (unit_price_cents >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (product_id IS NOT NULL OR bundle_id IS NOT NULL OR name <> '')
);
CREATE INDEX idx_order_items_order ON order_items (order_id);
CREATE UNIQUE INDEX uq_order_items_product ON order_items (order_id, product_id) WHERE product_id IS NOT NULL;
CREATE UNIQUE INDEX uq_order_items_bundle ON order_items (order_id, bundle_id) WHERE bundle_id IS NOT NULL;

CREATE TABLE order_todos (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    position    INT NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    done        BOOLEAN NOT NULL DEFAULT FALSE,
    done_by     BIGINT,
    done_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_order_todos_order ON order_todos (order_id, position);

CREATE TABLE order_status_history (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    changed_by  BIGINT,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_order_status_history_order ON order_status_history (order_id, created_at);

CREATE TABLE showcases (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    order_id     BIGINT REFERENCES orders (id) ON DELETE SET NULL,
    customer_id  BIGINT,
    designer_id  BIGINT,
    title        TEXT NOT NULL,
    description  TEXT,
    channel_id   BIGINT,
    message_id   BIGINT,
    created_by   BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_showcases_guild ON showcases (guild_id, created_at DESC);
