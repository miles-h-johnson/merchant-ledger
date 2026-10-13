CREATE TABLE recipes (
    id SERIAL PRIMARY KEY,
    output_canonical_id TEXT REFERENCES canonical_items(id),
    output_game_code TEXT,
    output_qty NUMERIC DEFAULT 1,
    recipe_type TEXT,
    source_mod TEXT
);

CREATE TABLE recipe_ingredients (
    id SERIAL PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    input_canonical_id TEXT REFERENCES canonical_items(id),
    input_game_code TEXT,
    qty NUMERIC,
    ratio_min NUMERIC,
    ratio_max NUMERIC,
    variant_group_id TEXT,
    is_primary_variant BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_recipes_output_canonical_id
    ON recipes (output_canonical_id);

CREATE INDEX idx_recipes_output_game_code
    ON recipes (output_game_code);

CREATE INDEX idx_recipe_ingredients_recipe_id
    ON recipe_ingredients (recipe_id);

CREATE INDEX idx_recipe_ingredients_input_canonical_id
    ON recipe_ingredients (input_canonical_id);

CREATE INDEX idx_recipe_ingredients_variant_group_id
    ON recipe_ingredients (variant_group_id);