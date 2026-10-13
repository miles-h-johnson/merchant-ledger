CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE lr_items (
    id SERIAL PRIMARY KEY,
    item_id TEXT UNIQUE,
    display_name TEXT,
    lr_category TEXT,
    count INTEGER,
    base_value NUMERIC,
    price_current NUMERIC,
    price_industrial_town NUMERIC,
    price_industrial_city NUMERIC,
    price_market_town NUMERIC,
    price_market_city NUMERIC,
    price_religious_town NUMERIC,
    price_temple_city NUMERIC,
    unit_price_current NUMERIC GENERATED ALWAYS AS (
        price_current / NULLIF(count::NUMERIC, 0)
    ) STORED
);

CREATE TABLE settlement_multipliers (
    id SERIAL PRIMARY KEY,
    settlement_type TEXT,
    category TEXT,
    multiplier NUMERIC,
    UNIQUE (settlement_type, category)
);

CREATE TABLE canonical_items (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    game_code TEXT,
    lr_item_id INTEGER REFERENCES lr_items(id) ON DELETE SET NULL,
    match_tier TEXT CHECK (match_tier IN ('exact', 'high', 'low', 'unmatched')),
    match_score DOUBLE PRECISION
);

CREATE TABLE item_aliases (
    id SERIAL PRIMARY KEY,
    alias TEXT,
    canonical_id TEXT REFERENCES canonical_items(id) ON DELETE CASCADE,
    source TEXT CHECK (source IN ('generated', 'pattern', 'manual')),
    UNIQUE (alias, canonical_id)
);

CREATE INDEX idx_item_aliases_alias_trgm
    ON item_aliases USING GIN (alias gin_trgm_ops);