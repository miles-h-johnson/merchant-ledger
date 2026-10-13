CREATE TABLE fta_items (
    id SERIAL PRIMARY KEY,
    item_id TEXT UNIQUE,
    display_name TEXT,
    fta_category TEXT,
    fta_sub_category TEXT,
    qty_unit TEXT,
    qty_numeric NUMERIC,
    unit_is_liters BOOLEAN DEFAULT FALSE,
    copper_sovereign_value NUMERIC,
    unit_price NUMERIC GENERATED ALWAYS AS (
        copper_sovereign_value / NULLIF(qty_numeric, 0)
    ) STORED,
    notes TEXT
);

ALTER TABLE canonical_items
ADD COLUMN fta_item_id INTEGER REFERENCES fta_items(id) ON DELETE SET NULL;