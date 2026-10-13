#!/usr/bin/env python3
"""Compute primitive fallback price overrides from deterministic LR-derived rules.

Rules are intentionally ordered and idempotent. Existing manual overrides are
never overwritten; only missing overrides or prior computed overrides are
inserted/updated.
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Sequence, Tuple

import psycopg2
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Rule:
    name: str
    target_query: str
    unit_price: Decimal
    note: str
    params: Tuple[object, ...] = ()


def ensure_price_overrides_table(cur) -> None:
    cur.execute("SELECT to_regclass('public.price_overrides')")
    row = cur.fetchone()
    if row and row[0]:
        return

    cur.execute(
        """
        CREATE TABLE price_overrides (
            canonical_id TEXT PRIMARY KEY REFERENCES canonical_items(id),
            unit_price NUMERIC NOT NULL,
            note TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    print("[INFO] Created missing table: price_overrides")


def _is_computed_note(note: str | None) -> bool:
    return bool(note and note.lower().startswith("computed:"))


def _select_target_ids(cur, query: str, params: Sequence[object]) -> List[str]:
    if params:
        cur.execute(query, params)
    else:
        cur.execute(query)
    return [row[0] for row in cur.fetchall() if row and row[0]]


def _upsert_computed(cur, canonical_id: str, unit_price: Decimal, note: str) -> int:
    cur.execute(
        """
        INSERT INTO price_overrides (canonical_id, unit_price, note, created_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (canonical_id) DO UPDATE
        SET unit_price = EXCLUDED.unit_price,
            note = EXCLUDED.note
        WHERE price_overrides.note ILIKE 'computed:%%'
        """,
        (canonical_id, unit_price, note),
    )
    return cur.rowcount


def apply_rule(cur, rule: Rule) -> Tuple[int, int, int, int]:
    target_ids = _select_target_ids(cur, rule.target_query, rule.params)

    inserted = 0
    updated = 0
    skipped_manual = 0

    for canonical_id in target_ids:
        cur.execute(
            "SELECT note FROM price_overrides WHERE canonical_id = %s",
            (canonical_id,),
        )
        row = cur.fetchone()

        if row is None:
            changed = _upsert_computed(cur, canonical_id, rule.unit_price, rule.note)
            if changed:
                inserted += 1
            continue

        existing_note = row[0]
        if not _is_computed_note(existing_note):
            skipped_manual += 1
            continue

        changed = _upsert_computed(cur, canonical_id, rule.unit_price, rule.note)
        if changed:
            updated += 1

    total_targets = len(target_ids)
    return inserted, updated, skipped_manual, total_targets


def get_price(cur, canonical_id: str) -> Decimal | None:
    """Get the best available price for a canonical item (LR > override)."""
    cur.execute(
        """
        SELECT COALESCE(li.unit_price_current, po.unit_price)
        FROM canonical_items ci
        LEFT JOIN lr_items li ON li.id = ci.lr_item_id
        LEFT JOIN price_overrides po ON po.canonical_id = ci.id
        WHERE ci.id = %s
        """,
        (canonical_id,),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return Decimal(str(row[0]))
    return None


def apply_metal_nails_and_strips_rules(cur) -> None:
    """Price metal nails/strips at 1/4 of their parent ingot where resolvable."""
    cur.execute(
        """
        SELECT ci.id, ci.game_code
        FROM canonical_items ci
        WHERE ci.id = 'metalnailsandstrips'
           OR ci.id LIKE 'metalnailsandstrips_%'
           OR ci.id = 'metalstrip'
        ORDER BY ci.id
        """
    )
    rows = cur.fetchall()

    # Canonical material mapping for known families/wildcards.
    material_overrides = {
        "metalnailsandstrips": "iron",
        "metalstrip": "iron",
        "metalnailsandstrips_iron_2": "iron",
        "metalnailsandstrips_bronze": "tinbronze",
    }

    applied = 0
    skipped = 0

    for canonical_id, game_code in rows:
        material = material_overrides.get(canonical_id)

        if not material:
            game_code_text = (game_code or "").strip().lower()
            if game_code_text.startswith("item:metalnailsandstrips-"):
                material = game_code_text.split("item:metalnailsandstrips-", 1)[1]

        if not material or "*" in material:
            skipped += 1
            continue

        ingot_price = get_price(cur, f"ingot_{material}")
        if ingot_price is None:
            skipped += 1
            continue

        nailstrip_price = (ingot_price / Decimal("4")).quantize(Decimal("0.000001"))
        note = f"computed: {canonical_id} = ingot_{material} / 4"

        cur.execute(
            "SELECT note FROM price_overrides WHERE canonical_id = %s",
            (canonical_id,),
        )
        existing = cur.fetchone()

        if existing is not None and not _is_computed_note(existing[0]):
            skipped += 1
            continue

        if _upsert_computed(cur, canonical_id, nailstrip_price, note):
            applied += 1

    print(
        f"Rule 7/8 (dynamic) — metal nails/strips from parent ingot / 4: "
        f"applied={applied}, skipped={skipped}"
    )


def get_lr_price(cur, canonical_id: str) -> Decimal | None:
    """Get LR unit price only (ignore overrides)."""
    cur.execute(
        """
        SELECT li.unit_price_current
        FROM canonical_items ci
        JOIN lr_items li ON li.id = ci.lr_item_id
        WHERE ci.id = %s
        """,
        (canonical_id,),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return Decimal(str(row[0]))
    return None


def apply_dynamic_anvil_rules(cur) -> None:
    """Rule 6k — Price anvil_<metal> at 10x ingot_<metal> LR price."""
    cur.execute(
        """
        SELECT ci.id
        FROM canonical_items ci
        LEFT JOIN lr_items li ON li.id = ci.lr_item_id
        WHERE ci.id ILIKE 'anvil\\_%' ESCAPE '\\'
          AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
        ORDER BY ci.id
        """
    )
    anvil_ids = [row[0] for row in cur.fetchall() if row and row[0]]

    applied = 0
    skipped_manual = 0
    skipped_missing_ingot = 0

    for anvil_id in anvil_ids:
        metal = anvil_id.split("anvil_", 1)[1].strip().lower()
        if not metal:
            skipped_missing_ingot += 1
            continue

        ingot_id = f"ingot_{metal}"
        ingot_price = get_lr_price(cur, ingot_id)
        if ingot_price is None:
            skipped_missing_ingot += 1
            print(
                f"[WARN] Rule 6k — missing LR price for '{ingot_id}' required by '{anvil_id}', skipping."
            )
            continue

        cur.execute(
            "SELECT note FROM price_overrides WHERE canonical_id = %s",
            (anvil_id,),
        )
        existing = cur.fetchone()
        if existing is not None and not _is_computed_note(existing[0]):
            skipped_manual += 1
            continue

        anvil_price = (ingot_price * Decimal("10")).quantize(Decimal("0.000001"))
        note = f"computed:anvil_{metal} = 10 x {metal}_ingot_price"
        if _upsert_computed(cur, anvil_id, anvil_price, note):
            applied += 1

    print(
        "Rule 6k (dynamic) — anvils from ingot LR price x 10: "
        f"targets={len(anvil_ids)}, applied={applied}, "
        f"skipped_manual={skipped_manual}, skipped_missing_ingot={skipped_missing_ingot}"
    )


def apply_pelt_rules(cur) -> None:
    """Rule 6q — Price hide_pelt_* from prepared hide LR rows by size tier."""
    # Task 1 LR mapping results:
    # AG039 -> Prepared Small Hide
    # AG040 -> Prepared Medium Hide
    # AG041 -> Prepared Large Hide
    # AG042 -> Prepared Huge Hide
    tier_config = {
        "small": {"canonical_id": "hide_pelt_small", "lr_item_id": "AG039", "stack_size": Decimal("64")},
        "medium": {"canonical_id": "hide_pelt_medium", "lr_item_id": "AG040", "stack_size": Decimal("32")},
        "large": {"canonical_id": "hide_pelt_large", "lr_item_id": "AG041", "stack_size": Decimal("16")},
        "huge": {"canonical_id": "hide_pelt_huge", "lr_item_id": "AG042", "stack_size": Decimal("8")},
    }

    applied = 0
    skipped_manual = 0
    skipped_missing_parent = 0
    targets = 0
    bear_applied = 0
    bear_skipped_manual = 0
    bear_skipped_missing_large = 0

    for size, cfg in tier_config.items():
        canonical_id = cfg["canonical_id"]
        lr_item_id = cfg["lr_item_id"]
        stack_size = cfg["stack_size"]

        cur.execute("SELECT 1 FROM canonical_items WHERE id = %s", (canonical_id,))
        if cur.fetchone() is None:
            print(f"[WARN] Rule 6q — target canonical '{canonical_id}' not found, skipping.")
            skipped_missing_parent += 1
            continue

        targets += 1

        cur.execute(
            "SELECT note FROM price_overrides WHERE canonical_id = %s",
            (canonical_id,),
        )
        existing = cur.fetchone()
        if existing is not None and not _is_computed_note(existing[0]):
            skipped_manual += 1
            continue

        cur.execute(
            """
            SELECT item_id, display_name, unit_price_current
            FROM lr_items
            WHERE item_id = %s
               OR (
                    display_name ILIKE %s
                AND (display_name ILIKE '%%hide%%' OR display_name ILIKE '%%pelt%%')
               )
            ORDER BY CASE WHEN item_id = %s THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (lr_item_id, f"%{size}%", lr_item_id),
        )
        lr_row = cur.fetchone()
        if lr_row is None or lr_row[2] is None:
            skipped_missing_parent += 1
            print(
                f"[WARN] Rule 6q — missing prepared hide LR price for size '{size}' "
                f"(expected item_id={lr_item_id}) required by '{canonical_id}', skipping."
            )
            continue

        prepared_hide_lr = Decimal(str(lr_row[2]))
        pelt_price = (prepared_hide_lr * Decimal("0.8")).quantize(Decimal("0.000001"))
        note = f"computed:hide_pelt_{size} = 0.8 × prepared_{size}_hide_lr"

        if _upsert_computed(cur, canonical_id, pelt_price, note):
            applied += 1

    # Bear-specific pelt variants map to large tier pelt pricing.
    cur.execute(
        "SELECT unit_price FROM price_overrides WHERE canonical_id = %s",
        ("hide_pelt_large",),
    )
    large_tier_row = cur.fetchone()
    bear_variant_ids = [
        "hide_pelt_bear_sun_body",
        "hide_pelt_bear_panda_body",
        "hide_pelt_bear_brown_body",
        "hide_pelt_bear_black_body",
        "hide_pelt_bear_polar_body",
    ]

    if large_tier_row is None or large_tier_row[0] is None:
        bear_skipped_missing_large = len(bear_variant_ids)
        print(
            "[WARN] Rule 6q — missing 'hide_pelt_large' override price; "
            "skipping bear large-tier mappings."
        )
    else:
        large_tier_price = Decimal(str(large_tier_row[0]))
        for bear_id in bear_variant_ids:
            cur.execute(
                "SELECT note FROM price_overrides WHERE canonical_id = %s",
                (bear_id,),
            )
            existing = cur.fetchone()
            if existing is not None and not _is_computed_note(existing[0]):
                bear_skipped_manual += 1
                continue

            variant = bear_id.removeprefix("hide_pelt_bear_").removesuffix("_body")
            note = (
                f"computed:hide_pelt_bear_{variant}_body = "
                "hide_pelt_large price (large tier mapping)"
            )
            if _upsert_computed(cur, bear_id, large_tier_price, note):
                bear_applied += 1

    print(
        "Rule 6q (dynamic) — pelts from prepared hide LR by size tier: "
        f"targets={targets}, applied={applied}, skipped_manual={skipped_manual}, "
        f"skipped_missing_parent={skipped_missing_parent}"
    )
    print(
        "Rule 6q (dynamic) — bear pelts mapped to hide_pelt_large: "
        f"targets={len(bear_variant_ids)}, applied={bear_applied}, "
        f"skipped_manual={bear_skipped_manual}, skipped_missing_large={bear_skipped_missing_large}"
    )


def apply_crushed_rules(cur) -> None:
    """Rule 6r — Price crushed_* at parent LR / 20 (ingot first, then base material)."""
    cur.execute(
        """
        SELECT ci.id
        FROM canonical_items ci
        LEFT JOIN lr_items li ON li.id = ci.lr_item_id
        WHERE ci.id ILIKE 'crushed\\_%' ESCAPE '\\'
          AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
        ORDER BY ci.id
        """
    )
    crushed_ids = [row[0] for row in cur.fetchall() if row and row[0]]

    applied = 0
    skipped_manual = 0
    skipped_missing_parent = 0

    for crushed_id in crushed_ids:
        material = crushed_id.split("crushed_", 1)[1].strip().lower()
        if not material:
            skipped_missing_parent += 1
            print(f"[WARN] Rule 6r — unable to parse material from '{crushed_id}', skipping.")
            continue

        cur.execute(
            "SELECT note FROM price_overrides WHERE canonical_id = %s",
            (crushed_id,),
        )
        existing = cur.fetchone()
        if existing is not None and not _is_computed_note(existing[0]):
            skipped_manual += 1
            continue

        parent_id = f"ingot_{material}"
        parent_price = get_lr_price(cur, parent_id)
        if parent_price is None:
            parent_id = material
            parent_price = get_lr_price(cur, parent_id)

        if parent_price is None:
            skipped_missing_parent += 1
            print(
                f"[WARN] Rule 6r — missing LR price for parent 'ingot_{material}'/'{material}' "
                f"required by '{crushed_id}', skipping."
            )
            continue

        crushed_price = (parent_price / Decimal("20")).quantize(Decimal("0.000001"))
        note = f"computed:crushed_{material} = lr_price / 20 (1 nugget equivalent)"
        if _upsert_computed(cur, crushed_id, crushed_price, note):
            applied += 1

    print(
        "Rule 6r (dynamic) — crushed materials from LR parent / 20: "
        f"targets={len(crushed_ids)}, applied={applied}, skipped_manual={skipped_manual}, "
        f"skipped_missing_parent={skipped_missing_parent}"
    )


def apply_powdered_rules(cur) -> None:
    """Rule 6s — Price powdered_* at parent LR / 40 (ingot first, then base material)."""
    cur.execute(
        """
        SELECT ci.id
        FROM canonical_items ci
        LEFT JOIN lr_items li ON li.id = ci.lr_item_id
        WHERE ci.id ILIKE 'powdered\\_%' ESCAPE '\\'
          AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
        ORDER BY ci.id
        """
    )
    powdered_ids = [row[0] for row in cur.fetchall() if row and row[0]]

    applied = 0
    skipped_manual = 0
    skipped_missing_parent = 0

    for powdered_id in powdered_ids:
        material = powdered_id.split("powdered_", 1)[1].strip().lower()
        if not material:
            skipped_missing_parent += 1
            print(f"[WARN] Rule 6s — unable to parse material from '{powdered_id}', skipping.")
            continue

        parent_material_candidates = []

        def _add_parent_material(candidate: str) -> None:
            normalized = (candidate or "").strip().lower()
            if normalized and normalized not in parent_material_candidates:
                parent_material_candidates.append(normalized)

        _add_parent_material(material)

        stripped_material = material
        while True:
            next_material = stripped_material
            if next_material.startswith("metal_"):
                next_material = next_material[len("metal_") :]
            elif next_material.startswith("ore_"):
                next_material = next_material[len("ore_") :]

            if next_material == stripped_material:
                break

            stripped_material = next_material
            _add_parent_material(stripped_material)

        cur.execute(
            "SELECT note FROM price_overrides WHERE canonical_id = %s",
            (powdered_id,),
        )
        existing = cur.fetchone()
        if existing is not None and not _is_computed_note(existing[0]):
            skipped_manual += 1
            continue

        parent_id = None
        parent_price = None
        attempted_parents = []

        for parent_material in parent_material_candidates:
            for candidate_parent_id in (f"ingot_{parent_material}", parent_material):
                attempted_parents.append(candidate_parent_id)
                candidate_parent_price = get_lr_price(cur, candidate_parent_id)
                if candidate_parent_price is not None:
                    parent_id = candidate_parent_id
                    parent_price = candidate_parent_price
                    break
            if parent_price is not None:
                break

        if parent_price is None:
            skipped_missing_parent += 1
            print(
                f"[WARN] Rule 6s — missing LR price for parent candidates {attempted_parents} "
                f"required by '{powdered_id}', skipping."
            )
            continue

        powdered_price = (parent_price / Decimal("40")).quantize(Decimal("0.000001"))
        note = f"computed:{powdered_id} = {parent_id}_lr_price / 40 (0.5 crushed equivalent)"
        if _upsert_computed(cur, powdered_id, powdered_price, note):
            applied += 1

    print(
        "Rule 6s (dynamic) — powdered materials from LR parent / 40: "
        f"targets={len(powdered_ids)}, applied={applied}, skipped_manual={skipped_manual}, "
        f"skipped_missing_parent={skipped_missing_parent}"
    )


def main() -> int:
    print("[MEMORY BANK: ACTIVE]")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    # Shared filter: primitive gap candidates only (no direct LR price and no recipe output).
    primitive_gap_filter = """
        AND NOT (
            ci.lr_item_id IS NOT NULL
            AND COALESCE(ci.match_tier, '') != 'unmatched'
            AND li.unit_price_current IS NOT NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM recipes r
            WHERE r.output_canonical_id = ci.id
        )
    """

    # Shared filter for Rules 1-6: preserve manual overrides.
    non_manual_override_filter = """
        AND ci.id NOT IN (
            SELECT canonical_id
            FROM price_overrides
            WHERE note NOT ILIKE 'computed:%'
        )
    """

    primitive_harvest_price = Decimal("1") / Decimal("64")

    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                ensure_price_overrides_table(cur)

                cur.execute(
                    """
                    SELECT display_name, unit_price_current
                    FROM lr_items
                    WHERE item_id = 'IN045'
                    ORDER BY id
                    LIMIT 1
                    """
                )
                slab_row = cur.fetchone()
                if slab_row is None:
                    print("[WARN] No LR slab row found for item_id='IN045'.", file=sys.stderr)
                    return 1

                slab_name, slab_unit_price = slab_row
                if slab_unit_price is None:
                    print("[WARN] LR slab row IN045 has NULL unit_price_current.", file=sys.stderr)
                    return 1

                slab_unit_price_dec = Decimal(str(slab_unit_price))
                rock_price = slab_unit_price_dec / Decimal("216")
                stone_price = rock_price / Decimal("10")

                # Dynamic prices derived from other items
                cattail_price = get_price(cur, "cattailtops") or Decimal("0.015625")
                papyrus_price = get_price(cur, "papyrustops") or cattail_price
                parchment_price = (min(cattail_price, papyrus_price) * Decimal("2")).quantize(
                    Decimal("0.000001")
                )

                # Clay price: cheapest non-zero clay variant
                cur.execute(
                    """
                    SELECT MIN(COALESCE(li.unit_price_current, po.unit_price))
                    FROM canonical_items ci
                    LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                    LEFT JOIN price_overrides po ON po.canonical_id = ci.id
                    WHERE ci.id ILIKE '%clay%'
                      AND ci.id NOT ILIKE '%clayforming%'
                      AND ci.id NOT ILIKE '%claystone%'
                      AND COALESCE(li.unit_price_current, po.unit_price) > 0
                    """
                )
                clay_row = cur.fetchone()
                clay_price = Decimal(str(clay_row[0])) if clay_row and clay_row[0] else Decimal("0.5")
                bowl_price = clay_price  # 1 clay per bowl

                # Log price (Logs LR item)
                log_price = get_price(cur, "log_placed_oak_ud") or Decimal("1")
                debarkedlog_price = (log_price / Decimal("4")).quantize(Decimal("0.000001"))
                supportbeam_price = debarkedlog_price
                bone_base_price = get_price(cur, "bone") or (Decimal("1") / Decimal("64"))

                print(
                    "[INFO] Slab source: "
                    f"display_name='{slab_name}', "
                    f"unit_price_current={slab_unit_price_dec}, "
                    f"computed_rock_price={rock_price}, "
                    f"computed_stone_price={stone_price}"
                )
                print(f"[INFO] Dynamic prices: cattail={cattail_price}, parchment={parchment_price}")
                print(f"[INFO] Dynamic prices: clay={clay_price}, bowl={bowl_price}")
                print(f"[INFO] Dynamic prices: log={log_price}, debarkedlog={debarkedlog_price}")
                print(
                    f"[INFO] Dynamic prices: supportbeam={supportbeam_price}, "
                    f"bone_base={bone_base_price}"
                )

                rules = [
                    Rule(
                        name="Rule 1 — Rock from LR slab price",
                        target_query=f"""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.game_code ILIKE '%rock%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              {non_manual_override_filter}
                            ORDER BY ci.id
                        """,
                        unit_price=rock_price,
                        note="computed: rock = LR_slab_price / 216",
                    ),
                    Rule(
                        name="Rule 2 — Stone from rock",
                        target_query=f"""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.game_code ILIKE '%stone%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              {non_manual_override_filter}
                            ORDER BY ci.id
                        """,
                        unit_price=stone_price,
                        note="computed: stone = rock_price / 10",
                    ),
                    Rule(
                        name="Rule 3 — Sand and soil from stone",
                        target_query=f"""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE (ci.game_code ILIKE '%sand%' OR ci.game_code ILIKE '%soil%')
                              AND ci.id NOT ILIKE 'metalnailsandstrips%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              {non_manual_override_filter}
                            ORDER BY ci.id
                        """,
                        unit_price=stone_price,
                        note="computed: sand/soil = stone_price",
                    ),
                    Rule(
                        name="Rule 4 — Flint from stone",
                        target_query=f"""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.game_code ILIKE '%flint%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              {non_manual_override_filter}
                            ORDER BY ci.id
                        """,
                        unit_price=stone_price,
                        note="computed: flint = stone_price",
                    ),
                    Rule(
                        name="Rule 5 — Drygrass (primitive harvest)",
                        target_query=f"""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.id ILIKE '%drygrass%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              {non_manual_override_filter}
                            ORDER BY ci.id
                        """,
                        unit_price=primitive_harvest_price,
                        note="computed: drygrass = 1/64 primitive harvest",
                    ),
                    Rule(
                        name="Rule 6 — Brineportion (primitive harvest)",
                        target_query=f"""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.id ILIKE '%brineportion%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              {non_manual_override_filter}
                            ORDER BY ci.id
                        """,
                        unit_price=primitive_harvest_price,
                        note="computed: brineportion = 1/64 primitive harvest",
                    ),
                    Rule(
                        name="Rule 6b — Salt (= stone price, rock family)",
                        target_query="""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.game_code ILIKE '%salt%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (
                                  SELECT canonical_id FROM price_overrides
                                  WHERE note NOT ILIKE 'computed:%'
                              )
                            ORDER BY ci.id
                        """,
                        unit_price=stone_price,
                        note="computed: salt = stone_price (rock family)",
                    ),
                    Rule(
                        name="Rule 6c — Needle (primitive, 1/64 CS)",
                        target_query="""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.game_code ILIKE '%needle%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (
                                  SELECT canonical_id FROM price_overrides
                                  WHERE note NOT ILIKE 'computed:%'
                              )
                            ORDER BY ci.id
                        """,
                        unit_price=Decimal("1") / Decimal("64"),
                        note="computed: needle = 1/64 primitive",
                    ),
                    Rule(
                        name="Rule 6e — Parchment (2x cheapest cattail/papyrus)",
                        target_query="""
                            SELECT ci.id FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE (ci.game_code ILIKE '%parchment%' OR ci.id ILIKE '%parchment%')
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (SELECT canonical_id FROM price_overrides WHERE note NOT ILIKE 'computed:%')
                            ORDER BY ci.id
                        """,
                        unit_price=parchment_price,
                        note="computed: parchment = 2 x min(cattail, papyrus) price",
                    ),
                    Rule(
                        name="Rule 6f — Bowl fired (1x clay)",
                        target_query="""
                            SELECT ci.id FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE (ci.game_code ILIKE '%bowl%fired%' OR ci.game_code ILIKE '%bowl%raw%')
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (SELECT canonical_id FROM price_overrides WHERE note NOT ILIKE 'computed:%')
                            ORDER BY ci.id
                        """,
                        unit_price=bowl_price,
                        note="computed: bowl_fired = 1 x clay_price",
                    ),
                    Rule(
                        name="Rule 6g — Debarked log (1/4 log price)",
                        target_query="""
                            SELECT ci.id FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.game_code ILIKE '%debarkedlog%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (SELECT canonical_id FROM price_overrides WHERE note NOT ILIKE 'computed:%')
                            ORDER BY ci.id
                        """,
                        unit_price=debarkedlog_price,
                        note="computed: debarkedlog = log_price / 4",
                    ),
                    Rule(
                        name="Rule 6p — gear_rusty (foraged primitive, flat)",
                        target_query="""
                            SELECT ci.id
                            FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.id = 'gear_rusty'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (
                                  SELECT canonical_id FROM price_overrides
                                  WHERE note NOT ILIKE 'computed:%'
                              )
                            ORDER BY ci.id
                        """,
                        unit_price=Decimal("5.0"),
                        note="computed:gear_rusty — foraged primitive, 5 CS flat",
                    ),
                    Rule(
                        name="Rule 6l — Driftwood/flotsam (foraged primitive = stick price)",
                        target_query="""
                            SELECT ci.id FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE (ci.id ILIKE '%flotsam%' OR ci.id ILIKE '%driftwood%')
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (SELECT canonical_id FROM price_overrides WHERE note NOT ILIKE 'computed:%')
                            ORDER BY ci.id
                        """,
                        unit_price=Decimal("0.03125"),
                        note="computed: driftwood = foraged primitive",
                    ),
                    Rule(
                        name="Rule 6m — Slush (zero-price world resource)",
                        target_query="""
                            SELECT ci.id FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.id ILIKE '%slush%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (SELECT canonical_id FROM price_overrides WHERE note NOT ILIKE 'computed:%')
                            ORDER BY ci.id
                        """,
                        unit_price=Decimal("0"),
                        note="computed: slush = zero-price world resource",
                    ),
                    Rule(
                        name="Rule 6n — Support beams (component wood cost)",
                        target_query="""
                            SELECT ci.id FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.id ILIKE 'supportbeam%'
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (SELECT canonical_id FROM price_overrides WHERE note NOT ILIKE 'computed:%')
                            ORDER BY ci.id
                        """,
                        unit_price=supportbeam_price,
                        note="computed: supportbeam = component wood cost",
                    ),
                    Rule(
                        name="Rule 6o — Primitive bones fallback",
                        target_query="""
                            SELECT ci.id FROM canonical_items ci
                            LEFT JOIN lr_items li ON li.id = ci.lr_item_id
                            WHERE ci.id IN ('bone', 'bone_tiny', 'strongbone', 'burnedbone')
                              AND (ci.lr_item_id IS NULL OR li.unit_price_current IS NULL)
                              AND ci.id NOT IN (SELECT canonical_id FROM price_overrides WHERE note NOT ILIKE 'computed:%')
                            ORDER BY ci.id
                        """,
                        unit_price=bone_base_price,
                        note="computed: primitive bones = base bone price or 1/64 CS fallback",
                    ),
                ]

                print("\nPrimitive computed override run summary")
                print("-" * 72)

                apply_metal_nails_and_strips_rules(cur)
                apply_dynamic_anvil_rules(cur)
                apply_pelt_rules(cur)
                apply_crushed_rules(cur)
                apply_powdered_rules(cur)

                for rule in rules:
                    inserted, updated, skipped_manual, targets = apply_rule(cur, rule)
                    print(
                        f"{rule.name}: targets={targets}, inserted={inserted}, "
                        f"updated={updated}, skipped_manual={skipped_manual}"
                    )

            conn.commit()

        return 0
    except Exception as exc:  # pragma: no cover - operational script
        print(f"[ERROR] compute_primitive_prices failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
