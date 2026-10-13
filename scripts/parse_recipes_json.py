#!/usr/bin/env python3
"""
Parse Vintage Story recipe JSON directly into canonical recipe tables.

- Discovers base game and installed-mod recipe JSON files
- Parses JSON5 and detects the recipe type from the path segment after "recipes"
- Truncates recipes and recipe_ingredients, then inserts one row set per recipe
  through a handler per type (grid, smithing, barrel, cooking, alloy, clayforming, knapping)
"""

from __future__ import annotations

import os
import sys
import math
from itertools import product
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import json5
import psycopg2
from dotenv import load_dotenv


load_dotenv()


VS_INSTALL_DIR = Path(os.getenv("VINTAGE_STORY_INSTALL_DIR", "C:/Games/Vintagestory"))
# The game unpacks every installed mod here, which is how mod recipes are found.
VS_DATA_DIR = Path(os.getenv("VINTAGE_STORY_DATA_DIR", Path.home() / "AppData/Roaming/VintagestoryData"))

BASE_RECIPE_ROOTS = [
    VS_INSTALL_DIR / "assets" / "survival" / "recipes",
]
MOD_CACHE_ROOT = VS_DATA_DIR / "Cache" / "unpack"
KNOWN_RECIPE_TYPES = {
    "grid",
    "smithing",
    "barrel",
    "cooking",
    "alloy",
    "clayforming",
    "knapping",
}

UNKNOWN_WARNED_KEYS: set[tuple[str, str]] = set()
UNKNOWN_WARN_SUPPRESSED = 0
MAX_VARIANT_COMBINATIONS = 500


INSERT_RECIPE_SQL = """
INSERT INTO recipes (
    output_canonical_id,
    output_game_code,
    output_qty,
    recipe_type,
    source_mod
) VALUES (NULL, %s, %s, %s, %s)
RETURNING id
"""


INSERT_INGREDIENT_SQL = """
INSERT INTO recipe_ingredients (
    recipe_id,
    input_canonical_id,
    input_game_code,
    qty,
    ratio_min,
    ratio_max,
    variant_group_id,
    is_primary_variant
) VALUES (%s, NULL, %s, %s, NULL, NULL, NULL, FALSE)
"""


INSERT_INGREDIENT_ALLOY_SQL = """
INSERT INTO recipe_ingredients (
    recipe_id,
    input_canonical_id,
    input_game_code,
    qty,
    ratio_min,
    ratio_max,
    variant_group_id,
    is_primary_variant
) VALUES (%s, NULL, %s, %s, %s, %s, NULL, FALSE)
"""


def normalize_game_code(code: str | None) -> str:
    """
    Normalize a game code by stripping the explicit game-domain middle segment.

    Examples:
    - game:ingot-iron -> ingot-iron
    - item:game:lantern-up -> item:lantern-up
    - Item:candle -> item:candle
    """
    if not code:
        return ""

    text = str(code).strip()
    if not text:
        return ""

    parts = text.split(":")
    if len(parts) >= 3:
        prefix = parts[0].lower()
        domain = parts[1].lower()
        rest = ":".join(parts[2:])
        if domain == "game":
            return f"{prefix}:{rest}"
        return f"{prefix}:{domain}:{rest}"

    if len(parts) == 2:
        left = parts[0].lower()
        right = parts[1]
        if left == "game":
            return right
        return f"{left}:{right}"

    return text


def make_item_code(obj: dict[str, Any] | None) -> str:
    """
    Convert an ingredient/output JSON object into a "type:code" item string.
    """
    if not isinstance(obj, dict):
        return ""

    code = normalize_game_code(str(obj.get("code", "")).strip())
    if not code:
        return ""

    stack_type = str(obj.get("type", "item")).strip().lower() or "item"
    if stack_type not in {"item", "block"}:
        stack_type = "item"

    if ":" in code:
        return code
    return f"{stack_type}:{code}"


def parse_json5_file(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as f:
        data = json5.load(f)

    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def detect_recipe_type(path: Path) -> str:
    parts_lower = [p.lower() for p in path.parts]
    try:
        idx = parts_lower.index("recipes")
    except ValueError:
        return "unknown"

    if idx + 1 >= len(parts_lower):
        return "unknown"
    return parts_lower[idx + 1]


def detect_source_mod(path: Path) -> str:
    parts = list(path.parts)
    parts_lower = [p.lower() for p in parts]

    if "cache" in parts_lower and "unpack" in parts_lower:
        unpack_idx = parts_lower.index("unpack")
        if unpack_idx + 1 < len(parts):
            raw_name = parts[unpack_idx + 1]
            if ".zip_" in raw_name:
                return raw_name.split(".zip_", 1)[0]
            return raw_name

    return "Base Game"


def iter_recipe_files() -> Iterable[Path]:
    for base_recipe_root in BASE_RECIPE_ROOTS:
        if base_recipe_root.is_dir():
            yield from base_recipe_root.rglob("*.json")

    # Mods: scan only files under recipe directories, not every JSON in unpack cache.
    for unpack_root in MOD_CACHE_ROOT.iterdir() if MOD_CACHE_ROOT.is_dir() else []:
        if not unpack_root.is_dir():
            continue
        for recipes_dir in unpack_root.rglob("recipes"):
            if recipes_dir.is_dir():
                yield from recipes_dir.rglob("*.json")


def is_patch_like_recipe_obj(obj: dict[str, Any]) -> bool:
    # json patch payloads often contain op/path/file and no output/ingredient-like fields
    patch_keys = {"op", "path", "file", "value", "side", "dependson"}
    recipe_keys = {
        "output",
        "ingredient",
        "ingredients",
        "ingredientpattern",
        "cooksinto",
        "smeltsinto",
    }

    keys = {str(k).lower() for k in obj.keys()}
    return bool(keys & patch_keys) and not bool(keys & recipe_keys)


def parse_qty(raw: Any, default: Decimal = Decimal("1")) -> Decimal:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return default


def split_ingredient_pattern_rows(pattern: Any) -> list[str]:
    if isinstance(pattern, list):
        return [str(r) for r in pattern]
    if isinstance(pattern, str):
        return [r for r in pattern.replace("\t", ",").split(",")]
    return []


def count_grid_symbols(pattern: Any) -> Counter[str]:
    rows = split_ingredient_pattern_rows(pattern)
    symbol_counts: Counter[str] = Counter()
    for row in rows:
        for ch in row:
            if ch in {" ", "_"}:
                continue
            symbol_counts[ch] += 1
    return symbol_counts


def build_variant_rows(
    placeholders_to_variants: dict[str, list[str]],
    filepath: Path,
    recipe_type: str,
    max_combinations: int = MAX_VARIANT_COMBINATIONS,
) -> list[dict[str, str]] | None:
    if not placeholders_to_variants:
        return [{}]

    for variants in placeholders_to_variants.values():
        if not variants:
            return [{}]

    total_combinations = 1
    for variants in placeholders_to_variants.values():
        total_combinations *= len(variants)
        if total_combinations > max_combinations:
            print(
                f"[WARN] Skipping {recipe_type} recipe at {filepath}: "
                f"variant expansion would produce {total_combinations} combinations "
                f"(cap={max_combinations})."
            )
            return None

    placeholders = list(placeholders_to_variants.keys())
    variant_lists = [placeholders_to_variants[p] for p in placeholders]
    return [dict(zip(placeholders, combo)) for combo in product(*variant_lists)]


def variant_rows_for_grid_recipe(recipe: dict[str, Any], filepath: Path) -> list[dict[str, str]] | None:
    ingredients = recipe.get("ingredients") if isinstance(recipe.get("ingredients"), dict) else {}

    placeholders_to_variants: dict[str, list[str]] = {}
    for ingredient in ingredients.values():
        if not isinstance(ingredient, dict):
            continue

        allowed = ingredient.get("allowedVariants")
        if not isinstance(allowed, list) or not allowed:
            continue

        placeholder_name = str(ingredient.get("name", "")).strip()
        if not placeholder_name:
            continue

        if placeholder_name in placeholders_to_variants:
            if placeholders_to_variants[placeholder_name] != [str(v) for v in allowed if str(v).strip()]:
                # Conflicting variant lists for same placeholder: skip expansion for safety.
                return [{}]
        else:
            placeholders_to_variants[placeholder_name] = [str(v).strip() for v in allowed if str(v).strip()]

    return build_variant_rows(placeholders_to_variants, filepath, "grid")


def apply_variant_template(text: str, variant_map: dict[str, str]) -> str:
    out = text
    for key, value in variant_map.items():
        out = out.replace(f"{{{key}}}", value)
    return out


def handle_grid(cur, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    output_obj = recipe.get("output")
    if not isinstance(output_obj, dict):
        return 0

    ingredients_map = recipe.get("ingredients")
    if not isinstance(ingredients_map, dict):
        return 0

    symbol_counts = count_grid_symbols(recipe.get("ingredientPattern"))
    if not symbol_counts:
        return 0

    output_qty = parse_qty(output_obj.get("quantity") or output_obj.get("stacksize") or output_obj.get("stackSize"), Decimal("1"))
    variants = variant_rows_for_grid_recipe(recipe, filepath)
    if variants is None:
        return 0
    inserted = 0

    for variant_map in variants:
        out_copy = dict(output_obj)
        out_code = apply_variant_template(str(out_copy.get("code", "")), variant_map)
        out_copy["code"] = out_code

        output_game_code = make_item_code(out_copy)
        if not output_game_code:
            continue

        cur.execute(
            INSERT_RECIPE_SQL,
            (
                output_game_code,
                output_qty,
                "grid",
                source_mod,
            ),
        )
        recipe_id = cur.fetchone()[0]

        ingredient_rows: list[tuple[int, str, Decimal]] = []
        for symbol, count in symbol_counts.items():
            ingredient_obj = ingredients_map.get(symbol)
            if not isinstance(ingredient_obj, dict):
                continue

            if bool(ingredient_obj.get("isTool")):
                continue

            ing_copy = dict(ingredient_obj)
            raw_code = str(ing_copy.get("code", ""))
            placeholder_name = str(ing_copy.get("name", "")).strip()

            if placeholder_name and placeholder_name in variant_map:
                raw_code = raw_code.replace("*", variant_map[placeholder_name])
            raw_code = apply_variant_template(raw_code, variant_map)
            ing_copy["code"] = raw_code

            input_game_code = make_item_code(ing_copy)
            if not input_game_code:
                continue

            per_slot_qty = parse_qty(ing_copy.get("quantity") or ing_copy.get("stacksize") or ing_copy.get("stackSize"), Decimal("1"))
            qty = per_slot_qty * Decimal(count)
            ingredient_rows.append((recipe_id, input_game_code, qty))

        if ingredient_rows:
            cur.executemany(INSERT_INGREDIENT_SQL, ingredient_rows)

        inserted += 1

    return inserted


def handle_smithing(cur, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    output_obj = recipe.get("output")
    ingredient_obj = recipe.get("ingredient")

    if not isinstance(output_obj, dict) or not isinstance(ingredient_obj, dict):
        return 0

    pattern = recipe.get("pattern")
    filled_voxels = 0
    if isinstance(pattern, list):
        for layer in pattern:
            if isinstance(layer, list):
                for row in layer:
                    if isinstance(row, str):
                        filled_voxels += row.count("#")
            elif isinstance(layer, str):
                filled_voxels += layer.count("#")

    if filled_voxels <= 0:
        return 0

    # Smithing baseline: 42 filled voxels per ingot input.
    ingot_qty = Decimal(str(math.ceil(filled_voxels / 42)))

    allowed = ingredient_obj.get("allowedVariants")
    placeholder_name = str(ingredient_obj.get("name", "")).strip()
    variants: list[dict[str, str]] = [{}]
    if isinstance(allowed, list) and allowed and placeholder_name:
        clean_variants = [str(v).strip() for v in allowed if str(v).strip()]
        if clean_variants:
            variants = [{placeholder_name: v} for v in clean_variants]

    inserted = 0
    for variant_map in variants:
        out_copy = dict(output_obj)
        out_code = apply_variant_template(str(out_copy.get("code", "")), variant_map)
        out_copy["code"] = out_code
        output_game_code = make_item_code(out_copy)
        if not output_game_code:
            continue

        ing_copy = dict(ingredient_obj)
        raw_ing_code = str(ing_copy.get("code", ""))
        if placeholder_name and placeholder_name in variant_map:
            raw_ing_code = raw_ing_code.replace("*", variant_map[placeholder_name])
        raw_ing_code = apply_variant_template(raw_ing_code, variant_map)
        ing_copy["code"] = raw_ing_code
        input_game_code = make_item_code(ing_copy)
        if not input_game_code:
            continue

        cur.execute(
            INSERT_RECIPE_SQL,
            (
                output_game_code,
                Decimal("1"),
                "smithing",
                source_mod,
            ),
        )
        recipe_id = cur.fetchone()[0]

        cur.execute(
            INSERT_INGREDIENT_SQL,
            (
                recipe_id,
                input_game_code,
                ingot_qty,
            ),
        )

        inserted += 1

    return inserted


def handle_barrel(cur, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    output_obj = recipe.get("output")
    ingredients = recipe.get("ingredients")

    if not isinstance(output_obj, dict) or not isinstance(ingredients, list) or not ingredients:
        return 0

    # Barrel outputs are usually stack-based item outputs, but some are liquid-style.
    output_qty = parse_qty(
        output_obj.get("stackSize")
        or output_obj.get("stacksize")
        or output_obj.get("quantity")
        or output_obj.get("litres"),
        Decimal("1"),
    )

    # Variant expansion (barrel recipes commonly use one named wildcard placeholder).
    placeholders_to_variants: dict[str, list[str]] = {}
    for ingredient_obj in ingredients:
        if not isinstance(ingredient_obj, dict):
            continue

        allowed = ingredient_obj.get("allowedVariants")
        if not isinstance(allowed, list) or not allowed:
            continue

        placeholder_name = str(ingredient_obj.get("name", "")).strip()
        if not placeholder_name:
            continue

        clean_variants = [str(v).strip() for v in allowed if str(v).strip()]
        if not clean_variants:
            continue

        if placeholder_name in placeholders_to_variants:
            if placeholders_to_variants[placeholder_name] != clean_variants:
                return 0
        else:
            placeholders_to_variants[placeholder_name] = clean_variants

    variant_rows = build_variant_rows(placeholders_to_variants, filepath, "barrel")
    if variant_rows is None:
        return 0

    inserted = 0
    for variant_map in variant_rows:
        out_copy = dict(output_obj)
        out_copy["code"] = apply_variant_template(str(out_copy.get("code", "")), variant_map)
        output_game_code = make_item_code(out_copy)
        if not output_game_code:
            continue

        cur.execute(
            INSERT_RECIPE_SQL,
            (
                output_game_code,
                output_qty,
                "barrel",
                source_mod,
            ),
        )
        recipe_id = cur.fetchone()[0]

        ingredient_rows: list[tuple[int, str, Decimal]] = []
        for ingredient_obj in ingredients:
            if not isinstance(ingredient_obj, dict):
                continue

            ing_copy = dict(ingredient_obj)
            raw_code = str(ing_copy.get("code", ""))

            placeholder_name = str(ing_copy.get("name", "")).strip()
            if placeholder_name and placeholder_name in variant_map:
                raw_code = raw_code.replace("*", variant_map[placeholder_name])
            raw_code = apply_variant_template(raw_code, variant_map)
            ing_copy["code"] = raw_code

            input_game_code = make_item_code(ing_copy)
            if not input_game_code:
                continue

            ing_type = str(ing_copy.get("type", "")).strip().lower()
            is_liquid = (
                ing_type == "liquid"
                or ing_copy.get("consumeLitres") is not None
                or ing_copy.get("consumelitres") is not None
                or ing_copy.get("litres") is not None
            )

            if is_liquid:
                qty = parse_qty(
                    ing_copy.get("consumeLitres")
                    if ing_copy.get("consumeLitres") is not None
                    else ing_copy.get("consumelitres")
                    if ing_copy.get("consumelitres") is not None
                    else ing_copy.get("litres"),
                    Decimal("0"),
                )
            else:
                qty = parse_qty(ing_copy.get("quantity"), Decimal("0"))

            if qty <= 0:
                continue

            ingredient_rows.append((recipe_id, input_game_code, qty))

        if not ingredient_rows:
            cur.execute("DELETE FROM recipes WHERE id = %s", (recipe_id,))
            continue

        cur.executemany(INSERT_INGREDIENT_SQL, ingredient_rows)
        inserted += 1

    return inserted


def handle_cooking(cur, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    cooks_into = recipe.get("cooksInto")
    ingredients = recipe.get("ingredients")

    if not isinstance(ingredients, list) or not ingredients:
        return 0

    output_obj: dict[str, Any]
    if isinstance(cooks_into, dict):
        output_obj = cooks_into
    else:
        # Some meal recipes omit cooksInto and use meal code identity.
        recipe_code = str(recipe.get("code", "")).strip()
        if not recipe_code:
            return 0
        output_obj = {"type": "item", "code": f"meal-{recipe_code}", "quantity": 1}

    output_game_code = make_item_code(output_obj)
    if not output_game_code:
        return 0

    output_qty = parse_qty(
        output_obj.get("quantity")
        or output_obj.get("stackSize")
        or output_obj.get("stacksize"),
        Decimal("1"),
    )

    cur.execute(
        INSERT_RECIPE_SQL,
        (
            output_game_code,
            output_qty,
            "cooking",
            source_mod,
        ),
    )
    recipe_id = cur.fetchone()[0]

    ingredient_rows: list[tuple[int, str, Decimal, str | None, bool]] = []
    required_slots = 0
    recipe_code = str(recipe.get("code", "")).strip() or output_game_code

    for slot_idx, slot in enumerate(ingredients):
        if not isinstance(slot, dict):
            continue

        min_qty = parse_qty(slot.get("minQuantity"), Decimal("0"))
        if min_qty <= 0:
            # Optional slot for minimum-viable meal baseline.
            continue

        valid_stacks = slot.get("validStacks")
        if not isinstance(valid_stacks, list) or not valid_stacks:
            continue

        required_slots += 1
        variant_group_id = f"cooking:{recipe_code}:{slot_idx}"
        primary_written = False

        for stack in valid_stacks:
            if not isinstance(stack, dict):
                continue
            input_game_code = make_item_code(stack)
            if not input_game_code:
                continue

            is_primary = not primary_written
            ingredient_rows.append((recipe_id, input_game_code, min_qty, variant_group_id, is_primary))
            primary_written = True

    if required_slots == 0 or not ingredient_rows:
        cur.execute("DELETE FROM recipes WHERE id = %s", (recipe_id,))
        return 0

    cur.executemany(
        """
        INSERT INTO recipe_ingredients (
            recipe_id,
            input_canonical_id,
            input_game_code,
            qty,
            ratio_min,
            ratio_max,
            variant_group_id,
            is_primary_variant
        ) VALUES (%s, NULL, %s, %s, NULL, NULL, %s, %s)
        """,
        ingredient_rows,
    )

    return 1


def handle_alloy(cur, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    output_obj = recipe.get("output")
    ingredients = recipe.get("ingredients")

    if not isinstance(output_obj, dict) or not isinstance(ingredients, list) or not ingredients:
        return 0

    output_game_code = make_item_code(output_obj)
    if not output_game_code:
        return 0

    output_qty = parse_qty(
        output_obj.get("quantity")
        or output_obj.get("stackSize")
        or output_obj.get("stacksize")
        or output_obj.get("litres"),
        Decimal("1"),
    )

    cur.execute(
        INSERT_RECIPE_SQL,
        (
            output_game_code,
            output_qty,
            "alloy",
            source_mod,
        ),
    )
    recipe_id = cur.fetchone()[0]

    inserted_ingredients = 0
    for ingredient_obj in ingredients:
        if not isinstance(ingredient_obj, dict):
            continue

        input_game_code = make_item_code(ingredient_obj)
        if not input_game_code:
            continue

        ratio_min = parse_qty(ingredient_obj.get("minratio"), Decimal("0"))
        ratio_max = parse_qty(ingredient_obj.get("maxratio"), Decimal("0"))

        if ratio_min <= 0 and ratio_max <= 0:
            continue

        if ratio_min <= 0:
            ratio_min = ratio_max
        if ratio_max <= 0:
            ratio_max = ratio_min

        qty = (ratio_min + ratio_max) / Decimal("2")

        cur.execute(
            INSERT_INGREDIENT_ALLOY_SQL,
            (
                recipe_id,
                input_game_code,
                qty,
                ratio_min,
                ratio_max,
            ),
        )
        inserted_ingredients += 1

    if inserted_ingredients == 0:
        cur.execute("DELETE FROM recipes WHERE id = %s", (recipe_id,))
        return 0

    return 1


def handle_clayforming(cur, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    output_obj = recipe.get("output")
    ingredient_obj = recipe.get("ingredient")

    if not isinstance(output_obj, dict) or not isinstance(ingredient_obj, dict):
        return 0

    allowed = ingredient_obj.get("allowedVariants")
    placeholder_name = str(ingredient_obj.get("name", "")).strip()
    variants: list[dict[str, str]] = [{}]
    if isinstance(allowed, list) and allowed and placeholder_name:
        clean_variants = [str(v).strip() for v in allowed if str(v).strip()]
        if clean_variants:
            variants = [{placeholder_name: v} for v in clean_variants]

    inserted = 0
    for variant_map in variants:
        out_copy = dict(output_obj)
        out_code = apply_variant_template(str(out_copy.get("code", "")), variant_map)
        out_copy["code"] = out_code
        output_game_code = make_item_code(out_copy)
        if not output_game_code:
            continue

        ing_copy = dict(ingredient_obj)
        raw_ing_code = str(ing_copy.get("code", ""))
        if placeholder_name and placeholder_name in variant_map:
            raw_ing_code = raw_ing_code.replace("*", variant_map[placeholder_name])
        raw_ing_code = apply_variant_template(raw_ing_code, variant_map)
        ing_copy["code"] = raw_ing_code
        input_game_code = make_item_code(ing_copy)
        if not input_game_code:
            continue

        # Clayforming consumes one clay ball regardless of pattern occupancy.
        input_qty = Decimal("1")

        cur.execute(
            INSERT_RECIPE_SQL,
            (
                output_game_code,
                Decimal("1"),
                "clayforming",
                source_mod,
            ),
        )
        recipe_id = cur.fetchone()[0]

        cur.execute(
            INSERT_INGREDIENT_SQL,
            (
                recipe_id,
                input_game_code,
                input_qty,
            ),
        )

        inserted += 1

    return inserted


def handle_knapping(cur, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    output_obj = recipe.get("output")
    ingredient_obj = recipe.get("ingredient")

    if not isinstance(output_obj, dict) or not isinstance(ingredient_obj, dict):
        return 0

    allowed = ingredient_obj.get("allowedVariants")
    placeholder_name = str(ingredient_obj.get("name", "")).strip()
    variants: list[dict[str, str]] = [{}]
    if isinstance(allowed, list) and allowed and placeholder_name:
        clean_variants = [str(v).strip() for v in allowed if str(v).strip()]
        if clean_variants:
            variants = [{placeholder_name: v} for v in clean_variants]

    inserted = 0
    for variant_map in variants:
        out_copy = dict(output_obj)
        out_code = apply_variant_template(str(out_copy.get("code", "")), variant_map)
        out_copy["code"] = out_code
        output_game_code = make_item_code(out_copy)
        if not output_game_code:
            continue

        ing_copy = dict(ingredient_obj)
        raw_ing_code = str(ing_copy.get("code", ""))
        if placeholder_name and placeholder_name in variant_map:
            raw_ing_code = raw_ing_code.replace("*", variant_map[placeholder_name])
        raw_ing_code = apply_variant_template(raw_ing_code, variant_map)
        ing_copy["code"] = raw_ing_code
        input_game_code = make_item_code(ing_copy)
        if not input_game_code:
            continue

        # Knapping consumes one source stone/flint/obsidian regardless of pattern occupancy.
        input_qty = Decimal("1")

        cur.execute(
            INSERT_RECIPE_SQL,
            (
                output_game_code,
                Decimal("1"),
                "knapping",
                source_mod,
            ),
        )
        recipe_id = cur.fetchone()[0]

        cur.execute(
            INSERT_INGREDIENT_SQL,
            (
                recipe_id,
                input_game_code,
                input_qty,
            ),
        )

        inserted += 1

    return inserted


def handle_unknown(
    cur,
    recipe: dict[str, Any],
    source_mod: str,
    filepath: Path,
    recipe_type: str,
) -> int:
    global UNKNOWN_WARN_SUPPRESSED

    output_obj = recipe.get("output")
    output_game_code = ""
    output_qty = Decimal("1")

    if isinstance(output_obj, dict):
        output_game_code = make_item_code(output_obj)
        output_qty = parse_qty(
            output_obj.get("quantity")
            or output_obj.get("stackSize")
            or output_obj.get("stacksize")
            or output_obj.get("litres"),
            Decimal("1"),
        )

    ingredient_codes_with_qty: list[tuple[str, Decimal]] = []

    ingredient_obj = recipe.get("ingredient")
    if isinstance(ingredient_obj, dict):
        input_game_code = make_item_code(ingredient_obj)
        if input_game_code:
            qty = parse_qty(
                ingredient_obj.get("quantity")
                or ingredient_obj.get("stackSize")
                or ingredient_obj.get("stacksize")
                or ingredient_obj.get("litres")
                or ingredient_obj.get("consumeLitres")
                or ingredient_obj.get("consumelitres"),
                Decimal("1"),
            )
            if qty > 0:
                ingredient_codes_with_qty.append((input_game_code, qty))

    ingredients_obj = recipe.get("ingredients")
    if isinstance(ingredients_obj, list):
        for ing in ingredients_obj:
            if not isinstance(ing, dict):
                continue
            input_game_code = make_item_code(ing)
            if not input_game_code:
                continue
            qty = parse_qty(
                ing.get("quantity")
                or ing.get("stackSize")
                or ing.get("stacksize")
                or ing.get("litres")
                or ing.get("consumeLitres")
                or ing.get("consumelitres"),
                Decimal("1"),
            )
            if qty > 0:
                ingredient_codes_with_qty.append((input_game_code, qty))
    elif isinstance(ingredients_obj, dict):
        # Some mod recipes may use symbol-keyed dicts outside grid semantics.
        for ing in ingredients_obj.values():
            if not isinstance(ing, dict):
                continue
            input_game_code = make_item_code(ing)
            if not input_game_code:
                continue
            qty = parse_qty(
                ing.get("quantity")
                or ing.get("stackSize")
                or ing.get("stacksize")
                or ing.get("litres")
                or ing.get("consumeLitres")
                or ing.get("consumelitres"),
                Decimal("1"),
            )
            if qty > 0:
                ingredient_codes_with_qty.append((input_game_code, qty))

    inserted = 0
    if output_game_code:
        cur.execute(
            INSERT_RECIPE_SQL,
            (
                output_game_code,
                output_qty,
                recipe_type,
                source_mod,
            ),
        )
        recipe_id = cur.fetchone()[0]

        ingredient_rows = [(recipe_id, code, qty) for code, qty in ingredient_codes_with_qty]
        if ingredient_rows:
            cur.executemany(INSERT_INGREDIENT_SQL, ingredient_rows)

        inserted = 1

    warn_key = (recipe_type, str(filepath))
    if warn_key not in UNKNOWN_WARNED_KEYS:
        UNKNOWN_WARNED_KEYS.add(warn_key)
        print(
            f"[WARN] Unknown recipe type '{recipe_type}' at {filepath}. "
            f"Best-effort insert: output={'yes' if output_game_code else 'no'}, "
            f"ingredients={len(ingredient_codes_with_qty)}"
        )
    else:
        UNKNOWN_WARN_SUPPRESSED += 1

    return inserted


def dispatch_recipe(cur, recipe_type: str, recipe: dict[str, Any], source_mod: str, filepath: Path) -> int:
    if recipe_type == "grid":
        return handle_grid(cur, recipe, source_mod, filepath)
    if recipe_type == "smithing":
        return handle_smithing(cur, recipe, source_mod, filepath)
    if recipe_type == "barrel":
        return handle_barrel(cur, recipe, source_mod, filepath)
    if recipe_type == "cooking":
        return handle_cooking(cur, recipe, source_mod, filepath)
    if recipe_type == "alloy":
        return handle_alloy(cur, recipe, source_mod, filepath)
    if recipe_type == "clayforming":
        return handle_clayforming(cur, recipe, source_mod, filepath)
    if recipe_type == "knapping":
        return handle_knapping(cur, recipe, source_mod, filepath)
    return handle_unknown(cur, recipe, source_mod, filepath, recipe_type)


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    files_seen = 0
    files_skipped = 0
    recipes_skipped = 0
    recipes_seen_by_type: Counter[str] = Counter()
    inserted_by_type: Counter[str] = Counter()

    print(f"[INFO] Base recipe roots: {BASE_RECIPE_ROOTS}")
    print(f"[INFO] Mod cache root: {MOD_CACHE_ROOT}")

    try:
        # psycopg2 context manager commits on clean exit, rolls back on exception.
        # All parse + insert work is inside this single transaction.
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE recipe_ingredients, recipes RESTART IDENTITY CASCADE")

                for filepath in iter_recipe_files():
                    files_seen += 1

                    recipe_type = detect_recipe_type(filepath)
                    source_mod = detect_source_mod(filepath)

                    try:
                        recipes = parse_json5_file(filepath)
                    except Exception as exc:
                        print(f"[SKIP] Malformed recipe file {filepath} ({recipe_type}): {exc}")
                        files_skipped += 1
                        continue

                    for recipe in recipes:
                        if is_patch_like_recipe_obj(recipe):
                            continue

                        recipes_seen_by_type[recipe_type] += 1
                        try:
                            inserted = dispatch_recipe(cur, recipe_type, recipe, source_mod, filepath)
                        except Exception as exc:
                            print(f"[SKIP] Failed to insert recipe in {filepath} ({recipe_type}): {exc}")
                            recipes_skipped += 1
                            continue
                        if inserted:
                            inserted_by_type[recipe_type] += int(inserted)

    except Exception as exc:
        print(f"[ERROR] Failed during parse run: {exc}", file=sys.stderr)
        return 1

    print("=== parse_recipes_json.py summary ===")
    print(f"Files walked: {files_seen}")
    print("Recipe counts by type:")
    for recipe_type in sorted(recipes_seen_by_type.keys()):
        print(f"- {recipe_type}: {recipes_seen_by_type[recipe_type]}")
    print(f"Total recipes discovered: {sum(recipes_seen_by_type.values())}")
    print(f"Total recipes inserted: {sum(inserted_by_type.values())}")
    if files_skipped:
        print(f"Files skipped (malformed): {files_skipped}")
    if recipes_skipped:
        print(f"Recipes skipped (insert error): {recipes_skipped}")
    if UNKNOWN_WARN_SUPPRESSED:
        print(f"Suppressed duplicate unknown-type warnings: {UNKNOWN_WARN_SUPPRESSED}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
