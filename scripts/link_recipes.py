#!/usr/bin/env python3
"""
Link recipe game codes to canonical item IDs and mark primary ingredient variants.

Behavior:
  - Reads DATABASE_URL from environment.
  - Uses exact game-code matching against canonical_items.game_code.
  - Updates:
      1) recipes.output_canonical_id where currently NULL
      2) recipe_ingredients.input_canonical_id where currently NULL
      3) recipe_ingredients.is_primary_variant for each variant_group_id

Logs:
  - recipes linked
  - recipes unlinked
  - ingredients linked
  - ingredients unlinked
  - variant groups processed
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Pattern, Tuple

import psycopg2
from dotenv import load_dotenv


load_dotenv()


PLACEHOLDER_RE = re.compile(r"\{[^}]+\}")


def load_template_patterns(cur) -> List[Tuple[str, Pattern[str]]]:
    cur.execute(
        """
        SELECT id, game_code
        FROM canonical_items
        WHERE game_code IS NOT NULL
          AND game_code LIKE '%{%'
          AND game_code LIKE '%}%'
        ORDER BY id
        """
    )

    patterns: List[Tuple[str, Pattern[str]]] = []
    for canonical_id, template_code in cur.fetchall():
        templated = PLACEHOLDER_RE.sub("__PLACEHOLDER__", template_code)
        escaped = re.escape(templated)
        # Placeholder segments should match a single token, not an arbitrary
        # hyphenated phrase. This prevents broad templates like item:{metal}
        # from incorrectly matching outputs such as item:awlthorn-gold.
        regex = "^" + escaped.replace("__PLACEHOLDER__", r"[^:\-|]+") + "$"
        patterns.append((canonical_id, re.compile(regex)))
    return patterns


def link_unlinked_by_template(
    cur,
    *,
    table: str,
    row_id_col: str,
    code_col: str,
    canonical_col: str,
    patterns: List[Tuple[str, Pattern[str]]],
) -> int:
    if not patterns:
        return 0

    cur.execute(
        f"""
        SELECT {row_id_col}, {code_col}
        FROM {table}
        WHERE {canonical_col} IS NULL
          AND {code_col} IS NOT NULL
        """
    )

    updates: List[Tuple[str, int]] = []
    for row_id, game_code in cur.fetchall():
        for canonical_id, pattern in patterns:
            if pattern.match(game_code):
                updates.append((canonical_id, row_id))
                break

    if not updates:
        return 0

    cur.executemany(
        f"""
        UPDATE {table}
        SET {canonical_col} = %s
        WHERE {row_id_col} = %s
          AND {canonical_col} IS NULL
        """,
        updates,
    )
    return len(updates)


def count_variant_groups(cur) -> int:
    cur.execute(
        """
        SELECT COUNT(DISTINCT variant_group_id)
        FROM recipe_ingredients
        WHERE variant_group_id IS NOT NULL
        """
    )
    value = cur.fetchone()
    return int(value[0] or 0)


def link_recipe_outputs(cur) -> tuple[int, int]:
    template_patterns = load_template_patterns(cur)

    # Pass 1: exact full-code match
    cur.execute(
        """
        UPDATE recipes r
        SET output_canonical_id = ci.id
        FROM canonical_items ci
        WHERE r.output_canonical_id IS NULL
          AND r.output_game_code = ci.game_code
        """
    )
    linked_exact = cur.rowcount

    # Pass 2: fallback tail match after stripping namespaces
    cur.execute(
        """
        UPDATE recipes r
        SET output_canonical_id = ci.id
        FROM canonical_items ci
        WHERE r.output_canonical_id IS NULL
          AND r.output_game_code IS NOT NULL
          AND ci.game_code IS NOT NULL
          AND SUBSTRING(r.output_game_code FROM '[^:]+$') = SUBSTRING(ci.game_code FROM '[^:]+$')
        """
    )
    linked_tail = cur.rowcount

    # Pass 3: template match (e.g. item:metalplate-{metal} -> ^item:metalplate-[^:]+$)
    linked_template = link_unlinked_by_template(
        cur,
        table="recipes",
        row_id_col="id",
        code_col="output_game_code",
        canonical_col="output_canonical_id",
        patterns=template_patterns,
    )
    linked = linked_exact + linked_tail + linked_template

    cur.execute(
        """
        SELECT COUNT(*)
        FROM recipes
        WHERE output_canonical_id IS NULL
        """
    )
    unlinked = int(cur.fetchone()[0])
    return linked, unlinked


def link_recipe_ingredients(cur) -> tuple[int, int]:
    template_patterns = load_template_patterns(cur)

    # Pass 1: exact full-code match
    cur.execute(
        """
        UPDATE recipe_ingredients ri
        SET input_canonical_id = ci.id
        FROM canonical_items ci
        WHERE ri.input_canonical_id IS NULL
          AND ri.input_game_code = ci.game_code
        """
    )
    linked_exact = cur.rowcount

    # Pass 1b: attribute-specific code fallback (e.g. block:lantern-up|material=brass)
    # to base code canonical match when no canonical exists for the full attributed key.
    cur.execute(
        """
        UPDATE recipe_ingredients ri
        SET input_canonical_id = ci.id
        FROM canonical_items ci
        WHERE ri.input_canonical_id IS NULL
          AND ri.input_game_code IS NOT NULL
          AND POSITION('|' IN ri.input_game_code) > 0
          AND split_part(ri.input_game_code, '|', 1) = ci.game_code
        """
    )
    linked_attr_base = cur.rowcount

    # Pass 2: fallback tail match after stripping namespaces
    cur.execute(
        """
        UPDATE recipe_ingredients ri
        SET input_canonical_id = ci.id
        FROM canonical_items ci
        WHERE ri.input_canonical_id IS NULL
          AND ri.input_game_code IS NOT NULL
          AND ci.game_code IS NOT NULL
          AND SUBSTRING(ri.input_game_code FROM '[^:]+$') = SUBSTRING(ci.game_code FROM '[^:]+$')
        """
    )
    linked_tail = cur.rowcount

    # Pass 3: template match (e.g. item:metalplate-{metal} -> ^item:metalplate-[^:]+$)
    linked_template = link_unlinked_by_template(
        cur,
        table="recipe_ingredients",
        row_id_col="id",
        code_col="input_game_code",
        canonical_col="input_canonical_id",
        patterns=template_patterns,
    )
    linked = linked_exact + linked_attr_base + linked_tail + linked_template

    cur.execute(
        """
        SELECT COUNT(*)
        FROM recipe_ingredients
        WHERE input_canonical_id IS NULL
        """
    )
    unlinked = int(cur.fetchone()[0])
    return linked, unlinked


def mark_primary_variants(cur) -> None:
    # Clear all primary flags for variant groups, then mark first-inserted row
    # (lowest id) as the representative variant per group.
    #
    # This preserves parser ordering semantics: the first expanded wildcard
    # variant is the costing representative; remaining rows are substitutes.
    cur.execute(
        """
        UPDATE recipe_ingredients
        SET is_primary_variant = FALSE
        WHERE variant_group_id IS NOT NULL
        """
    )

    cur.execute(
        """
        WITH ranked AS (
            SELECT
                ri.id,
                ROW_NUMBER() OVER (
                    PARTITION BY ri.variant_group_id
                    ORDER BY ri.id ASC
                ) AS rn
            FROM recipe_ingredients ri
            WHERE ri.variant_group_id IS NOT NULL
        )
        UPDATE recipe_ingredients ri
        SET is_primary_variant = TRUE
        FROM ranked r
        WHERE ri.id = r.id
          AND r.rn = 1
        """
    )


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                recipes_linked, recipes_unlinked = link_recipe_outputs(cur)
                ingredients_linked, ingredients_unlinked = link_recipe_ingredients(cur)

                variant_groups_processed = count_variant_groups(cur)
                mark_primary_variants(cur)

                print(f"Recipes linked: {recipes_linked}")
                print(f"Recipes unlinked: {recipes_unlinked}")
                print(f"Ingredients linked: {ingredients_linked}")
                print(f"Ingredients unlinked: {ingredients_unlinked}")
                print(f"Variant groups processed: {variant_groups_processed}")
                print("[OK] Done.")

    except Exception as exc:
        print(f"[ERROR] Failed to link recipes: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
