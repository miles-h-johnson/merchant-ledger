CREATE TABLE recipe_staging (
    id SERIAL PRIMARY KEY,
    source_mod TEXT,
    recipe_type TEXT,
    output_game_code TEXT,
    output_qty_raw TEXT,
    ingredients_raw TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);