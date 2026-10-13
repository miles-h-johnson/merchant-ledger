#!/usr/bin/env python3
"""
Build generated aliases for canonical_items into item_aliases.

Behavior:
  - Reads DATABASE_URL from environment.
  - Truncates item_aliases on each run.
  - Generates aliases from canonical_items.display_name and canonical_items.game_code.
  - Inserts only non-empty, deduplicated aliases per canonical_id with source='generated'.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

import psycopg2
from dotenv import load_dotenv


load_dotenv()


PARENS_RE = re.compile(r"\([^)]*\)")
WHITESPACE_RE = re.compile(r"\s+")

COMPOUND_SEGMENT_SPLITS = {
    "knifeblade": "knife blade",
    "axehead": "axe head",
    "shovelhead": "shovel head",
    "arrowhead": "arrow head",
    "hammerhead": "hammer head",
    "pickaxehead": "pickaxe head",
    "spearhead": "spear head",
}


CLOTHING_POSITION_TOKENS = {
    "upperbody",
    "lowerbody",
    "upperbodyover",
    "head",
    "hands",
    "hand",
    "feet",
    "foot",
    "neck",
}


CLOTHING_COLOR_TOKENS = {
    "plain",
    "black",
    "white",
    "brown",
    "gray",
    "grey",
    "red",
    "blue",
    "green",
    "yellow",
    "pink",
    "orange",
    "purple",
}


CLOTHING_COMPOUND_SPLITS = {
    # User-specified clothing compounds.
    "woolsweater": "wool sweater",
    "mountaineerpants": "mountaineer pants",
    "mountaineercoat": "mountaineer coat",
    "mountaineercoatnofringe": "mountaineer coat no fringe",
    "leathercoat": "leather coat",
    "leatherpants": "leather pants",
    "flaxdress": "flax dress",
    "linendress": "linen dress",
    "cottonshirt": "cotton shirt",
    "woolhat": "wool hat",
    # Additional compounds observed in item:clothes-* game_code corpus.
    "woolsweatershort": "wool sweater short",
    "woolsweatertucked": "wool sweater tucked",
    "woolsweatertuckedshort": "wool sweater tucked short",
    "coloredcottonshirt": "colored cotton shirt",
    "coloredcottonshirtlong": "colored cotton shirt long",
    "coloredcottonshirttucked": "colored cotton shirt tucked",
    "coloredcottonshirttuckedlong": "colored cotton shirt tucked long",
    "flanneltucked": "flannel tucked",
    "flanneltuckedlong": "flannel tucked long",
    "suspendersleather": "suspenders leather",
    "suspenderslinen": "suspenders linen",
    "leathergloves": "leather gloves",
    "leatherglovesfringe": "leather gloves fringe",
    "leatherchaps": "leather chaps",
    "bluejeans": "blue jeans",
    "summerdress": "summer dress",
    "finegown": "fine gown",
    "tailcoat": "tail coat",
    "waistscarf": "waist scarf",
    "strawboaterhat": "straw boater hat",
    "cowboyhatopencrown": "cowboy hat open crown",
    "chapsbasic": "chaps basic",
}


# Known barrel outputs where a short user-facing name differs materially from
# display_name generated from game code expansion.
BARREL_SHORT_ALIASES = {
    "item:leather-normal-plain": ["leather", "leathers"],
    "item:weaktanninportion": ["tannin", "tannins"],
}


# Craftable grid outputs that should keep stable plain-word aliases even when
# other recipe-less/modded items generate similar short aliases.
GRID_SHORT_ALIASES = {
    "block:chest-east": ["chest", "chests"],
}


def normalize_alias(text: str) -> str:
    """Lowercase, trim, and collapse inner whitespace."""
    value = WHITESPACE_RE.sub(" ", (text or "").strip().lower())
    return value


def pluralize_simple(text: str) -> str:
    """Simple plural rule from spec: append 's' when not already ending in 's'."""
    value = normalize_alias(text)
    if not value:
        return ""
    if value.endswith("s"):
        return value
    return f"{value}s"


def game_code_tail(game_code: str) -> str:
    """
    Strip domain prefix and secondary namespace segments.

    Examples:
      item:ingot-iron -> ingot-iron
      item:game:ingot-iron -> ingot-iron
    """
    return (game_code or "").strip().split(":")[-1]


def humanize_code_fragment(text: str) -> str:
    """Replace '-' and '_' with spaces, then normalize."""
    return normalize_alias((text or "").replace("-", " ").replace("_", " "))


def aliases_from_display_name(
    display_name: Optional[str], *, include_parenthetical_stripped: bool = True
) -> Set[str]:
    aliases: Set[str] = set()
    base = normalize_alias(display_name or "")
    if not base:
        return aliases

    # Display name itself
    aliases.add(base)

    # Plural display name
    plural = pluralize_simple(base)
    if plural:
        aliases.add(plural)

    # Parenthetical stripped variant
    if include_parenthetical_stripped and "(" in base and ")" in base:
        stripped = normalize_alias(PARENS_RE.sub("", base))
        if stripped:
            aliases.add(stripped)

    # Slash-separated parts
    if "/" in base:
        for part in base.split("/"):
            part_alias = normalize_alias(part)
            if part_alias:
                aliases.add(part_alias)

    return aliases


def aliases_for_plate_armor(display_name: Optional[str]) -> Set[str]:
    """Generate disambiguated aliases for LR plate armor rows.

    Example:
      "Iron Plate (Plate)" -> "iron plate armor", "iron plate armour"
    """
    aliases: Set[str] = set()
    base = normalize_alias(display_name or "")
    if not base:
        return aliases

    match = re.match(r"^(.+?)\s+plate\b", base)
    if not match:
        return aliases

    metal = normalize_alias(match.group(1))
    if not metal:
        return aliases

    aliases.add(normalize_alias(f"{metal} plate armor"))
    aliases.add(normalize_alias(f"{metal} plate armour"))
    return aliases


def aliases_from_armor_plate_game_code(game_code: Optional[str]) -> Set[str]:
    """Generate armor-oriented aliases for item:armor-*-plate-<metal> rows."""
    aliases: Set[str] = set()
    if not game_code:
        return aliases

    tail = game_code_tail(game_code)
    match = re.match(r"^armor-(body|head|legs)-plate-(.+)$", tail)
    if not match:
        return aliases

    part = match.group(1)
    metal = humanize_code_fragment(match.group(2))
    if not metal:
        return aliases

    part_label = {
        "body": "body armor",
        "head": "helmet",
        "legs": "leg armor",
    }.get(part, "armor")

    aliases.add(normalize_alias(f"{metal} plate armor"))
    aliases.add(normalize_alias(f"{metal} plate armour"))
    aliases.add(normalize_alias(f"plate armor {metal}"))
    aliases.add(normalize_alias(f"plate armour {metal}"))
    aliases.add(normalize_alias(f"{metal} plate {part_label}"))
    aliases.add(normalize_alias(f"{part_label} {metal} plate"))
    return aliases


def aliases_from_game_code(game_code: Optional[str]) -> Set[str]:
    aliases: Set[str] = set()
    if not game_code:
        return aliases

    tail = game_code_tail(game_code)
    if not tail:
        return aliases

    # Humanized full tail
    humanized_tail = humanize_code_fragment(tail)
    if humanized_tail:
        aliases.add(humanized_tail)

    # Last '-' segment humanized.
    # Guardrail: skip overly-generic single-word aliases for schematic-like
    # mod items (e.g. br-schematic-chest -> chest) to avoid collisions with
    # craftable base-game items.
    schematic_like = re.search(r"(^|[-_])schematic([-_]|$)", tail) is not None
    if not schematic_like:
        last_dash_segment = tail.split("-")[-1]
        humanized_last = humanize_code_fragment(last_dash_segment)
        if humanized_last:
            aliases.add(humanized_last)

            # Last segment pluralized
            plural_last = pluralize_simple(humanized_last)
            if plural_last:
                aliases.add(plural_last)

    return aliases


def aliases_from_compound_game_code(game_code: Optional[str]) -> Set[str]:
    """Add aliases for common VS compound output fragments.

    Examples:
      item:knifeblade-flint -> "knife blade flint", "flint knife blade"
      item:axehead-copper -> "axe head copper", "copper axe head"
    """
    aliases: Set[str] = set()
    if not game_code:
        return aliases

    tail = game_code_tail(game_code)
    if not tail:
        return aliases

    segments = [seg for seg in tail.split("-") if seg]
    if not segments:
        return aliases

    expanded_segments = [COMPOUND_SEGMENT_SPLITS.get(seg, seg) for seg in segments]
    expanded_alias = normalize_alias(" ".join(expanded_segments))
    if expanded_alias:
        aliases.add(expanded_alias)

    # Material-first phrasing for common smithing/knapping output tails.
    # e.g. "knifeblade-flint" -> "flint knife blade"
    if len(segments) == 2 and segments[0] in COMPOUND_SEGMENT_SPLITS:
        head = normalize_alias(COMPOUND_SEGMENT_SPLITS[segments[0]])
        material = humanize_code_fragment(segments[1])
        if head and material:
            aliases.add(normalize_alias(f"{material} {head}"))

    return aliases


def aliases_from_metalplate_code(game_code: Optional[str]) -> Set[str]:
    """Generate explicit aliases for item:metalplate-{metal} game codes only."""
    aliases: Set[str] = set()
    if not game_code:
        return aliases

    tail = game_code_tail(game_code)
    match = re.match(r"^metalplate-(.+)$", tail)
    if not match:
        return aliases

    metal = humanize_code_fragment(match.group(1))
    if not metal:
        return aliases

    aliases.add(normalize_alias(f"{metal} plate"))
    aliases.add(normalize_alias(f"{metal} plates"))
    aliases.add(normalize_alias(f"metal plate {metal}"))

    return aliases


def aliases_from_clothes_game_code(game_code: Optional[str]) -> Set[str]:
    """Generate natural-language aliases for item:clothes-* game codes only."""
    aliases: Set[str] = set()
    if not game_code:
        return aliases

    tail = game_code_tail(game_code)
    if not tail.startswith("clothes-"):
        return aliases

    remainder = tail[len("clothes-") :]
    if not remainder:
        return aliases

    output_tokens: List[str] = []
    for segment in remainder.split("-"):
        token = normalize_alias(segment)
        if not token:
            continue
        if token in CLOTHING_POSITION_TOKENS:
            continue
        if token in CLOTHING_COLOR_TOKENS:
            continue

        split_phrase = CLOTHING_COMPOUND_SPLITS.get(token)
        if split_phrase:
            output_tokens.extend(split_phrase.split())
        else:
            output_tokens.append(token)

    phrase = normalize_alias(" ".join(output_tokens))
    if len(phrase) >= 3:
        aliases.add(phrase)

    return aliases


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
    """Build game_code -> display_name map from lang/en.json files.

    Supported keys:
      - item-<tail>  -> item:<tail>
      - block-<tail> -> block:<tail>
    """
    alias_map: Dict[str, str] = {}

    for path in discover_lang_files():
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
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


def load_canonical_items(
    cur,
) -> List[
    Tuple[
        str,
        Optional[str],
        Optional[str],
        Optional[int],
        Optional[str],
    ]
]:
    cur.execute(
        """
        SELECT
            ci.id,
            ci.display_name,
            ci.game_code,
            ci.lr_item_id,
            li.lr_sub_category
        FROM canonical_items ci
        LEFT JOIN lr_items li ON li.id = ci.lr_item_id
        ORDER BY ci.id
        """
    )
    return cur.fetchall()


def build_alias_rows(
    canonical_rows: Sequence[
        Tuple[
            str,
            Optional[str],
            Optional[str],
            Optional[int],
            Optional[str],
        ]
    ],
    lang_alias_map: Optional[Dict[str, str]] = None,
) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    lang_alias_map = lang_alias_map or {}

    for (
        canonical_id,
        display_name,
        game_code,
        lr_item_id,
        lr_sub_category,
    ) in canonical_rows:
        aliases: Set[str] = set()
        is_plate_armor = bool(
            game_code
            and re.match(r"^armor-(body|head|legs)-plate-[a-z0-9]+$", game_code_tail(game_code))
        )
        looks_like_plate_armor_name = " plate (" in normalize_alias(display_name or "")

        if is_plate_armor:
            aliases.update(
                aliases_from_display_name(
                    display_name,
                    include_parenthetical_stripped=False,
                )
            )
            aliases.update(aliases_for_plate_armor(display_name))
            aliases.update(aliases_from_armor_plate_game_code(game_code))
        elif lr_item_id is not None and looks_like_plate_armor_name:
            # Avoid stripping parentheticals for LR plate armor style names
            # like "Iron Plate (Viking / Hussar / Templar)" -> "iron plate".
            aliases.update(
                aliases_from_display_name(
                    display_name,
                    include_parenthetical_stripped=False,
                )
            )
        else:
            aliases.update(aliases_from_display_name(display_name))

        lang_display_name = lang_alias_map.get(game_code or "")
        if lang_display_name:
            aliases.update(aliases_from_display_name(lang_display_name))

        aliases.update(aliases_from_metalplate_code(game_code))
        aliases.update(aliases_from_game_code(game_code))
        aliases.update(aliases_from_compound_game_code(game_code))
        aliases.update(aliases_from_clothes_game_code(game_code))

        # Targeted short aliases for known outputs.
        for extra_alias in BARREL_SHORT_ALIASES.get(game_code or "", []):
            normalized_extra_alias = normalize_alias(extra_alias)
            if normalized_extra_alias:
                aliases.add(normalized_extra_alias)

        for extra_alias in GRID_SHORT_ALIASES.get(game_code or "", []):
            normalized_extra_alias = normalize_alias(extra_alias)
            if normalized_extra_alias:
                aliases.add(normalized_extra_alias)

        for alias in sorted(aliases):
            normalized = normalize_alias(alias)
            if not normalized:
                continue
            rows.append((normalized, canonical_id, "generated"))

    return rows


def insert_alias_rows(cur, rows: Sequence[Tuple[str, str, str]]) -> None:
    if not rows:
        return

    cur.executemany(
        """
        INSERT INTO item_aliases (alias, canonical_id, source)
        VALUES (%s, %s, %s)
        """,
        rows,
    )


def alias_to_display_name(alias: str) -> str:
    return normalize_alias(alias).title()


def improve_unmatched_display_names_from_generated_aliases(
    cur,
    *,
    lang_alias_map: Dict[str, str],
) -> int:
    """
    For unmatched/non-LR canonical rows with no lang entry, prefer the most
    human-readable generated alias as display_name fallback.

    Human-readable preference:
      1) more spaces (word boundaries)
      2) longer alias (more descriptive)
      3) lexicographic stability
    """
    cur.execute(
        """
        SELECT
            ci.id,
            ci.display_name,
            ci.game_code,
            ia.alias
        FROM canonical_items ci
        JOIN item_aliases ia
          ON ia.canonical_id = ci.id
         AND ia.source = 'generated'
        WHERE ci.match_tier = 'unmatched'
          AND ci.lr_item_id IS NULL
          AND ci.game_code IS NOT NULL
        ORDER BY ci.id, ia.alias
        """
    )
    rows = cur.fetchall()

    grouped: Dict[str, Dict[str, object]] = {}
    for canonical_id, current_name, game_code, alias in rows:
        if not alias:
            continue
        if game_code in lang_alias_map:
            # Lang file remains preferred for game_code-backed names.
            continue
        entry = grouped.setdefault(
            canonical_id,
            {
                "current_name": current_name or "",
                "best_alias": None,
            },
        )
        best_alias = entry["best_alias"]
        if best_alias is None:
            entry["best_alias"] = alias
            continue

        candidate_key = (-alias.count(" "), -len(alias), alias)
        best_key = (-best_alias.count(" "), -len(best_alias), best_alias)
        if candidate_key < best_key:
            entry["best_alias"] = alias

    updates: List[Tuple[str, str]] = []
    for canonical_id, payload in grouped.items():
        best_alias = payload.get("best_alias")
        current_name = normalize_alias(payload.get("current_name") or "")
        if not best_alias:
            continue
        best_display_name = alias_to_display_name(best_alias)
        if normalize_alias(best_display_name) == current_name:
            continue
        updates.append((best_display_name, canonical_id))

    if updates:
        cur.executemany(
            """
            UPDATE canonical_items
            SET display_name = %s
            WHERE id = %s
            """,
            updates,
        )

    return len(updates)


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE item_aliases")

                canonical_rows = load_canonical_items(cur)
                lang_alias_map = load_lang_alias_map()
                alias_rows = build_alias_rows(canonical_rows, lang_alias_map)
                insert_alias_rows(cur, alias_rows)
                fallback_updates = improve_unmatched_display_names_from_generated_aliases(
                    cur,
                    lang_alias_map=lang_alias_map,
                )

                print(f"Total aliases generated: {len(alias_rows)}")
                print(f"Lang-derived game-code labels loaded: {len(lang_alias_map)}")
                print(f"Display-name fallback updates from generated aliases: {fallback_updates}")
                print("[OK] Done.")

    except Exception as exc:
        print(f"[ERROR] Failed to build aliases: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
