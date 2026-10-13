"""Core lookup helpers for canonical item and Lost Realm price resolution."""

import copy
import re

from dotenv import load_dotenv
from psycopg2 import sql


load_dotenv()


SETTLEMENT_COLUMNS = {
    "current": "price_current",
    "industrial_town": "price_industrial_town",
    "industrial_city": "price_industrial_city",
    "market_town": "price_market_town",
    "market_city": "price_market_city",
    "religious_town": "price_religious_town",
    "temple_city": "price_temple_city",
}

QUALITY_PREFIXES = ("uncommon", "rare", "epic", "legendary")

# Non-consumable tool policy (applied at resolver-time so raw recipe truth remains intact).
# Anchored prefix matching is intentionally conservative to avoid false positives.
NON_CONSUMABLE_TOOL_ROOTS = (
    "sewingkit",
    "sewing",
    "pickaxe",
    "shovel",
    "scythe",
    "hammer",
    "chisel",
    "knife",
    "axe",
    "awl",
    "saw",
    "sew",
)

VARIANT_INFER_FAMILY_WHITELIST = {
    "ingot",
    "nugget",
    "lantern-up",
    "pickaxehead",
    "axehead",
    "shovelhead",
    "hoehead",
    "swordhead",
    "knifeblade",
    "arrowhead",
    "hammerhead",
    "chiselhead",
    "sawblade",
    "helvehammerhead",
    "scythepart",
    "javelin",
    "metalplate",
    "armor-body-chain",
    "armor-body-plate",
    "armor-head-chain",
    "armor-head-plate",
    "armor-legs-chain",
    "armor-legs-plate",
}


def _infer_variant_from_game_code(game_code):
    text = (game_code or "").strip().lower()
    if not text.startswith("item:"):
        return None, None

    tail = text.split(":", 1)[1]
    if "-" not in tail:
        return None, None

    family, material = tail.rsplit("-", 1)
    if family not in VARIANT_INFER_FAMILY_WHITELIST:
        return None, None

    if not material:
        return None, None

    return family, material


def _material_token_matches(text, material):
    normalized_text = (text or "").strip().lower()
    normalized_material = (material or "").strip().lower()
    if not normalized_text or not normalized_material:
        return False

    pattern = rf"(^|[_\-\s]){re.escape(normalized_material)}($|[_\-\s])"
    return re.search(pattern, normalized_text) is not None


def _ingredient_matches_material(input_canonical_id, ingredient_game_code, ingredient_display_name, material):
    return (
        _material_token_matches(input_canonical_id, material)
        or _material_token_matches(ingredient_game_code, material)
        or _material_token_matches(ingredient_display_name, material)
    )


def _normalize_tool_match_text(value):
    text = (value or "").strip().lower()
    if not text:
        return ""

    if ":" in text:
        # canonical ids rarely include ":", game codes commonly do (item:/block:)
        return text.split(":")[-1]

    return text


def _matches_non_consumable_tool_root(value):
    normalized = _normalize_tool_match_text(value)
    if not normalized:
        return False

    for root in NON_CONSUMABLE_TOOL_ROOTS:
        pattern = rf"^{re.escape(root)}($|[_\-\s:])"
        if re.search(pattern, normalized):
            return True

    return False


def _is_non_consumable_tool_ingredient(
    input_canonical_id,
    input_game_code,
    ingredient_game_code,
    ingredient_display_name,
):
    """Return True when ingredient should be treated as a non-consumable tool."""
    return any(
        _matches_non_consumable_tool_root(candidate)
        for candidate in (
            input_canonical_id,
            input_game_code,
            ingredient_game_code,
            ingredient_display_name,
        )
    )


def _filter_recipe_results_for_material(recipe_results, material):
    normalized_material = (material or "").strip().lower()
    if not normalized_material:
        return recipe_results

    max_match_score = max((r.get("_material_match_score", 0) or 0) for r in recipe_results) if recipe_results else 0
    if max_match_score <= 0:
        return recipe_results

    return [r for r in recipe_results if (r.get("_material_match_score", 0) or 0) == max_match_score]


def _resolve_wildcard_input_canonical_id(
    input_game_code,
    settlement_type,
    db_conn,
    _visited,
    _memo,
    material=None,
    include_all_alternatives=False,
):
    """Resolve wildcard recipe inputs (currently clay-*) to cheapest priced variant."""
    normalized_input = (input_game_code or "").strip().lower()
    if "clay-*" not in normalized_input:
        return None

    wildcard_prefix = normalized_input.replace("*", "%")

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM canonical_items
            WHERE lower(game_code) LIKE %s
            ORDER BY id
            """,
            (wildcard_prefix,),
        )
        candidate_ids = [row[0] for row in cur.fetchall() if row and row[0]]

    if not candidate_ids:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM canonical_items
                WHERE lower(id) LIKE 'clay-%'
                ORDER BY id
                """
            )
            candidate_ids = [row[0] for row in cur.fetchall() if row and row[0]]

    best_canonical_id = None
    best_unit_cost = None
    for candidate_id in candidate_ids:
        candidate_result = calculate_cost(
            candidate_id,
            settlement_type,
            db_conn,
            quantity=1,
            _visited=_visited.copy(),
            _memo=_memo,
            material=material,
            include_recipe_alternative=False,
            include_all_alternatives=include_all_alternatives,
        )

        candidate_unit_cost = candidate_result.get("unit_cost") if isinstance(candidate_result, dict) else None
        if candidate_unit_cost is None:
            continue

        candidate_unit_cost = float(candidate_unit_cost)
        if (
            best_unit_cost is None
            or candidate_unit_cost < best_unit_cost
            or (candidate_unit_cost == best_unit_cost and candidate_id < (best_canonical_id or candidate_id))
        ):
            best_unit_cost = candidate_unit_cost
            best_canonical_id = candidate_id

    return best_canonical_id


def _build_recipe_result_for_canonical(
    canonical_id,
    settlement_type,
    db_conn,
    qty_requested,
    _visited,
    _memo,
    material=None,
    include_all_alternatives=False,
):
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT display_name, match_tier
            FROM canonical_items
            WHERE id = %s
            """,
            (canonical_id,),
        )
        item_row = cur.fetchone()

    display_name = item_row[0] if item_row else canonical_id
    match_tier = item_row[1] if item_row else None

    return _build_recipe_result(
        canonical_id=canonical_id,
        display_name=display_name,
        match_tier=match_tier,
        settlement_type=settlement_type,
        db_conn=db_conn,
        qty_requested=qty_requested,
        _visited=_visited,
        _memo=_memo,
        material=material,
        include_all_alternatives=include_all_alternatives,
    )


def _find_recipe_alternative_for_display_name(
    canonical_id,
    display_name,
    settlement_type,
    db_conn,
    qty_requested,
    _visited,
    _memo,
    material=None,
    include_all_alternatives=False,
):
    """When LR-linked canonical has no recipe path, try same-display-name siblings."""
    if not display_name:
        return None

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM canonical_items
            WHERE display_name = %s
              AND id <> %s
            ORDER BY id
            """,
            (display_name, canonical_id),
        )
        sibling_ids = [row[0] for row in cur.fetchall()]

    for sibling_id in sibling_ids:
        sibling_recipe = _build_recipe_result_for_canonical(
            canonical_id=sibling_id,
            settlement_type=settlement_type,
            db_conn=db_conn,
            qty_requested=qty_requested,
            _visited=_visited,
            _memo=_memo,
            material=material,
            include_all_alternatives=include_all_alternatives,
        )
        if sibling_recipe and sibling_recipe.get("source") == "recipe":
            sibling_recipe["canonical_id"] = canonical_id
            sibling_recipe["display_name"] = display_name
            sibling_recipe["recipe_from_canonical_id"] = sibling_id
            return sibling_recipe

    return None


def _build_recipe_result(
    canonical_id,
    display_name,
    match_tier,
    settlement_type,
    db_conn,
    qty_requested,
    _visited,
    _memo,
    material=None,
    include_all_alternatives=False,
    recipe_type_filter=None,
):
    """Compute the best recipe-based result for a canonical item, or None if no recipes exist."""
    with db_conn.cursor() as cur:
        if recipe_type_filter is None:
            cur.execute(
                """
                SELECT id, output_qty, recipe_type
                FROM recipes
                WHERE output_canonical_id = %s
                ORDER BY id ASC
                """,
                (canonical_id,),
            )
        else:
            cur.execute(
                """
                SELECT id, output_qty, recipe_type
                FROM recipes
                WHERE output_canonical_id = %s
                  AND recipe_type = %s
                ORDER BY id ASC
                """,
                (canonical_id, recipe_type_filter),
            )
        recipes = cur.fetchall()

    if not recipes:
        return None

    recipe_results = []
    for recipe_id, output_qty, recipe_type in recipes:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ri.input_canonical_id,
                    ri.input_game_code,
                    ri.qty,
                    ri.ratio_min,
                    ri.ratio_max,
                    ri.variant_group_id,
                    ri.is_primary_variant,
                    ci.game_code,
                    ci.display_name
                FROM recipe_ingredients ri
                LEFT JOIN canonical_items ci ON ci.id = ri.input_canonical_id
                WHERE recipe_id = %s
                """,
                (recipe_id,),
            )
            ingredient_rows = cur.fetchall()

        ingredient_results = []
        recipe_partial = False
        recipe_has_cycle_or_unresolved = False
        ingredient_total_cost = 0.0
        total_ingredient_count = 0
        resolved_ingredient_count = 0
        material_match_score = 0

        for (
            input_canonical_id,
            input_game_code,
            ing_qty,
            ratio_min,
            ratio_max,
            variant_group_id,
            is_primary_variant,
            ingredient_game_code,
            ingredient_display_name,
        ) in ingredient_rows:
            resolved_input_canonical_id = input_canonical_id
            if resolved_input_canonical_id is None:
                resolved_input_canonical_id = _resolve_wildcard_input_canonical_id(
                    input_game_code=input_game_code,
                    settlement_type=settlement_type,
                    db_conn=db_conn,
                    _visited=_visited,
                    _memo=_memo,
                    material=material,
                    include_all_alternatives=include_all_alternatives,
                )

            if resolved_input_canonical_id is None:
                continue

            if variant_group_id is not None and not is_primary_variant:
                continue

            total_ingredient_count += 1

            if ing_qty is not None:
                resolved_ing_qty = float(ing_qty)
            elif ratio_min is not None or ratio_max is not None:
                resolved_ratio_min = float(ratio_min) if ratio_min is not None else float(ratio_max or 0.0)
                resolved_ratio_max = float(ratio_max) if ratio_max is not None else float(ratio_min or 0.0)
                resolved_ing_qty = (resolved_ratio_min + resolved_ratio_max) / 2.0
            else:
                resolved_ing_qty = 1.0

            if _is_non_consumable_tool_ingredient(
                resolved_input_canonical_id,
                input_game_code,
                ingredient_game_code,
                ingredient_display_name,
            ):
                ingredient_result = {
                    "canonical_id": resolved_input_canonical_id,
                    "display_name": ingredient_display_name or resolved_input_canonical_id,
                    "quantity": resolved_ing_qty,
                    "unit_cost": 0.0,
                    "total_cost": 0.0,
                    "source": "non_consumable_tool",
                    "price_source": "non_consumable_tool",
                    "lr_unit_price": None,
                    "crafting_cost": None,
                    "crafting_breakdown": None,
                    "is_partial": False,
                    "match_tier": None,
                    "recipe_type": None,
                    "ingredients": None,
                    "quality_prices": None,
                    "resolved_ingredient_count": None,
                    "total_ingredient_count": None,
                    "completeness_ratio": None,
                    "non_consumable_tool": True,
                }
                ingredient_results.append(ingredient_result)
                resolved_ingredient_count += 1
                continue

            ingredient_result = calculate_cost(
                resolved_input_canonical_id,
                settlement_type,
                db_conn,
                resolved_ing_qty,
                _visited.copy(),
                include_recipe_alternative=False,
                _memo=_memo,
                material=material,
                include_all_alternatives=include_all_alternatives,
            )
            ingredient_results.append(ingredient_result)

            if _ingredient_matches_material(
                resolved_input_canonical_id,
                ingredient_game_code,
                ingredient_display_name,
                material,
            ):
                material_match_score += 1

            ingredient_source = ingredient_result.get("source")
            ingredient_unit_cost = ingredient_result.get("unit_cost")
            ingredient_total_cost_value = ingredient_result.get("total_cost")
            ingredient_is_partial = bool(ingredient_result.get("is_partial"))
            ingredient_error = ingredient_result.get("error")

            if ingredient_source == "unresolved" or ingredient_error == "circular_reference":
                recipe_has_cycle_or_unresolved = True

            if (
                ingredient_source in {"unresolved", "not_found"}
                or ingredient_unit_cost is None
                or ingredient_total_cost_value is None
            ):
                recipe_partial = True
                continue

            if ingredient_is_partial:
                recipe_partial = True

            if not ingredient_is_partial:
                resolved_ingredient_count += 1

            ingredient_total_cost += float(ingredient_total_cost_value)

        resolved_output_qty = float(output_qty) if output_qty else 1.0
        unit_cost = ingredient_total_cost / resolved_output_qty if resolved_output_qty else None
        total_cost = unit_cost * qty_requested if unit_cost is not None else None
        completeness_ratio = (
            float(resolved_ingredient_count) / float(total_ingredient_count)
            if total_ingredient_count
            else 1.0
        )

        # Do not emit zero-cost recipe totals when every ingredient in this branch
        # is unresolved/non-priced.
        if total_ingredient_count > 0 and resolved_ingredient_count == 0:
            unit_cost = None
            total_cost = None

        # Hard fail recipe paths that encountered unresolved/cycle ingredients so
        # caller can safely fall back to override/LR precedence.
        if recipe_has_cycle_or_unresolved:
            recipe_partial = True
            unit_cost = None
            total_cost = None

        recipe_result = {
            "_recipe_id": recipe_id,
            "canonical_id": canonical_id,
            "display_name": display_name,
            "quantity": qty_requested,
            "unit_cost": unit_cost,
            "total_cost": total_cost,
            "source": "recipe",
            "is_partial": recipe_partial,
            "match_tier": match_tier,
            "recipe_type": recipe_type,
            "ingredients": ingredient_results,
            "quality_prices": None,
            "resolved_ingredient_count": resolved_ingredient_count,
            "total_ingredient_count": total_ingredient_count,
            "completeness_ratio": completeness_ratio,
            "_material_match_score": material_match_score,
        }
        recipe_results.append(recipe_result)

    recipe_results = _filter_recipe_results_for_material(recipe_results, material)

    candidates = [r for r in recipe_results if r["unit_cost"] is not None]
    complete_candidates = [r for r in candidates if not r.get("is_partial")]

    if complete_candidates:
        best_complete = min(
            complete_candidates,
            key=lambda r: (
                -(r.get("_material_match_score") or 0),
                r["unit_cost"],
                r.get("_recipe_id", float("inf")),
            ),
        )
        best_complete.pop("_recipe_id", None)
        best_complete.pop("_material_match_score", None)
        return best_complete

    if candidates:
        best_candidate = sorted(
            candidates,
            key=lambda r: (
                -(r.get("_material_match_score") or 0),
                -(r.get("completeness_ratio") or 0.0),
                -(r.get("resolved_ingredient_count") or 0),
                r.get("unit_cost") if r.get("unit_cost") is not None else float("inf"),
            ),
        )[0]
        best_candidate.pop("_recipe_id", None)
        best_candidate.pop("_material_match_score", None)
        return best_candidate

    # If recipe structure exists but pricing is incomplete/unavailable, surface the
    # best partial recipe instead of collapsing to a bare unresolved result.
    partial_candidates = [
        r
        for r in recipe_results
        if (r.get("total_ingredient_count") or 0) > 0
        and isinstance(r.get("ingredients"), list)
        and len(r.get("ingredients") or []) > 0
    ]
    if partial_candidates:
        best_partial = sorted(
            partial_candidates,
            key=lambda r: (
                -(r.get("_material_match_score") or 0),
                -(r.get("completeness_ratio") or 0.0),
                -(r.get("resolved_ingredient_count") or 0),
                r.get("_recipe_id", float("inf")),
            ),
        )[0]
        best_partial["source"] = "recipe"
        best_partial["is_partial"] = True
        best_partial["unit_cost"] = None
        best_partial["total_cost"] = None
        best_partial.pop("_recipe_id", None)
        best_partial.pop("_material_match_score", None)
        return best_partial

    return {
        "canonical_id": canonical_id,
        "display_name": display_name,
        "quantity": qty_requested,
        "unit_cost": None,
        "total_cost": None,
        "source": "unresolved",
        "is_partial": True,
        "match_tier": match_tier,
        "recipe_type": None,
        "ingredients": None,
        "quality_prices": None,
        "resolved_ingredient_count": None,
        "total_ingredient_count": None,
        "completeness_ratio": None,
    }


def _has_recipe_type(canonical_id, recipe_type, db_conn):
    """Return True when canonical item has at least one recipe of the given type."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM recipes
            WHERE output_canonical_id = %s
              AND recipe_type = %s
            LIMIT 1
            """,
            (canonical_id, recipe_type),
        )
        return cur.fetchone() is not None


def _scale_cost_result(result, new_quantity):
    """Return a scaled copy of a cost result for a different requested quantity."""
    scaled = copy.deepcopy(result)

    try:
        old_quantity = float(scaled.get("quantity") or 0.0)
    except (TypeError, ValueError):
        old_quantity = 0.0

    try:
        new_quantity = float(new_quantity or 0.0)
    except (TypeError, ValueError):
        new_quantity = 0.0

    scaled["quantity"] = new_quantity

    unit_cost = scaled.get("unit_cost")
    if unit_cost is not None:
        scaled["total_cost"] = float(unit_cost) * new_quantity
    elif scaled.get("total_cost") is not None and old_quantity:
        factor = new_quantity / old_quantity
        scaled["total_cost"] = float(scaled["total_cost"]) * factor

    if old_quantity and old_quantity != 0:
        factor = new_quantity / old_quantity
    else:
        factor = 1.0

    ingredients = scaled.get("ingredients")
    if isinstance(ingredients, list):
        scaled["ingredients"] = [
            _scale_cost_result(ingredient, (ingredient.get("quantity") or 0.0) * factor)
            for ingredient in ingredients
        ]

    recipe_alternative = scaled.get("recipe_alternative")
    if isinstance(recipe_alternative, dict):
        scaled["recipe_alternative"] = _scale_cost_result(
            recipe_alternative,
            (recipe_alternative.get("quantity") or 0.0) * factor,
        )

    crafting_breakdown = scaled.get("crafting_breakdown")
    if isinstance(crafting_breakdown, dict):
        scaled["crafting_breakdown"] = _scale_cost_result(
            crafting_breakdown,
            (crafting_breakdown.get("quantity") or 0.0) * factor,
        )

    return scaled


def get_lr_price(canonical_id, settlement_type, db_conn):
    """Return (unit_price, source) for a canonical item.

    Returns:
        - (None, None) if no price can be resolved
        - source='lr_price' for lr_items-backed pricing
        - unit_price_current for lr_items-backed pricing
    """
    settlement_key = (settlement_type or "").strip().lower()
    settlement_column = SETTLEMENT_COLUMNS.get(settlement_key, "price_current")

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT lr_item_id, game_code, match_tier
            FROM canonical_items
            WHERE id = %s
            """,
            (canonical_id,),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None, None

        lr_item_id, game_code, match_tier = row

        query = sql.SQL(
            """
            SELECT count, {settlement_col}, unit_price_current, lr_sub_category
            FROM lr_items
            WHERE id = %s
            """
        ).format(settlement_col=sql.Identifier(settlement_column))

        cur.execute(query, (lr_item_id,))
        price_row = cur.fetchone()
        if not price_row:
            return None, None

        count_value, settlement_price_value, current_unit_price, lr_sub_category = price_row

        # Guardrail: metalplate game-code rows should not inherit LR ingot pricing
        # from fuzzy/contains canonical linkage. If linked LR row is not explicitly
        # in the PLATE sub-category, prefer recipe pricing.
        normalized_game_code = (game_code or "").strip().lower()
        normalized_lr_sub_category = (lr_sub_category or "").strip().upper()
        if normalized_game_code.startswith("item:metalplate-") and normalized_lr_sub_category != "PLATE":
            if (match_tier or "").lower() == "mapped":
                print(
                    "[WARN] mapped-tier metalplate LR link bypassed sub-category guardrail: "
                    f"canonical_id={canonical_id}, lr_item_id={lr_item_id}, lr_sub_category={lr_sub_category}"
                )
            else:
                return None, None

        if settlement_price_value not in (None, 0) and count_value not in (None, 0):
            return float(settlement_price_value) / float(count_value), "lr_price"

        if current_unit_price is None:
            return None, None

        return float(current_unit_price), "lr_price"


def get_manual_override_price(canonical_id, db_conn):
    """Return (unit_price, 'manual_override') for canonical override rows."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT unit_price
            FROM price_overrides
            WHERE canonical_id = %s
            """,
            (canonical_id,),
        )
        row = cur.fetchone()

    if not row or row[0] is None:
        return None, None

    return float(row[0]), "manual_override"


def get_lr_quality_prices(canonical_id, settlement_type, db_conn):
    """Return LR quality unit prices for quality-tiered items, else None.

    For quality-tiered entries this includes:
      common (from base_value/count), uncommon, rare, epic, legendary

    For non-tiered entries this intentionally returns None so the caller keeps
    using the primary LR unit price path (price_current / count).
    """
    settlement_key = (settlement_type or "").strip().lower()
    settlement_column = SETTLEMENT_COLUMNS.get(settlement_key, "price_current")
    settlement_suffix = settlement_column.replace("price_", "", 1)

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT lr_item_id
            FROM canonical_items
            WHERE id = %s
            """,
            (canonical_id,),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None

        lr_item_id = row[0]

        quality_cols = [
            f"price_uncommon_{settlement_suffix}",
            f"price_rare_{settlement_suffix}",
            f"price_epic_{settlement_suffix}",
            f"price_legendary_{settlement_suffix}",
        ]

        query = sql.SQL(
            """
            SELECT has_quality_tiers, count, base_value, {settlement_col}, {cols}
            FROM lr_items
            WHERE id = %s
            """
        ).format(
            settlement_col=sql.Identifier(settlement_column),
            cols=sql.SQL(", ").join(sql.Identifier(col) for col in quality_cols),
        )

        cur.execute(query, (lr_item_id,))
        price_row = cur.fetchone()
        if not price_row:
            return None

        has_quality_tiers, count, base_value, settlement_price_value, *quality_values = price_row

        common_unit_price = None
        if settlement_price_value is not None and count:
            common_unit_price = float(settlement_price_value) / float(count)
        elif base_value is not None and count:
            common_unit_price = float(base_value) / float(count)

        if not has_quality_tiers:
            return None

        quality_prices = {"common": common_unit_price}
        for tier, raw_price in zip(QUALITY_PREFIXES, quality_values):
            if raw_price is None or not count:
                quality_prices[tier] = None
            else:
                quality_prices[tier] = float(raw_price) / float(count)

        return quality_prices


def resolve_canonical_id(user_input, db_conn):
    """Resolve free-form user text to best-matching canonical_id via trigram similarity."""
    normalized = (user_input or "").strip().lower()
    if not normalized:
        return None

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT ia.canonical_id
            FROM item_aliases ia
            JOIN canonical_items ci ON ci.id = ia.canonical_id
            WHERE similarity(ia.alias, %s) > 0.3
            ORDER BY
                CASE WHEN ia.alias = %s THEN 0 ELSE 1 END,
                similarity(ia.alias, %s) DESC,
                CASE WHEN ci.lr_item_id IS NOT NULL THEN 0 ELSE 1 END,
                ci.id
            LIMIT 1
            """,
            (normalized, normalized, normalized),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        # Fallback: token-aware display-name matching to handle word-order differences
        # like "flint knife blade" vs "Knifeblade Flint".
        tokens = [t for t in re.split(r"[^a-z0-9]+", normalized) if t]
        if tokens:
            token_filters = [sql.SQL("lower(ci.display_name) LIKE %s") for _ in tokens]
            token_values = [f"%{t}%" for t in tokens]
            token_query = sql.SQL(
                """
                SELECT ci.id
                FROM canonical_items ci
                WHERE {token_where}
                ORDER BY
                    CASE WHEN ci.lr_item_id IS NOT NULL THEN 0 ELSE 1 END,
                    ci.id
                LIMIT 1
                """
            ).format(token_where=sql.SQL(" AND ").join(token_filters))
            cur.execute(token_query, token_values)
            row = cur.fetchone()
            if row:
                return row[0]

        # Fallback: when alias coverage is sparse, try direct display-name similarity.
        cur.execute(
            """
            SELECT ci.id
            FROM canonical_items ci
            WHERE similarity(lower(ci.display_name), %s) > 0.2
            ORDER BY
                CASE WHEN lower(ci.display_name) = %s THEN 0 ELSE 1 END,
                similarity(lower(ci.display_name), %s) DESC,
                CASE WHEN ci.lr_item_id IS NOT NULL THEN 0 ELSE 1 END,
                ci.id
            LIMIT 1
            """,
            (normalized, normalized, normalized),
        )
        row = cur.fetchone()
        return row[0] if row else None


def resolve_variant_material_canonical(canonical_id, material, db_conn):
    """If canonical belongs to a variant family, return the requested material variant id."""
    normalized_material = (material or "").strip().lower()
    if not canonical_id or not normalized_material:
        return canonical_id

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT variant_family, game_code
            FROM canonical_items
            WHERE id = %s
            """,
            (canonical_id,),
        )
        row = cur.fetchone()
        if not row:
            return canonical_id

        variant_family = row[0]
        if not variant_family:
            inferred_family, _inferred_material = _infer_variant_from_game_code(row[1])
            if not inferred_family:
                return canonical_id
            variant_family = inferred_family

        cur.execute(
            """
            SELECT id
            FROM canonical_items
            WHERE variant_family = %s
              AND variant_material = %s
            ORDER BY id
            LIMIT 1
            """,
            (variant_family, normalized_material),
        )
        variant_row = cur.fetchone()

    return variant_row[0] if variant_row else canonical_id


def calculate_cost(
    canonical_id,
    settlement_type,
    db_conn,
    quantity=1,
    _visited=None,
    _memo=None,
    material=None,
    include_recipe_alternative=True,
    include_all_alternatives=False,
    labor_markup=False,
):
    """Resolve unit/total cost for an item from LR prices or crafting recipes."""
    if _visited is None:
        _visited = set()
    if _memo is None:
        _memo = {}

    qty_requested = float(quantity or 0)
    settlement_key = (settlement_type or "").strip().lower()

    memo_key = (canonical_id, settlement_key, qty_requested)
    if memo_key in _memo:
        return copy.deepcopy(_memo[memo_key])

    base_key_prefix = (canonical_id, settlement_key)
    for cached_key, cached_value in _memo.items():
        if cached_key[0] == base_key_prefix[0] and cached_key[1] == base_key_prefix[1]:
            if cached_value.get("unit_cost") is None or cached_value.get("is_partial"):
                continue
            return _scale_cost_result(cached_value, qty_requested)

    if canonical_id in _visited:
        override_unit_price, override_price_source = get_manual_override_price(canonical_id, db_conn)
        if override_unit_price is not None:
            unit_cost = override_unit_price * 1.2 if labor_markup else override_unit_price
            result = {
                "canonical_id": canonical_id,
                "display_name": canonical_id,
                "quantity": qty_requested,
                "unit_cost": unit_cost,
                "total_cost": unit_cost * qty_requested,
                "source": override_price_source or "manual_override",
                "price_source": override_price_source or "manual_override",
                "lr_unit_price": None,
                "crafting_cost": None,
                "crafting_breakdown": None,
                "is_partial": False,
                "match_tier": None,
                "recipe_type": None,
                "ingredients": None,
                "quality_prices": None,
                "resolved_ingredient_count": None,
                "total_ingredient_count": None,
                "completeness_ratio": None,
            }
            _memo[memo_key] = copy.deepcopy(result)
            return result

        result = {
            "canonical_id": canonical_id,
            "display_name": canonical_id,
            "quantity": qty_requested,
            "unit_cost": None,
            "total_cost": None,
            "source": "unresolved",
            "price_source": "unresolved",
            "lr_unit_price": None,
            "crafting_cost": None,
            "crafting_breakdown": None,
            "is_partial": True,
            "match_tier": None,
            "recipe_type": None,
            "ingredients": None,
            "quality_prices": None,
            "resolved_ingredient_count": None,
            "total_ingredient_count": None,
            "completeness_ratio": None,
            "error": "circular_reference",
        }
        _memo[memo_key] = copy.deepcopy(result)
        return result

    _visited.add(canonical_id)

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT display_name, match_tier
            FROM canonical_items
            WHERE id = %s
            """,
            (canonical_id,),
        )
        item_row = cur.fetchone()

    display_name = item_row[0] if item_row else canonical_id
    match_tier = item_row[1] if item_row else None

    lr_unit_price, lr_price_source = get_lr_price(canonical_id, settlement_type, db_conn)
    if lr_unit_price is not None:
        unit_cost = lr_unit_price * 1.2 if labor_markup else lr_unit_price
        quality_prices = get_lr_quality_prices(canonical_id, settlement_type, db_conn)

        result = {
            "canonical_id": canonical_id,
            "display_name": display_name,
            "quantity": qty_requested,
            "unit_cost": unit_cost,
            "total_cost": unit_cost * qty_requested,
            "source": lr_price_source or "lr_price",
            "price_source": lr_price_source or "lr_price",
            "lr_unit_price": lr_unit_price,
            "crafting_cost": None,
            "crafting_breakdown": None,
            "is_partial": False,
            "match_tier": match_tier,
            "recipe_type": None,
            "ingredients": None,
            "quality_prices": quality_prices,
            "recipe_alternative": None,
            "resolved_ingredient_count": None,
            "total_ingredient_count": None,
            "completeness_ratio": None,
        }
        _memo[memo_key] = copy.deepcopy(result)
        return result

    # Check override before recipe traversal.
    override_unit_price, override_price_source = get_manual_override_price(canonical_id, db_conn)

    alloy_recipe_result = None
    if _has_recipe_type(canonical_id, "alloy", db_conn):
        alloy_recipe_result = _build_recipe_result(
            canonical_id=canonical_id,
            display_name=display_name,
            match_tier=match_tier,
            settlement_type=settlement_type,
            db_conn=db_conn,
            qty_requested=qty_requested,
            _visited=_visited,
            _memo=_memo,
            material=material,
            include_all_alternatives=include_all_alternatives,
            recipe_type_filter="alloy",
        )

    recipe_result = alloy_recipe_result or _build_recipe_result(
        canonical_id=canonical_id,
        display_name=display_name,
        match_tier=match_tier,
        settlement_type=settlement_type,
        db_conn=db_conn,
        qty_requested=qty_requested,
        _visited=_visited,
        _memo=_memo,
        material=material,
        include_all_alternatives=include_all_alternatives,
    )

    crafting_unit_cost = recipe_result.get("unit_cost") if recipe_result else None
    recipe_is_usable = (
        recipe_result is not None
        and not recipe_result.get("is_partial")
        and recipe_result.get("unit_cost") is not None
    )

    if override_unit_price is not None:
        if (
            not recipe_is_usable
            or crafting_unit_cost is None
            or float(crafting_unit_cost) >= float(override_unit_price)
        ):
            unit_cost = override_unit_price * 1.2 if labor_markup else override_unit_price
            result = {
                "canonical_id": canonical_id,
                "display_name": display_name,
                "quantity": qty_requested,
                "unit_cost": unit_cost,
                "total_cost": unit_cost * qty_requested,
                "source": override_price_source or "manual_override",
                "price_source": override_price_source or "manual_override",
                "lr_unit_price": None,
                "crafting_cost": crafting_unit_cost,
                "crafting_breakdown": (
                    recipe_result
                    if (
                        recipe_result
                        and not recipe_result.get("is_partial")
                        and recipe_result.get("unit_cost") is not None
                    )
                    else None
                ),
                "is_partial": False,
                "match_tier": match_tier,
                "recipe_type": None,
                "ingredients": None,
                "quality_prices": None,
                "recipe_alternative": None,
                "resolved_ingredient_count": None,
                "total_ingredient_count": None,
                "completeness_ratio": None,
            }
            _memo[memo_key] = copy.deepcopy(result)
            return result

    if recipe_is_usable:
        recipe_result["price_source"] = recipe_result.get("source")
        recipe_result["lr_unit_price"] = None
        recipe_result["crafting_cost"] = crafting_unit_cost
        recipe_result["crafting_breakdown"] = None

        if labor_markup and recipe_result.get("unit_cost") is not None:
            recipe_result["unit_cost"] = float(recipe_result["unit_cost"]) * 1.2
            recipe_result["total_cost"] = recipe_result["unit_cost"] * qty_requested

        _memo[memo_key] = copy.deepcopy(recipe_result)
        return recipe_result

    if recipe_result:
        recipe_result["price_source"] = recipe_result.get("source")
        recipe_result["lr_unit_price"] = None
        recipe_result["crafting_cost"] = crafting_unit_cost
        recipe_result["crafting_breakdown"] = None
        _memo[memo_key] = copy.deepcopy(recipe_result)
        return recipe_result

    if override_unit_price is not None:
        unit_cost = override_unit_price * 1.2 if labor_markup else override_unit_price
        result = {
            "canonical_id": canonical_id,
            "display_name": display_name,
            "quantity": qty_requested,
            "unit_cost": unit_cost,
            "total_cost": unit_cost * qty_requested,
            "source": override_price_source or "manual_override",
            "price_source": override_price_source or "manual_override",
            "lr_unit_price": None,
            "crafting_cost": None,
            "crafting_breakdown": None,
            "is_partial": False,
            "match_tier": match_tier,
            "recipe_type": None,
            "ingredients": None,
            "quality_prices": None,
            "recipe_alternative": None,
            "resolved_ingredient_count": None,
            "total_ingredient_count": None,
            "completeness_ratio": None,
        }
        _memo[memo_key] = copy.deepcopy(result)
        return result

    result = {
        "canonical_id": canonical_id,
        "display_name": display_name,
        "quantity": qty_requested,
        "unit_cost": None,
        "total_cost": None,
        "source": "unresolved",
        "price_source": "unresolved",
        "lr_unit_price": None,
        "crafting_cost": None,
        "crafting_breakdown": None,
        "is_partial": True,
        "match_tier": match_tier,
        "recipe_type": None,
        "ingredients": None,
        "quality_prices": None,
        "resolved_ingredient_count": None,
        "total_ingredient_count": None,
        "completeness_ratio": None,
    }
    _memo[memo_key] = copy.deepcopy(result)
    return result


def parse_order_input(raw_string):
    """Parse a comma-separated order string into raw item+quantity dicts."""
    parsed_items = []
    if not raw_string:
        return parsed_items

    for part in raw_string.split(","):
        segment = (part or "").strip()
        if not segment:
            continue

        match = re.match(r"^\s*(\d+(?:\.\d+)?)\s+(.+)$", segment)
        if match:
            quantity = float(match.group(1))
            raw_name = match.group(2).strip()
        else:
            quantity = 1.0
            raw_name = segment

        if not raw_name:
            continue

        parsed_items.append({"raw": raw_name, "quantity": quantity})

    return parsed_items


def process_order(
    items_input,
    settlement_type,
    db_conn,
    labor_markup=False,
    include_all_alternatives=False,
    material=None,
):
    """Resolve a list of requested items and aggregate pricing totals."""
    results = []
    total_lr_cost = 0.0
    total_recipe_cost = 0.0
    total_combined = 0.0
    has_unresolved = False
    memo = {}

    for item in items_input or []:
        item = item or {}
        raw = (item.get("raw") or "").strip()

        try:
            quantity = float(item.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 1.0

        canonical_id = resolve_canonical_id(raw, db_conn) if raw else None
        if canonical_id and material:
            canonical_id = resolve_variant_material_canonical(canonical_id, material, db_conn)

        if canonical_id is None:
            result = {
                "canonical_id": None,
                "display_name": raw,
                "quantity": quantity,
                "unit_cost": None,
                "total_cost": None,
                "source": "not_found",
                "price_source": "not_found",
                "lr_unit_price": None,
                "crafting_cost": None,
                "crafting_breakdown": None,
                "is_partial": False,
                "match_tier": None,
                "recipe_type": None,
                "ingredients": None,
                "quality_prices": None,
                "resolved_ingredient_count": None,
                "total_ingredient_count": None,
                "completeness_ratio": None,
            }
        else:
            result = calculate_cost(
                canonical_id,
                settlement_type,
                db_conn,
                quantity,
                _memo=memo,
                material=material,
                include_all_alternatives=include_all_alternatives,
                labor_markup=labor_markup,
            )

        results.append(result)

        source = result.get("source")
        total_cost = result.get("total_cost")

        if total_cost is not None:
            total_cost = float(total_cost)
            total_combined += total_cost
            if source in {"lr_price", "manual_override"}:
                total_lr_cost += total_cost
            elif source == "recipe":
                total_recipe_cost += total_cost

        if source in {"unresolved", "not_found"}:
            has_unresolved = True

    return {
        "settlement_type": settlement_type,
        "items": results,
        "totals": {
            "total_lr_cost": total_lr_cost,
            "total_recipe_cost": total_recipe_cost,
            "total_combined": total_combined,
            "has_unresolved": has_unresolved,
        },
    }
