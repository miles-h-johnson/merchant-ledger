#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import re
import math

from dotenv import load_dotenv
from flask_cors import CORS
from flask import Flask, jsonify, request, send_from_directory
from psycopg2.pool import ThreadedConnectionPool


# [MEMORY BANK: ACTIVE]
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.resolver import parse_order_input, process_order  # noqa: E402


load_dotenv()

DIST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "webapp", "dist"))

DATABASE_URL = os.getenv("DATABASE_URL")

pool_kwargs = {"minconn": 1, "maxconn": 10}
if DATABASE_URL:
    pool_kwargs["dsn"] = DATABASE_URL
else:
    db_name = os.getenv("DB_NAME") or os.getenv("PGDATABASE")
    if not db_name:
        raise RuntimeError("DATABASE_URL or DB_NAME/PGDATABASE must be set")

    pool_kwargs.update(
        {
            "host": os.getenv("DB_HOST") or os.getenv("PGHOST") or "localhost",
            "port": int(os.getenv("DB_PORT") or os.getenv("PGPORT") or 5432),
            "dbname": db_name,
            "user": os.getenv("DB_USER") or os.getenv("PGUSER"),
            "password": os.getenv("DB_PASSWORD") or os.getenv("PGPASSWORD"),
        }
    )

pool = ThreadedConnectionPool(**pool_kwargs)

app = Flask(__name__, static_folder=DIST_DIR, static_url_path="/")
CORS(app)


def _humanize_variant_family(family_key: str) -> str:
    text = (family_key or "").strip()
    text = text.replace("-", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.title() if text else "Variant"


def _infer_variant_from_game_code(game_code: str) -> tuple[str | None, str | None]:
    text = (game_code or "").strip().lower()
    if not text.startswith("item:"):
        return None, None

    tail = text.split(":", 1)[1]
    if "-" not in tail:
        return None, None

    family, material = tail.rsplit("-", 1)
    if family not in {
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
    }:
        return None, None

    if not material:
        return None, None

    return family, material


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok"}), 200


@app.post("/calculate")
def calculate() -> tuple:
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object", "code": "invalid_request"}), 400

    order_value = data.get("order")
    if order_value is None or not isinstance(order_value, str) or not order_value.strip():
        return jsonify({"error": "order is required", "code": "invalid_request"}), 400

    order = order_value.strip()
    settlement_type_value = data.get("settlement_type")
    settlement_type = (
        settlement_type_value.strip()
        if isinstance(settlement_type_value, str)
        else "current"
    ) or "current"
    labor_markup = bool(data.get("labor_markup", False))
    include_all_alternatives = bool(data.get("include_all_alternatives", False))
    material_value = data.get("material")
    material = material_value.strip().lower() if isinstance(material_value, str) else ""
    material = material or None

    quantity_segment_pattern = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(.+)$")
    for part in order.split(","):
        segment = (part or "").strip()
        if not segment:
            continue
        if re.match(r"^[+-]?\d", segment) and not quantity_segment_pattern.match(segment):
            return jsonify({"error": f"Invalid quantity format in segment: '{segment}'", "code": "invalid_request"}), 400

    conn = None
    try:
        items = parse_order_input(order)
        if not items:
            return jsonify({"error": "No valid item names were provided", "code": "invalid_request"}), 400

        for item in items:
            raw_item = (item.get("raw") or "").strip()
            quantity = item.get("quantity")
            if not raw_item:
                return jsonify({"error": "No valid item names were provided", "code": "invalid_request"}), 400
            if not isinstance(quantity, (int, float)) or not math.isfinite(float(quantity)) or float(quantity) <= 0:
                return jsonify({"error": f"Invalid quantity for item '{raw_item}'", "code": "invalid_request"}), 400

        conn = pool.getconn()
        result = process_order(
            items,
            settlement_type,
            conn,
            labor_markup=labor_markup,
            include_all_alternatives=include_all_alternatives,
            material=material,
        )

        not_found_item = next((item for item in result.get("items", []) if item.get("source") == "not_found"), None)
        if not_found_item is not None:
            query = (not_found_item.get("display_name") or "").strip()
            return jsonify({"error": "Item not found", "code": "item_not_found", "query": query}), 404

        if any(item.get("source") == "unresolved" for item in result.get("items", [])):
            result = dict(result)
            result["unresolvable"] = True

        return jsonify(result), 200
    except Exception:
        app.logger.exception("Unexpected error while processing /calculate request")
        return jsonify({"error": "Internal server error", "code": "server_error"}), 500
    finally:
        if conn is not None:
            pool.putconn(conn)


@app.get("/search")
def search() -> tuple:
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "q is required and must be at least 2 characters"}), 400

    limit_raw = (request.args.get("limit") or "").strip()
    limit = 10
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400

    if limit < 1:
        return jsonify({"error": "limit must be at least 1"}), 400
    if limit > 25:
        limit = 25

    raw_limit = min(max(limit * 10, 50), 300)

    query = """
    WITH ranked AS (
        SELECT
            ci.id as canonical_id,
            ci.display_name,
            ci.variant_family,
            ci.variant_material,
            ci.game_code,
            ci.lr_item_id,
            ci.match_tier,
            li.lr_category,
            li.price_current,
            po.unit_price as override_price,
            CASE WHEN ci.lr_item_id IS NOT NULL OR po.unit_price IS NOT NULL THEN 1 ELSE 0 END as has_linked_price,
            CASE WHEN EXISTS (
                SELECT 1
                FROM recipes r
                WHERE r.output_canonical_id = ci.id
                LIMIT 1
            ) THEN 1 ELSE 0 END as has_recipe,
            ia.alias as matched_alias,
            similarity(ia.alias, %(q)s) as score,
            ROW_NUMBER() OVER (
                PARTITION BY ci.id
                ORDER BY
                    CASE WHEN lower(ia.alias) = lower(%(q)s) THEN 0 ELSE 1 END,
                    similarity(ia.alias, %(q)s) DESC,
                    CASE WHEN lower(ci.display_name) = lower(%(q)s) THEN 0 ELSE 1 END,
                    similarity(ci.display_name, %(q)s) DESC,
                    CASE WHEN li.id IS NOT NULL THEN 0 ELSE 1 END,
                    ci.id
            ) as rn
        FROM item_aliases ia
        JOIN canonical_items ci ON ia.canonical_id = ci.id
        LEFT JOIN lr_items li ON ci.lr_item_id = li.id
        LEFT JOIN price_overrides po ON ci.id = po.canonical_id
        WHERE similarity(ia.alias, %(q)s) > 0.15
          AND (ci.game_code IS NULL OR ci.game_code NOT LIKE '%%-*')
          AND ci.display_name NOT LIKE '%%*%%'
          AND ci.display_name NOT LIKE '%%{%%'
          AND ci.display_name NOT LIKE '%%}%%'
    )
    SELECT canonical_id, display_name, variant_family, variant_material, game_code,
           lr_item_id, match_tier, lr_category, price_current,
           override_price, has_linked_price, has_recipe, matched_alias, score
    FROM ranked
    WHERE rn = 1
    ORDER BY score DESC, has_linked_price DESC, has_recipe DESC, canonical_id
    LIMIT %(raw_limit)s
    """

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, {"q": q, "raw_limit": raw_limit})
            rows = cur.fetchall()

        grouped = []
        family_index = {}

        for row in rows:
            canonical_id = row[0]
            display_name = row[1]
            variant_family = row[2]
            variant_material = row[3]
            game_code = row[4]
            lr_item_id = row[5]
            match_tier = row[6]
            lr_category = row[7]
            price_current = float(row[8]) if row[8] is not None else None
            override_price = float(row[9]) if row[9] is not None else None
            has_recipe = bool(row[11])
            matched_alias = row[12]
            score = round(float(row[13]), 2) if row[13] is not None else None
            has_any_price = (lr_item_id is not None) or (override_price is not None)

            if not variant_family:
                inferred_family, inferred_material = _infer_variant_from_game_code(game_code)
                if inferred_family:
                    variant_family = inferred_family
                if inferred_material and not variant_material:
                    variant_material = inferred_material

            if variant_family:
                family_result = family_index.get(variant_family)
                if family_result is None and (has_any_price or has_recipe):
                    family_result = {
                        "canonical_id": canonical_id,
                        "display_name": _humanize_variant_family(variant_family),
                        "variant_family": variant_family,
                        "available_materials": [],
                        "canonical_ids": {},
                        "match_tier": match_tier,
                        "lr_category": lr_category,
                        "price_current": price_current,
                        "matched_alias": matched_alias,
                        "score": score,
                    }
                    family_index[variant_family] = family_result
                    grouped.append(family_result)

                if (
                    family_result is not None
                    and (has_any_price or has_recipe)
                    and variant_material
                    and variant_material not in family_result["canonical_ids"]
                ):
                    family_result["canonical_ids"][variant_material] = canonical_id
                    family_result["available_materials"].append(variant_material)
                continue

            grouped.append(
                {
                    "canonical_id": canonical_id,
                    "display_name": display_name,
                    "variant_family": None,
                    "available_materials": None,
                    "canonical_ids": None,
                    "match_tier": match_tier,
                    "lr_category": lr_category,
                    "price_current": price_current,
                    "matched_alias": matched_alias,
                    "score": score,
                }
            )

        for result in grouped:
            materials = result.get("available_materials")
            if isinstance(materials, list):
                materials.sort()

        results = grouped[:limit]

        return jsonify({"query": q, "results": results}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn is not None:
            pool.putconn(conn)


@app.get("/diagnostics/missing-mods")
def diagnostics_missing_mods() -> tuple:
    query = """
    SELECT
        r.source_mod,
        COUNT(DISTINCT r.id) as total_recipes,
        COUNT(DISTINCT r.id) - COUNT(DISTINCT ri.recipe_id) as recipes_without_ingredients,
        ROUND(100.0 * (COUNT(DISTINCT r.id) - COUNT(DISTINCT ri.recipe_id)) / COUNT(DISTINCT r.id), 1) as pct_missing
    FROM recipes r
    LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
    GROUP BY r.source_mod
    HAVING COUNT(DISTINCT r.id) > 5
        AND (COUNT(DISTINCT r.id) - COUNT(DISTINCT ri.recipe_id))::float / COUNT(DISTINCT r.id) > 0.5
    ORDER BY pct_missing DESC
    """

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

        results = [
            {
                "source_mod": row[0],
                "total_recipes": int(row[1]) if row[1] is not None else 0,
                "recipes_without_ingredients": int(row[2]) if row[2] is not None else 0,
                "pct_missing": float(row[3]) if row[3] is not None else 0.0,
            }
            for row in rows
        ]

        return jsonify(results), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn is not None:
            pool.putconn(conn)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path: str):
    file_path = os.path.join(DIST_DIR, path)
    if path and os.path.exists(file_path) and os.path.isfile(file_path):
        return send_from_directory(DIST_DIR, path)
    return send_from_directory(DIST_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)