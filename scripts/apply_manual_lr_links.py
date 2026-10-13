#!/usr/bin/env python3
"""Apply targeted manual LR links for known canonical item misses.

This script intentionally patches canonical_items directly without re-running the
full ingestion/build pipeline.
"""

from __future__ import annotations

import os
import re
import sys
import traceback
import json
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2
from dotenv import load_dotenv


load_dotenv()


NORMALIZE_LINK_RE = re.compile(r"[^a-z0-9]+")
VALID_MATCH_TIERS = ("exact", "high", "low", "unmatched", "manual", "mapped")


def normalize_for_exact_link(value: Optional[str]) -> str:
    """Normalize names for strict exact-only linking (no fuzzy behavior)."""
    text = (value or "").lower().strip()
    return NORMALIZE_LINK_RE.sub("", text)


BASE_MANUAL_LINKS: List[Dict[str, Optional[str]]] = [
    # game_code_pattern, lr_display, match mode
    # --- Hides / leather ---
    {"pattern": "item:leather-normal-%", "lr_item_id": None, "lr_display": "Leather", "match": "like"},
    {"pattern": "item:hide-prepared-small", "lr_item_id": None, "lr_display": "Prepared Small Hide", "match": "exact"},
    {"pattern": "item:hide-prepared-medium", "lr_item_id": None, "lr_display": "Prepared Medium Hide", "match": "exact"},
    {"pattern": "item:hide-prepared-large", "lr_item_id": None, "lr_display": "Prepared Large Hide", "match": "exact"},
    {"pattern": "item:hide-prepared-huge", "lr_item_id": None, "lr_display": "Prepared Huge Hide", "match": "exact"},
    {"pattern": "item:leatherbundle-%", "lr_item_id": None, "lr_display": "Leather", "match": "like"},
    # --- Wood ---
    {"pattern": "block:log-%", "lr_item_id": None, "lr_display": "Logs", "match": "like"},
    {"pattern": "block:log-placed-%", "lr_item_id": None, "lr_display": "Logs", "match": "like"},
    {"pattern": "item:plank-%", "lr_item_id": None, "lr_display": "Planks", "match": "like"},
    {"pattern": "item:firewood%", "lr_item_id": None, "lr_display": "Firewood", "match": "like"},
    # --- Textile / tailoring ---
    # --- Candle ---
    {"pattern": "item:candle", "lr_item_id": None, "lr_display": "Regular Candles", "match": "exact"},
    {"pattern": "item:candle-%", "lr_item_id": None, "lr_display": "Regular Candles", "match": "like"},

    # --- Glass ---
    {"pattern": "block:glassslab-%", "lr_item_id": None, "lr_display": "Glass", "match": "like"},

    # --- Linen block orientation variants ---
    {"pattern": "block:linen-%", "lr_item_id": None, "lr_display": "Linen / Cloth", "match": "like"},

    {"pattern": "item:twine-%", "lr_item_id": None, "lr_display": "Twine (Processed, not flax fibers) (NOT BUYING)", "match": "like"},
    {"pattern": "item:linen-%", "lr_item_id": None, "lr_display": "Linen / Cloth", "match": "like"},
    {"pattern": "item:cloth-%", "lr_item_id": None, "lr_display": "Linen / Cloth", "match": "like"},
    # --- Mushrooms ---
    {"pattern": "item:mushroom-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "block:mushroom-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "item:cookedmushroom-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "item:choppedmushroom-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "item:cookedchoppedmushroom-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "block:mushroombasket-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "item:sporeprint-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "item:acornbreadedmushroom-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    {"pattern": "item:breadedmushroom-%", "lr_item_id": None, "lr_display": "Mushrooms", "match": "like"},
    # --- Additional LR-backed blockers ---
    {"pattern": "item:pipeleaf", "lr_item_id": None, "lr_display": "Pipeleaf", "match": "exact"},
    {"pattern": "item:smokable-pipeleaf-%", "lr_item_id": None, "lr_display": "Pipeleaf", "match": "like"},
    {"pattern": "item:saltpeter", "lr_item_id": None, "lr_display": "Saltpeter", "match": "exact"},
    {"pattern": "item:smellingsalts-%", "lr_item_id": None, "lr_display": "Smelling Salt", "match": "like"},
    # --- Common crafting intermediates ---
    {"pattern": "item:sewingkit", "lr_item_id": None, "lr_display": "Sewing Kit", "match": "exact"},
    {"pattern": "item:armor-tailoring-kit", "lr_item_id": None, "lr_display": "Armor Tailoring Kit", "match": "exact"},
    {"pattern": "item:lantern-%", "lr_item_id": None, "lr_display": "Lanterns/Chandeleries/Torch Holders", "match": "like"},
    {"pattern": "item:glass-%", "lr_item_id": None, "lr_display": "Glass", "match": "like"},
    # --- Existing high-value fixes ---
    {"pattern": "item:ore-chromite", "lr_item_id": None, "lr_display": "Chromite Chunk", "match": "exact"},
    {"pattern": "item:nugget-chromite", "lr_item_id": None, "lr_display": "Chromite Chunk", "match": "exact"},
    {"pattern": "item:waterportion", "lr_item_id": None, "lr_display": "Water", "match": "exact"},
    {"pattern": "item:powdered-metal-lead", "lr_item_id": None, "lr_display": "Lead", "match": "exact"},
    {"pattern": "item:crushed-chromite", "lr_item_id": None, "lr_display": "Chromite Chunk", "match": "exact"},
    {"pattern": "item:powdered-ore-chromite", "lr_item_id": None, "lr_display": "Chromite Chunk", "match": "exact"},
    {"pattern": "item:powder-lead%", "lr_item_id": None, "lr_display": "Lead", "match": "like"},
]


METAL_LR_NAMES = {
    "bismuth": "Bismuth",
    "blackbronze": "Black Bronze",
    "brass": "Brass",
    "copper": "Copper",
    "cupronickel": "Cupronickel",
    "electrum": "Electrum",
    "gold": "Gold",
    "iron": "Iron",
    "lead": "Lead",
    "meteoriciron": "Meteoric Iron",
    "nickel": "Nickel",
    "silver": "Silver",
    "steel": "Steel",
    "tin": "Tin",
    "tinbronze": "Tin Bronze",
    "zinc": "Zinc",
}


ORE_TO_CHUNK_LR_NAMES = {
    "chromite": "Chromite Chunk",
    "cassiterite": "Cassiterite Chunk",
    "galena": "Galena Chunk",
    "hematite": "Hematite Chunk",
    "limonite": "Limonite Chunk",
    "magnetite": "Magnetite Chunk",
    "malachite": "Malachite Chunk",
    "bismuthinite": "Bismuthinite Chunk",
    "sphalerite": "Sphalerite Chunk",
    "ilmenite": "Ilmenite Chunk",
    "pentlandite": "Pentlandite Chunk",
    "rhodochrosite": "Rhodochrosite Chunk",
    "gold": "Gold Chunk",
    "silver": "Silver Chunk",
}


# Fallback used when no ore-specific crush recipe ratio can be found in current DB.
# VS quern behavior is typically ~5-6 crushed output per ore chunk.
DEFAULT_ORE_CRUSH_OUTPUT_PER_CHUNK = 5.0


def build_material_rules() -> List[Dict[str, Optional[str]]]:
    rules: List[Dict[str, Optional[str]]] = []

    # Broad metal-derivative linkage so powdered/nugget/ingot variants resolve
    # against LR ingot rows and inherit settlement-aware pricing.
    for key, lr_name in METAL_LR_NAMES.items():
        rules.extend(
            [
                {
                    "pattern": f"item:powdered-metal-{key}",
                    "lr_item_id": None,
                    "lr_display": lr_name,
                    "match": "exact",
                },
                {
                    "pattern": f"item:powder-{key}%",
                    "lr_item_id": None,
                    "lr_display": lr_name,
                    "match": "like",
                },
            ]
        )

    # Ore derivative forms (ore / crushed / powdered ore) -> LR chunk pricing.
    for ore_key, lr_name in ORE_TO_CHUNK_LR_NAMES.items():
        rules.extend(
            [
                {
                    "pattern": f"item:ore-{ore_key}",
                    "lr_item_id": None,
                    "lr_display": lr_name,
                    "match": "exact",
                },
                {
                    "pattern": f"item:crushed-ore-{ore_key}",
                    "lr_item_id": None,
                    "lr_display": lr_name,
                    "match": "exact",
                },
                {
                    "pattern": f"item:powdered-ore-{ore_key}",
                    "lr_item_id": None,
                    "lr_display": lr_name,
                    "match": "exact",
                },
            ]
        )

    return rules


MANUAL_LINKS: List[Dict[str, Optional[str]]] = BASE_MANUAL_LINKS + build_material_rules()


# 5 metal units per nugget, 100 units per ingot in VS => 1 nugget = 1/20 ingot.
NUGGET_TO_INGOT_RATIO = 20

# Ore nuggets from survival/itemtypes/resource/nugget.json -> smeltedStack mapping.
ORE_NUGGET_TO_LR_INGOT = {
    "nativecopper": "Copper",
    "malachite": "Copper",
    "limonite": "Iron",
    "hematite": "Iron",
    "magnetite": "Iron",
    "nativegold": "Gold",
    "galena": "Lead",
    "cassiterite": "Tin",
    "sphalerite": "Zinc",
    "nativesilver": "Silver",
    "bismuthinite": "Bismuth",
    "pentlandite": "Nickel",
}

# Refined/metal nugget variants (modded and base-game where present).
METAL_NUGGET_TO_LR_INGOT = {
    "copper": "Copper",
    "tin": "Tin",
    "lead": "Lead",
    "zinc": "Zinc",
    "silver": "Silver",
    "gold": "Gold",
    "iron": "Iron",
    "nickel": "Nickel",
    "bismuth": "Bismuth",
    "steel": "Steel",
    "meteoriciron": "Meteoric Iron",
    "brass": "Brass",
    "cupronickel": "Cupronickel",
    "electrum": "Electrum",
    "blackbronze": "Black Bronze",
    "tinbronze": "Tin Bronze",
}


EXPLICIT_CANONICAL_LR_LINKS = {
    # Keep copper routed to ingot canonical even when similarly named chain rows exist.
    "ingot_copper": "Copper",
}

# Chain outputs that should not carry direct LR market links in this project policy.
# Keep limited and explicit to avoid broad destructive updates.
FORCED_CHAIN_UNLINK_IDS = [
    "chain_copper",
    "chain_silver",
    "chain_tinbronze",
    "metalchain_bismuthbronze",
    "metalchain_blackbronze",
    "metalchain_meteoriciron",
]


def _humanize_metal_key(metal_key: str) -> str:
    """Return display-friendly metal name from normalized key."""
    if not metal_key:
        return "Unknown"
    if metal_key in METAL_LR_NAMES:
        return METAL_LR_NAMES[metal_key]
    return metal_key.replace("_", " ").replace("-", " ").title()


def discover_lang_files() -> List[str]:
    """Return candidate English lang files from base game + unpacked mods."""
    paths: List[str] = []

    vs_install_dir = os.getenv("VINTAGE_STORY_INSTALL_DIR", "C:/Games/Vintagestory")
    base_en = os.path.join(vs_install_dir, "assets", "game", "lang", "en.json")
    if os.path.exists(base_en):
        paths.append(base_en)

    unpack_root = os.path.join("Cache", "unpack")
    if os.path.isdir(unpack_root):
        for root, _dirs, files in os.walk(unpack_root):
            if "en.json" not in files:
                continue

            normalized_root = root.replace("\\", "/").lower()
            if "/lang" not in normalized_root:
                continue

            paths.append(os.path.join(root, "en.json"))

    return paths


def load_lang_alias_map() -> Dict[str, str]:
    """Build game_code -> display_name map from lang/en.json files."""
    alias_map: Dict[str, str] = {}

    for path in discover_lang_files():
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue

            key = key.strip().lower()
            label = value.strip()

            if not label or "{" in label:
                continue

            if key.startswith("item-"):
                tail = key[len("item-") :]
                if not tail or "*" in tail:
                    continue
                alias_map.setdefault(f"item:{tail}", label)
            elif key.startswith("block-"):
                tail = key[len("block-") :]
                if not tail or "*" in tail:
                    continue
                alias_map.setdefault(f"block:{tail}", label)

    return alias_map


def fix_chain_ingot_suffix_matches(cur) -> int:
    """
    Remove incorrect ingot-price inheritance on chain variants.

    Safety guards (must all match):
      - canonical game_code is chain-like
      - canonical match_tier is exact (auto-linked by suffix matcher)
      - linked LR row is an ingot (`lr_sub_category='Metal Ingots'`)
    """
    cur.execute(
        """
        SELECT c.id, c.game_code, c.lr_item_id, l.display_name, l.lr_sub_category
        FROM canonical_items c
        JOIN lr_items l ON l.id = c.lr_item_id
        WHERE c.match_tier = 'exact'
          AND l.lr_sub_category = 'Metal Ingots'
          AND (
                lower(c.game_code) LIKE 'item:chain-%'
             OR lower(c.game_code) LIKE 'item:metalchain-%'
          )
        ORDER BY c.id
        """
    )
    rows = cur.fetchall()

    updated = 0
    for canonical_id, game_code, _lr_item_id, _lr_display_name, _lr_sub_category in rows:
        tail = (game_code or "").split(":")[-1]
        if tail.startswith("chain-"):
            metal_key = tail[len("chain-") :]
        elif tail.startswith("metalchain-"):
            metal_key = tail[len("metalchain-") :]
        else:
            metal_key = tail

        new_display_name = f"{_humanize_metal_key(metal_key)} Chain"

        cur.execute(
            """
            UPDATE canonical_items
            SET display_name = %s,
                lr_item_id = NULL,
                match_tier = 'unmatched'
            WHERE id = %s
            """,
            (new_display_name, canonical_id),
        )
        updated += cur.rowcount
        print(
            f"Chain ingot-fix: {canonical_id} ({game_code}) -> "
            f"display_name='{new_display_name}', lr_item_id=NULL"
        )

    print(f"Chain ingot-suffix cleanup complete. Rows updated: {updated}")
    return updated


def fix_metalbit_ingot_suffix_matches(cur) -> int:
    """
    Remove incorrect ingot-price inheritance on metalbit variants.

    Safety guards (must all match):
      - canonical game_code is item:metalbit-*
      - canonical match_tier is exact (auto/suffix linked)
      - linked LR row is an ingot (`lr_sub_category='Metal Ingots'`)
    """
    cur.execute(
        """
        UPDATE canonical_items c
        SET lr_item_id = NULL,
            match_tier = 'unmatched'
        FROM lr_items l
        WHERE l.id = c.lr_item_id
          AND c.match_tier = 'exact'
          AND l.lr_sub_category = 'Metal Ingots'
          AND lower(c.game_code) LIKE 'item:metalbit-%'
        """
    )
    updated = cur.rowcount
    print(f"Metalbit ingot-suffix cleanup complete. Rows updated: {updated}")
    return updated


def apply_explicit_canonical_lr_links(cur) -> int:
    """Apply deterministic canonical_id -> LR display name links."""
    updates = 0
    for canonical_id, lr_display_name in EXPLICIT_CANONICAL_LR_LINKS.items():
        lr_item_id = lookup_lr_item_id(cur, lr_display_name)
        if lr_item_id is None:
            print(
                f"[WARN] Explicit canonical link skipped: lr_items.display_name="
                f"'{lr_display_name}' not found"
            )
            continue

        cur.execute(
            """
            UPDATE canonical_items
            SET lr_item_id = %s,
                match_tier = 'manual'
            WHERE id = %s
            """,
            (lr_item_id, canonical_id),
        )
        updates += cur.rowcount
        if cur.rowcount:
            print(
                f"Explicit canonical link: {canonical_id} -> {lr_display_name} "
                f"(lr_items.id={lr_item_id})"
            )

    print(f"Explicit canonical-link pass complete. Rows updated: {updates}")
    return updates


def enforce_forced_chain_unlinks(cur) -> int:
    """
    Enforce chain canonical unlink policy for known suffix-derived edge cases.

    This is intentionally explicit (ID allowlist), not broad pattern-based.
    """
    cur.execute(
        """
        UPDATE canonical_items
        SET lr_item_id = NULL,
            match_tier = 'unmatched'
        WHERE id = ANY(%s)
          AND (
                lower(game_code) LIKE 'item:chain-%%'
             OR lower(game_code) LIKE 'item:metalchain-%%'
          )
          AND lr_item_id IS NOT NULL
        """,
        (FORCED_CHAIN_UNLINK_IDS,),
    )
    updated = cur.rowcount
    if updated:
        print(f"Forced chain unlink policy applied. Rows updated: {updated}")
    else:
        print("Forced chain unlink policy applied. Rows updated: 0")
    return updated


def enforce_primary_metal_aliases(cur) -> int:
    """
    Ensure ambiguous base-metal alias routes to ingot canonical.

    Current targeted policy:
      - alias 'copper' should resolve to canonical_id 'ingot_copper'
    """
    updates = 0

    # Remove conflicting exact alias on non-target canonicals.
    cur.execute(
        """
        DELETE FROM item_aliases
        WHERE alias = %s
          AND canonical_id <> %s
        """,
        ("copper", "ingot_copper"),
    )
    deleted = cur.rowcount
    updates += deleted
    print(f"Primary alias cleanup: removed {deleted} conflicting 'copper' aliases")

    # Ensure target alias exists.
    cur.execute(
        """
        INSERT INTO item_aliases (canonical_id, alias)
        VALUES (%s, %s)
        ON CONFLICT (canonical_id, alias) DO NOTHING
        """,
        ("ingot_copper", "copper"),
    )
    inserted = cur.rowcount
    updates += inserted
    print(f"Primary alias ensure: ingot_copper <- 'copper' inserted={inserted}")

    return updates


def ensure_manual_match_tier_allowed(cur) -> None:
    cur.execute(
        """
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'canonical_items'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%match_tier%'
        """
    )
    rows = cur.fetchall()

    expected_tokens = {f"'{tier}'" for tier in VALID_MATCH_TIERS}
    has_expected = False
    for _, definition in rows:
        definition_lower = (definition or "").lower()
        if all(token in definition_lower for token in expected_tokens):
            has_expected = True
            break

    if has_expected:
        print("match_tier CHECK already allows required tiers.")
        return

    for conname, _ in rows:
        cur.execute(f'ALTER TABLE canonical_items DROP CONSTRAINT IF EXISTS "{conname}"')

    allowed = ", ".join(f"'{tier}'" for tier in VALID_MATCH_TIERS)
    cur.execute(
        f"""
        ALTER TABLE canonical_items
        ADD CONSTRAINT canonical_items_match_tier_check
        CHECK (match_tier IN ({allowed}))
        """
    )
    print("Updated match_tier CHECK to include required tiers.")


def lookup_lr_item_id(cur, lr_display: str) -> Optional[int]:
    cur.execute(
        """
        SELECT id
        FROM lr_items
        WHERE display_name = %s
        ORDER BY id
        LIMIT 1
        """,
        (lr_display,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def ensure_price_overrides_table(cur) -> None:
    cur.execute("SELECT to_regclass('public.price_overrides')")
    row = cur.fetchone()
    if row and row[0]:
        print("price_overrides table already exists.")
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
    print("Created price_overrides table.")


def upsert_price_overrides(cur) -> None:
    # Intentionally empty: avoid synthetic placeholder pricing.
    # Derivative-material pricing should flow from LR linkage + recipe chains.
    overrides = []

    for canonical_id, unit_price, note in overrides:
        cur.execute(
            "SELECT 1 FROM canonical_items WHERE id = %s",
            (canonical_id,),
        )
        if not cur.fetchone():
            print(f"[WARN] Skipped override: canonical item '{canonical_id}' not found")
            continue

        cur.execute(
            """
            INSERT INTO price_overrides (canonical_id, unit_price, note)
            VALUES (%s, %s, %s)
            ON CONFLICT (canonical_id) DO UPDATE
            SET unit_price = EXCLUDED.unit_price,
                note = EXCLUDED.note
            """,
            (canonical_id, unit_price, note),
        )
        print(f"Override upserted: {canonical_id} -> {unit_price}")


def lookup_lr_unit_price(cur, lr_display: str) -> Optional[float]:
    cur.execute(
        """
        SELECT unit_price_current
        FROM lr_items
        WHERE display_name = %s
        ORDER BY id
        LIMIT 1
        """,
        (lr_display,),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def lookup_linked_ingot_price(cur, ingot_game_code: str) -> tuple[Optional[float], str]:
    """Resolve canonical ingot unit price from LR first, then FTA."""
    cur.execute(
        """
        SELECT l.unit_price_current,
               f.unit_price
        FROM canonical_items c
        LEFT JOIN lr_items l ON l.id = c.lr_item_id
        LEFT JOIN fta_items f ON f.id = c.fta_item_id
        WHERE lower(c.game_code) = lower(%s)
        ORDER BY c.id
        LIMIT 1
        """,
        (ingot_game_code,),
    )
    row = cur.fetchone()
    if not row:
        return None, "none"

    lr_price = float(row[0]) if row[0] is not None else None
    fta_price = float(row[1]) if row[1] is not None else None
    if lr_price is not None:
        return lr_price, "lr"
    if fta_price is not None:
        return fta_price, "fta"
    return None, "none"


def _extract_suffix_after_prefix(game_code: str, prefix: str) -> Optional[str]:
    text = (game_code or "").strip().lower()
    if not text.startswith(prefix):
        return None
    suffix = text[len(prefix) :].strip()
    if not suffix:
        return None
    if not re.fullmatch(r"[a-z0-9]+", suffix):
        return None
    return suffix


def _generate_unique_canonical_id(cur, base_slug: str) -> str:
    candidate = base_slug
    index = 2
    while True:
        cur.execute("SELECT 1 FROM canonical_items WHERE id = %s", (candidate,))
        if not cur.fetchone():
            return candidate
        candidate = f"{base_slug}_{index}"
        index += 1


def _fallback_nugget_display_name(game_code: str) -> str:
    metal_key = _extract_suffix_after_prefix((game_code or "").lower(), "item:nugget-")
    if not metal_key:
        return "Nugget"
    return f"{_humanize_metal_key(metal_key)} nugget"


def apply_metalbit_price_overrides(cur, lang_alias_map: Dict[str, str]) -> int:
    """
    Set metalbit price overrides from matching ingot prices and sync display names from lang.

    pricing: metalbit = ingot / 20
    note: auto:metalbit_from_ingot
    """
    cur.execute(
        """
        SELECT id, game_code
        FROM canonical_items
        WHERE lower(game_code) LIKE 'item:metalbit-%'
        ORDER BY id
        """
    )
    rows = cur.fetchall()

    updates = 0
    for canonical_id, game_code in rows:
        metal_key = _extract_suffix_after_prefix((game_code or "").lower(), "item:metalbit-")
        if not metal_key:
            continue

        ingot_game_code = f"item:ingot-{metal_key}"
        ingot_price, ingot_source = lookup_linked_ingot_price(cur, ingot_game_code)
        if ingot_price is None:
            continue

        bit_price = ingot_price / float(NUGGET_TO_INGOT_RATIO)
        cur.execute(
            """
            INSERT INTO price_overrides (canonical_id, unit_price, note)
            VALUES (%s, %s, %s)
            ON CONFLICT (canonical_id) DO UPDATE
            SET unit_price = EXCLUDED.unit_price,
                note = EXCLUDED.note
            """,
            (canonical_id, bit_price, "auto:metalbit_from_ingot"),
        )
        updates += 1

        lang_name = lang_alias_map.get((game_code or "").lower())
        if lang_name:
            cur.execute(
                """
                UPDATE canonical_items
                SET display_name = %s
                WHERE id = %s
                """,
                (lang_name, canonical_id),
            )

        print(
            f"Metalbit override: {canonical_id} ({game_code}) -> {bit_price:.6f} "
            f"from {ingot_game_code} ({ingot_source})"
        )

    print(f"Metalbit override pass complete. Total overrides upserted: {updates}")
    return updates


def upsert_nugget_price_overrides(cur) -> None:
    raise RuntimeError("upsert_nugget_price_overrides requires lang_alias_map; call apply_nugget_price_overrides")


def apply_nugget_price_overrides(cur, lang_alias_map: Dict[str, str]) -> int:
    """
    Apply nugget overrides for ore-derived nuggets and all ingot-backed metal nuggets.

    - Existing nuggets get override = ingot_price / 20
    - Missing ingot-metal nuggets are created in canonical_items first
    - Nugget display names are refreshed from lang map when available
    """
    total_overrides = 0

    # Refresh display names for all existing nugget canonicals from lang where possible.
    cur.execute(
        """
        SELECT id, game_code
        FROM canonical_items
        WHERE lower(game_code) LIKE 'item:nugget-%'
        ORDER BY id
        """
    )
    for canonical_id, game_code in cur.fetchall():
        lang_name = lang_alias_map.get((game_code or "").lower()) or _fallback_nugget_display_name(game_code or "")
        cur.execute(
            """
            UPDATE canonical_items
            SET display_name = %s
            WHERE id = %s
            """,
            (lang_name, canonical_id),
        )

    # Pass A: existing ore-derived and existing explicit metal nugget mappings.
    mappings: List[tuple[str, str, str]] = []
    for nugget_key, lr_ingot in ORE_NUGGET_TO_LR_INGOT.items():
        mappings.append((f"item:nugget-{nugget_key}", lr_ingot, "ore_nugget"))
    for nugget_key, lr_ingot in METAL_NUGGET_TO_LR_INGOT.items():
        mappings.append((f"item:nugget-{nugget_key}", lr_ingot, "metal_nugget"))

    for game_code, lr_ingot_name, _nugget_type in mappings:
        lr_unit_price = lookup_lr_unit_price(cur, lr_ingot_name)
        if lr_unit_price is None:
            continue

        nugget_unit_price = lr_unit_price / float(NUGGET_TO_INGOT_RATIO)
        cur.execute(
            """
            SELECT id
            FROM canonical_items
            WHERE lower(game_code) = lower(%s)
            ORDER BY id
            """,
            (game_code,),
        )
        canonical_rows = cur.fetchall()
        for (canonical_id,) in canonical_rows:
            cur.execute(
                """
                INSERT INTO price_overrides (canonical_id, unit_price, note)
                VALUES (%s, %s, %s)
                ON CONFLICT (canonical_id) DO UPDATE
                SET unit_price = EXCLUDED.unit_price,
                    note = EXCLUDED.note
                """,
                (canonical_id, nugget_unit_price, "auto:nugget_from_ingot"),
            )
            total_overrides += 1

    # Pass B: ensure all priced ingot metals have nugget canonicals + overrides.
    cur.execute(
        """
        SELECT c.game_code,
               l.unit_price_current,
               f.unit_price
        FROM canonical_items c
        LEFT JOIN lr_items l ON l.id = c.lr_item_id
        LEFT JOIN fta_items f ON f.id = c.fta_item_id
        WHERE lower(c.game_code) LIKE 'item:ingot-%'
          AND (l.unit_price_current IS NOT NULL OR f.unit_price IS NOT NULL)
        ORDER BY c.game_code, c.id
        """
    )
    ingot_rows = cur.fetchall()

    seen_metals: set[str] = set()
    for ingot_game_code, lr_price_raw, fta_price_raw in ingot_rows:
        metal_key = _extract_suffix_after_prefix((ingot_game_code or "").lower(), "item:ingot-")
        if not metal_key or metal_key in seen_metals:
            continue
        seen_metals.add(metal_key)

        ingot_price = float(lr_price_raw) if lr_price_raw is not None else None
        if ingot_price is None and fta_price_raw is not None:
            ingot_price = float(fta_price_raw)
        if ingot_price is None:
            continue

        nugget_game_code = f"item:nugget-{metal_key}"
        cur.execute(
            """
            SELECT id
            FROM canonical_items
            WHERE lower(game_code) = lower(%s)
            ORDER BY id
            LIMIT 1
            """,
            (nugget_game_code,),
        )
        row = cur.fetchone()

        canonical_id: str
        if row:
            canonical_id = row[0]
        else:
            base_slug = f"nugget_{metal_key}"
            canonical_id = _generate_unique_canonical_id(cur, base_slug)
            display_name = lang_alias_map.get(nugget_game_code, _fallback_nugget_display_name(nugget_game_code))
            cur.execute(
                """
                INSERT INTO canonical_items (
                    id,
                    display_name,
                    game_code,
                    lr_item_id,
                    fta_item_id,
                    match_tier,
                    match_score,
                    variant_family,
                    variant_material
                ) VALUES (%s, %s, %s, NULL, NULL, 'manual', NULL, 'nugget', %s)
                """,
                (canonical_id, display_name, nugget_game_code, metal_key),
            )
            print(f"Created nugget canonical: {canonical_id} ({nugget_game_code})")

        # Ensure display_name for existing/new canonical from lang when available.
        lang_name = lang_alias_map.get(nugget_game_code, _fallback_nugget_display_name(nugget_game_code))
        cur.execute(
            """
            UPDATE canonical_items
            SET display_name = %s
            WHERE id = %s
            """,
            (lang_name, canonical_id),
        )

        nugget_unit_price = ingot_price / float(NUGGET_TO_INGOT_RATIO)
        cur.execute(
            """
            INSERT INTO price_overrides (canonical_id, unit_price, note)
            VALUES (%s, %s, %s)
            ON CONFLICT (canonical_id) DO UPDATE
            SET unit_price = EXCLUDED.unit_price,
                note = EXCLUDED.note
            """,
            (canonical_id, nugget_unit_price, "auto:nugget_from_ingot"),
        )
        total_overrides += 1
        print(
            f"Nugget override upserted: {canonical_id} ({nugget_game_code}) -> "
            f"{nugget_unit_price:.6f}"
        )

    print(f"Nugget override pass complete. Total nugget overrides upserted: {total_overrides}")
    return total_overrides


def _extract_ore_key_from_game_code(game_code: Optional[str], prefix: str) -> Optional[str]:
    """Extract ore key from canonical game_code tail using a specific prefix."""
    if not game_code:
        return None
    tail = (game_code or "").lower().split(":")[-1]
    if prefix not in tail:
        return None
    ore_key = tail.split(prefix, 1)[1].strip()
    return ore_key or None


def _lookup_chunk_unit_price(cur, ore_key: str) -> tuple[Optional[float], str]:
    """
    Resolve ore-chunk unit price for an ore key.

    Preference order:
      1) canonical row linked to LR display chunk name, using LR unit_price_current
      2) same canonical row FTA unit_price
      3) fallback canonical rows containing ore+chunk hints with LR/FTA price
    """
    lr_chunk_name = ORE_TO_CHUNK_LR_NAMES.get(ore_key, f"{ore_key.title()} Chunk")

    cur.execute(
        """
        SELECT c.id, c.game_code, c.display_name,
               l.unit_price_current,
               f.unit_price
        FROM canonical_items c
        LEFT JOIN lr_items l ON l.id = c.lr_item_id
        LEFT JOIN fta_items f ON f.id = c.fta_item_id
        WHERE lower(c.display_name) = lower(%s)
           OR (l.display_name IS NOT NULL AND lower(l.display_name) = lower(%s))
        ORDER BY c.id
        LIMIT 1
        """,
        (lr_chunk_name, lr_chunk_name),
    )
    row = cur.fetchone()
    if row:
        lr_unit = float(row[3]) if row[3] is not None else None
        fta_unit = float(row[4]) if row[4] is not None else None
        if lr_unit is not None:
            return lr_unit, "lr"
        if fta_unit is not None:
            return fta_unit, "fta"

    cur.execute(
        """
        SELECT c.id, c.game_code, c.display_name,
               l.unit_price_current,
               f.unit_price
        FROM canonical_items c
        LEFT JOIN lr_items l ON l.id = c.lr_item_id
        LEFT JOIN fta_items f ON f.id = c.fta_item_id
        WHERE (
               lower(coalesce(c.game_code, '')) LIKE %s
            OR lower(coalesce(c.display_name, '')) LIKE %s
            OR lower(c.id) = lower(%s)
        )
        ORDER BY c.id
        """,
        (f"%{ore_key}%", f"%{ore_key}%chunk%", f"{ore_key}_chunk"),
    )
    for _cid, _code, _name, lr_unit_raw, fta_unit_raw in cur.fetchall():
        lr_unit = float(lr_unit_raw) if lr_unit_raw is not None else None
        fta_unit = float(fta_unit_raw) if fta_unit_raw is not None else None
        if lr_unit is not None:
            return lr_unit, "lr"
        if fta_unit is not None:
            return fta_unit, "fta"

    return None, "none"


def _lookup_crush_output_per_chunk(cur, crushed_game_code: Optional[str], ore_key: str) -> Optional[float]:
    """
    Lookup crush ratio as output units per 1 ore chunk from parsed recipe rows.

    Returns: output_qty / input_qty for the best candidate recipe row.
    """
    output_tail = None
    if crushed_game_code:
        output_tail = (crushed_game_code or "").lower().split(":")[-1]

    candidates: List[tuple] = []
    if output_tail:
        candidates.append((f"%{output_tail}", crushed_game_code or ""))
    candidates.append((f"%crushed-ore-{ore_key}", f"item:crushed-ore-{ore_key}"))
    candidates.append((f"%crushed-{ore_key}", f"item:crushed-{ore_key}"))

    for output_like, output_exact in candidates:
        cur.execute(
            """
            SELECT ri.input_game_code,
                   ri.qty AS input_qty,
                   r.output_game_code,
                   r.output_qty,
                   r.id
            FROM recipes r
            JOIN recipe_ingredients ri ON ri.recipe_id = r.id
            WHERE lower(r.output_game_code) LIKE lower(%s)
              AND lower(ri.input_game_code) LIKE lower(%s)
              AND lower(ri.input_game_code) NOT LIKE '%%nugget%%'
              AND ri.qty IS NOT NULL
              AND r.output_qty IS NOT NULL
            ORDER BY
                CASE WHEN lower(r.output_game_code) = lower(%s) THEN 0 ELSE 1 END,
                r.id
            LIMIT 1
            """,
            (output_like, f"%ore-{ore_key}%", output_exact),
        )
        row = cur.fetchone()
        if not row:
            continue

        _input_code, input_qty_raw, _output_code, output_qty_raw, _recipe_id = row
        input_qty = float(input_qty_raw)
        output_qty = float(output_qty_raw)
        if input_qty > 0 and output_qty > 0:
            return output_qty / input_qty

    return None


def apply_ore_processing_price_overrides(cur) -> int:
    """
    Auto-derive crushed/powdered ore unit prices from ore chunk prices + crush ratio.

    Pricing chain:
      ore chunk unit -> crushed unit (divide by output units per chunk)
      powdered ore unit -> same as crushed unit
    """
    updated = 0
    crushed_prices_by_ore: Dict[str, float] = {}

    cur.execute(
        """
        SELECT id, game_code
        FROM canonical_items
        WHERE lower(coalesce(game_code, '')) LIKE '%crushed-ore-%'
           OR lower(coalesce(game_code, '')) LIKE 'item:crushed-%'
        ORDER BY id
        """
    )
    crushed_rows = cur.fetchall()

    for canonical_id, game_code in crushed_rows:
        ore_key = _extract_ore_key_from_game_code(game_code, "crushed-ore-")
        if ore_key is None:
            ore_key = _extract_ore_key_from_game_code(game_code, "crushed-")
        if ore_key is None:
            continue

        chunk_unit_price, chunk_source = _lookup_chunk_unit_price(cur, ore_key)
        if chunk_unit_price is None:
            print(
                f"[WARN] Ore-chain skip crushed: {canonical_id} ({game_code}) "
                f"chunk price not found for ore='{ore_key}'"
            )
            continue

        output_per_chunk = _lookup_crush_output_per_chunk(cur, game_code, ore_key)
        used_fallback_ratio = False
        if output_per_chunk is None or output_per_chunk <= 0:
            output_per_chunk = DEFAULT_ORE_CRUSH_OUTPUT_PER_CHUNK
            used_fallback_ratio = True
            print(
                f"[WARN] Ore-chain fallback ratio used: {canonical_id} ({game_code}) "
                f"ore='{ore_key}', output_per_chunk={output_per_chunk:g}"
            )

        crushed_unit_price = chunk_unit_price / output_per_chunk
        crushed_prices_by_ore[ore_key] = crushed_unit_price

        note = (
            f"auto:ore_chain:crushed:{ore_key}:"
            f"chunk_source={chunk_source}:output_per_chunk={output_per_chunk:g}:"
            f"fallback_ratio={'1' if used_fallback_ratio else '0'}"
        )
        cur.execute(
            """
            INSERT INTO price_overrides (canonical_id, unit_price, note)
            VALUES (%s, %s, %s)
            ON CONFLICT (canonical_id) DO UPDATE
            SET unit_price = EXCLUDED.unit_price,
                note = EXCLUDED.note
            """,
            (canonical_id, crushed_unit_price, note),
        )
        updated += 1
        print(
            f"Ore-chain override: {canonical_id} ({game_code}) -> "
            f"{crushed_unit_price:.6f} (chunk={chunk_unit_price:.6f}, out/chunk={output_per_chunk:g})"
        )

    cur.execute(
        """
        SELECT id, game_code
        FROM canonical_items
        WHERE lower(coalesce(game_code, '')) LIKE '%powdered-ore-%'
        ORDER BY id
        """
    )
    powdered_rows = cur.fetchall()

    for canonical_id, game_code in powdered_rows:
        ore_key = _extract_ore_key_from_game_code(game_code, "powdered-ore-")
        if ore_key is None:
            continue

        crushed_unit_price = crushed_prices_by_ore.get(ore_key)
        if crushed_unit_price is None:
            # Fallback: if crushed override exists from a prior run, reuse it.
            cur.execute(
                """
                SELECT p.unit_price
                FROM price_overrides p
                JOIN canonical_items c ON c.id = p.canonical_id
                WHERE lower(coalesce(c.game_code, '')) LIKE lower(%s)
                ORDER BY c.id
                LIMIT 1
                """,
                (f"%crushed-ore-{ore_key}",),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                crushed_unit_price = float(row[0])

        if crushed_unit_price is None:
            print(
                f"[WARN] Ore-chain skip powdered: {canonical_id} ({game_code}) "
                f"no crushed price available for ore='{ore_key}'"
            )
            continue

        note = f"auto:ore_chain:powdered:{ore_key}:from_crushed"
        cur.execute(
            """
            INSERT INTO price_overrides (canonical_id, unit_price, note)
            VALUES (%s, %s, %s)
            ON CONFLICT (canonical_id) DO UPDATE
            SET unit_price = EXCLUDED.unit_price,
                note = EXCLUDED.note
            """,
            (canonical_id, crushed_unit_price, note),
        )
        updated += 1
        print(
            f"Ore-chain override: {canonical_id} ({game_code}) -> "
            f"{crushed_unit_price:.6f} (same-as-crushed)"
        )

    print(f"Ore-chain override pass complete. Total overrides upserted: {updated}")
    return updated


def apply_exact_normalized_name_links(cur) -> int:
    """
    Second-chance linker for canonicals with missing LR links.

    IMPORTANT: exact normalized equality only (no fuzzy/similarity matching).
    """
    cur.execute(
        """
        SELECT id, display_name, game_code
        FROM canonical_items
        WHERE lr_item_id IS NULL
        ORDER BY id
        """
    )
    canonical_rows = cur.fetchall()

    cur.execute(
        """
        SELECT id, display_name
        FROM lr_items
        WHERE display_name IS NOT NULL
          AND btrim(display_name) <> ''
        ORDER BY id
        """
    )
    lr_rows = cur.fetchall()

    normalized_lr_index: Dict[str, List[tuple[int, str]]] = {}
    for lr_id, lr_display_name in lr_rows:
        norm = normalize_for_exact_link(lr_display_name)
        if not norm:
            continue
        normalized_lr_index.setdefault(norm, []).append((int(lr_id), lr_display_name))

    updates = 0
    ambiguous = 0
    for canonical_id, canonical_display_name, canonical_game_code in canonical_rows:
        # Keep chain-family canonicals intentionally unlinked from LR direct-price rows.
        # These are handled via explicit chain cleanup rules and should not be
        # re-linked by normalized display-name matching.
        code = (canonical_game_code or "").lower()
        if code.startswith("item:metalplate-") or code == "item:metalplate-*" or canonical_id.startswith("metalplate"):
            continue
        if code.startswith("item:chain-") or code.startswith("item:metalchain-"):
            continue

        norm = normalize_for_exact_link(canonical_display_name)
        if not norm:
            continue

        matches = normalized_lr_index.get(norm, [])
        if not matches:
            continue

        if len(matches) > 1:
            # Ambiguous exact-normalized collision; skip deterministic safety.
            ambiguous += 1
            print(
                f"[WARN] Exact-normalized LR match ambiguous for canonical '{canonical_id}' "
                f"('{canonical_display_name}') -> {[name for _, name in matches]}"
            )
            continue

        lr_item_id, lr_display_name = matches[0]
        cur.execute(
            """
            UPDATE canonical_items
            SET lr_item_id = %s,
                match_tier = 'manual'
            WHERE id = %s
              AND lr_item_id IS NULL
            """,
            (lr_item_id, canonical_id),
        )
        if cur.rowcount:
            updates += cur.rowcount
            print(
                f"Exact-normalized link: {canonical_id} ('{canonical_display_name}') "
                f"-> {lr_display_name} (lr_items.id={lr_item_id})"
            )

    print(
        "Exact-normalized linking pass complete. "
        f"Rows updated: {updates}. Ambiguous skipped: {ambiguous}."
    )
    return updates


def apply_rule(cur, rule: Dict[str, Optional[str]]) -> int:
    pattern = (rule.get("pattern") or "").strip()
    match_mode = (rule.get("match") or "").strip().lower()
    lr_item_id = rule.get("lr_item_id")

    if not pattern or lr_item_id is None:
        return 0

    if match_mode == "exact":
        cur.execute(
            """
            UPDATE canonical_items
            SET lr_item_id = %s,
                match_tier = 'manual'
            WHERE lower(game_code) = lower(%s)
            """,
            (lr_item_id, pattern),
        )
    elif match_mode == "like":
        cur.execute(
            """
            UPDATE canonical_items
            SET lr_item_id = %s,
                match_tier = 'manual'
            WHERE lower(game_code) LIKE lower(%s)
            """,
            (lr_item_id, pattern),
        )
    else:
        raise ValueError(f"Unsupported match mode: {match_mode}")

    return cur.rowcount


def main() -> int:
    print("[MEMORY BANK: ACTIVE]")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                lang_alias_map = load_lang_alias_map()
                ensure_manual_match_tier_allowed(cur)
                ensure_price_overrides_table(cur)
                upsert_price_overrides(cur)
                nugget_override_updates = apply_nugget_price_overrides(cur, lang_alias_map)
                metalbit_override_updates = apply_metalbit_price_overrides(cur, lang_alias_map)
                ore_chain_updates = apply_ore_processing_price_overrides(cur)

                total_updated = 0
                total_updated += nugget_override_updates
                total_updated += metalbit_override_updates
                total_updated += ore_chain_updates
                total_updated += fix_chain_ingot_suffix_matches(cur)
                total_updated += fix_metalbit_ingot_suffix_matches(cur)

                for rule in MANUAL_LINKS:
                    lr_display = (rule.get("lr_display") or "").strip()
                    lr_item_id = lookup_lr_item_id(cur, lr_display)
                    if lr_item_id is None:
                        print(f"[WARN] Skipped: '{lr_display}' not found in lr_items")
                        continue

                    rule["lr_item_id"] = lr_item_id
                    updated = apply_rule(cur, rule)
                    total_updated += updated

                    print(
                        f"Rule: {rule['match'].upper():5} {rule['pattern']:<28} "
                        f"-> {lr_display} (lr_items.id={lr_item_id}) | updated={updated}"
                    )

                exact_name_updates = apply_exact_normalized_name_links(cur)
                total_updated += exact_name_updates

                explicit_link_updates = apply_explicit_canonical_lr_links(cur)
                total_updated += explicit_link_updates

                forced_chain_unlinks = enforce_forced_chain_unlinks(cur)
                total_updated += forced_chain_unlinks

                primary_alias_updates = enforce_primary_metal_aliases(cur)
                total_updated += primary_alias_updates

                print(f"[OK] Manual linking complete. Total rows updated: {total_updated}")

        return 0
    except Exception as exc:
        traceback.print_exc()
        print(f"[ERROR] Failed applying manual LR links: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())