ALTER TABLE recipe_ingredients
    DROP CONSTRAINT IF EXISTS recipe_ingredients_input_canonical_id_fkey,
    ADD CONSTRAINT recipe_ingredients_input_canonical_id_fkey
        FOREIGN KEY (input_canonical_id) REFERENCES canonical_items(id) ON DELETE SET NULL;

ALTER TABLE recipes
    DROP CONSTRAINT IF EXISTS recipes_output_canonical_id_fkey,
    ADD CONSTRAINT recipes_output_canonical_id_fkey
        FOREIGN KEY (output_canonical_id) REFERENCES canonical_items(id) ON DELETE SET NULL;