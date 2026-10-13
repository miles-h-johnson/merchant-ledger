#!/usr/bin/env python3
"""
Build canonical_items from recipe outputs and Lost Realm items.

Behavior:
  - Reads DATABASE_URL from environment.
  - Truncates canonical_items on each run.
  - Collects candidates from:
      A) distinct recipes.output_game_code
      B) lr_items (id, display_name, lr_category)
      C) distinct recipe_ingredients.input_game_code
  - Cross-matches game codes to LR items with exact/high/low priority.
  - Inserts one row per unique slug (disambiguating with _2, _3, ...).
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Fuzzy scoring backend selection
# ---------------------------------------------------------------------------
# Prefer rapidfuzz (C-extension, ~10-50x faster) when available; fall back to
# a lightweight token-overlap scorer that avoids the pathological O(N*M)
# SequenceMatcher inner loop while still producing comparable similarity
# scores for the short strings used here.
# ---------------------------------------------------------------------------
try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore[import-untyped]

    def _fuzzy_ratio(a: str, b: str) -> float:
        """Return similarity ratio in [0.0, 1.0] using rapidfuzz."""
        return _rf_fuzz.ratio(a, b) / 100.0

    _FUZZY_BACKEND = "rapidfuzz"

except ImportError:
    # Lightweight fallback: combined normalised token-overlap + length-ratio
    # scorer.  Produces scores on a comparable 0-1 scale to SequenceMatcher
    # for short game-item strings (typically <40 chars).
    from difflib import SequenceMatcher as _SequenceMatcher

    class _ReusableMatcher:
        """SequenceMatcher wrapper that indexes seq2 once and reuses it."""

        __slots__ = ("_sm",)

        def __init__(self, seq2: str) -> None:
            self._sm = _SequenceMatcher(None, "", seq2)

        def ratio(self, seq1: str) -> float:
            self._sm.set_seq1(seq1)
            return self._sm.ratio()

    def _fuzzy_ratio(a: str, b: str) -> float:  # type: ignore[misc]
        """Fallback: plain SequenceMatcher (one-shot, no reuse)."""
        return _SequenceMatcher(None, a, b).ratio()

    _FUZZY_BACKEND = "difflib"


import psycopg2
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class LRItem:
    item_id: str
    id: int
    display_name: str
    lr_category: Optional[str]
    lr_sub_category: Optional[str]


@dataclass(frozen=True)
class FTAItem:
    id: int
    display_name: str


@dataclass
class CanonicalRow:
    slug: str
    display_name: str
    game_code: Optional[str]
    lr_item_id: Optional[int]
    fta_item_id: Optional[int]
    match_tier: Optional[str]
    match_score: Optional[float]


@dataclass
class LRMatchIndex:
    """Precomputed lookup structures for LR item matching.

    Built once from lr_items to eliminate repeated normalization inside
    inner matching loops.
    """
    items: Sequence[LRItem]
    by_normalized_name: Dict[str, List[LRItem]]
    by_lowercase_name: Dict[str, List[LRItem]]
    items_with_norm: List[Tuple[LRItem, str]]
    token_sets: Dict[int, Set[str]]
    # --- Performance extensions ---
    # Precomputed lengths of normalized names (parallel to items_with_norm)
    norm_lengths: List[int] = field(default_factory=list)
    # Reusable difflib matchers keyed by LR item index (only built when
    # _FUZZY_BACKEND == "difflib"); empty otherwise.
    reusable_matchers: List[Optional[Any]] = field(default_factory=list)


NORMALIZE_COMPARE_RE = re.compile(r"[-_\s]+")
NON_SLUG_CHAR_RE = re.compile(r"[^a-z0-9_]+")
MULTI_UNDERSCORE_RE = re.compile(r"_+")
PLACEHOLDER_RE = re.compile(r"\{[^}]+\}")
PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
PAREN_CONTENT_RE = re.compile(r"\(([^)]*)\)")
UPPERCASE_PAREN_CONTENT_RE = re.compile(r"^[A-Z0-9\s'&/\-]+$")
ADMIN_NOTE_CONTENT_RE = re.compile(
    r"^(?:"
    r"havent\s+changed"
    r"|not\s+buying"
    r"|limit\b.*"
    r"|per\s+barrel\b.*"
    r")$",
    re.IGNORECASE,
)
NON_ALNUM_SPACE_RE = re.compile(r"[^a-z0-9\s]+")
MULTI_SPACE_RE = re.compile(r"\s+")
TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")
SUFFIX_MATCH_PREFIXES = frozenset(
    {
        "ingot",
        "ore",
        "nugget",
        "dust",
        "powder",
        "plate",
        "sheet",
        "log",
        "plank",
        "block",
        "grain",
        "seed",
        "flower",
        "crop",
    }
)

METAL_VARIANT_MATERIALS = {
    "copper",
    "iron",
    "tinbronze",
    "bismuthbronze",
    "blackbronze",
    "gold",
    "silver",
    "steel",
    "meteoriciron",
    "lead",
    "tin",
    "zinc",
    "nickel",
    "brass",
    "chromium",
    "cupronickel",
    "electrum",
    "molybdochalkos",
    "platinum",
    "titanium",
}


VALID_MATCH_TIERS = ("exact", "high", "low", "unmatched", "manual", "mapped")
MAPPING_FILE_PATH = os.path.join("data", "lr_item_mapping.json")
MAPPING_WARNINGS_PATH = os.path.join("data", "lr_mapping_warnings.json")
MATCH_ANOMALIES_PATH = os.path.join("data", "match_anomalies.json")

# Tiers exempt from anomaly reporting (authoritative linkage — never flagged)
_ANOMALY_EXEMPT_DB_TIERS = frozenset({"mapped", "manual"})


def ensure_match_tier_allowed(cur) -> None:
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


def normalize_mapping_game_code(game_code: str) -> str:
    normalized = normalize_game_code(game_code)
    return normalized.lower() if normalized else ""


def write_mapping_warnings(payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(MAPPING_WARNINGS_PATH), exist_ok=True)
    with open(MAPPING_WARNINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def load_lr_item_mapping(
    cur,
    *,
    lr_items: Sequence[LRItem],
) -> Tuple[Dict[str, Tuple[int, str]], Set[str], bool, Dict[str, Any]]:
    warnings_payload: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mapping_file": MAPPING_FILE_PATH,
        "mapping_active": False,
        "missing_lr_item_ids": [],
        "duplicate_game_codes": {},
        "invalid_entries": [],
    }

    if not os.path.exists(MAPPING_FILE_PATH):
        warnings_payload["invalid_entries"].append("mapping_file_missing")
        write_mapping_warnings(warnings_payload)
        return {}, set(), False, warnings_payload

    try:
        with open(MAPPING_FILE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        warnings_payload["invalid_entries"].append(f"mapping_file_read_error:{exc}")
        write_mapping_warnings(warnings_payload)
        return {}, set(), False, warnings_payload

    if not isinstance(raw, dict):
        warnings_payload["invalid_entries"].append("mapping_root_not_object")
        write_mapping_warnings(warnings_payload)
        return {}, set(), False, warnings_payload

    mapping_status = str(raw.get("_status") or "").strip().lower()
    mapping_active = mapping_status == "active"
    warnings_payload["mapping_active"] = mapping_active

    force_unlinked_values = raw.get("_force_unlinked") or []
    force_unlinked: Set[str] = set()
    if isinstance(force_unlinked_values, list):
        for game_code in force_unlinked_values:
            if not isinstance(game_code, str):
                continue
            normalized = normalize_mapping_game_code(game_code)
            if normalized:
                force_unlinked.add(normalized)
    elif force_unlinked_values is not None:
        warnings_payload["invalid_entries"].append("_force_unlinked_not_list")

    if not mapping_active:
        warnings_payload["invalid_entries"].append("mapping_status_not_active")
        write_mapping_warnings(warnings_payload)
        return {}, force_unlinked, False, warnings_payload

    lr_item_id_index = {item.item_id.upper(): item for item in lr_items if item.item_id}
    reverse_index: Dict[str, Tuple[int, str]] = {}
    duplicate_game_codes: Dict[str, Set[str]] = {}
    missing_lr_item_ids: List[str] = []

    for key, value in raw.items():
        if key.startswith("_"):
            continue

        lr_item_id = str(key or "").strip().upper()
        if not lr_item_id:
            continue

        lr_item = lr_item_id_index.get(lr_item_id)
        if lr_item is None:
            missing_lr_item_ids.append(lr_item_id)
            continue

        if not isinstance(value, list):
            warnings_payload["invalid_entries"].append(f"{lr_item_id}:not_list")
            continue

        for mapped_code in value:
            if not isinstance(mapped_code, str):
                warnings_payload["invalid_entries"].append(f"{lr_item_id}:non_string_game_code")
                continue
            normalized_code = normalize_mapping_game_code(mapped_code)
            if not normalized_code:
                continue

            existing = reverse_index.get(normalized_code)
            if existing and existing[0] != lr_item.id:
                dup = duplicate_game_codes.setdefault(normalized_code, set())
                dup.add(lr_item_id)
                # preserve original mapping LR id in report as well
                for maybe_item_id, maybe_item in lr_item_id_index.items():
                    if maybe_item.id == existing[0]:
                        dup.add(maybe_item_id)
                        break
                continue

            reverse_index[normalized_code] = (lr_item.id, lr_item.display_name)

    if missing_lr_item_ids:
        warnings_payload["missing_lr_item_ids"] = sorted(set(missing_lr_item_ids))

    if duplicate_game_codes:
        warnings_payload["duplicate_game_codes"] = {
            code: sorted(item_ids) for code, item_ids in sorted(duplicate_game_codes.items())
        }
        write_mapping_warnings(warnings_payload)
        duplicate_detail = ", ".join(
            f"{code} -> {item_ids}"
            for code, item_ids in warnings_payload["duplicate_game_codes"].items()
        )
        raise ValueError(f"Duplicate mapping game_code assignments detected: {duplicate_detail}")

    write_mapping_warnings(warnings_payload)
    return reverse_index, force_unlinked, True, warnings_payload


def normalize_game_code(game_code: str) -> str:
    """
    Normalize codes to a canonical stored form that always retains a type domain
    prefix (`item:`/`block:`).

    Examples:
      item:game:ingot-iron -> item:ingot-iron
      item:pipeleaf:smokable-pipeleaf-cured -> item:smokable-pipeleaf-cured
      item:ingot-iron -> item:ingot-iron
      ingot-iron -> item:ingot-iron
    """
    text = (game_code or "").strip()
    if not text:
        return text

    parts = text.split(":")
    if len(parts) >= 3:
        return f"{parts[0].lower()}:{parts[-1]}"
    if len(parts) == 2:
        domain = parts[0].lower()
        if domain in {"item", "block"}:
            return f"{domain}:{parts[-1]}"
        return f"item:{parts[-1]}"
    if len(parts) == 1:
        return f"item:{parts[0]}"
    return text


def game_code_tail(game_code: str) -> str:
    """Return the segment after the last namespace colon."""
    return (game_code or "").strip().split(":")[-1]


def slug_from_game_code(game_code: str) -> str:
    tail = game_code_tail(game_code)
    slug = tail.lower().replace("-", "_").replace(":", "_")
    slug = NON_SLUG_CHAR_RE.sub("", slug)
    slug = MULTI_UNDERSCORE_RE.sub("_", slug).strip("_")
    return slug or "item"


def slug_from_display_name(display_name: str) -> str:
    text = (display_name or "").strip().lower()
    text = text.replace(" ", "_")
    text = NON_SLUG_CHAR_RE.sub("", text)
    text = MULTI_UNDERSCORE_RE.sub("_", text).strip("_")
    return text or "item"


def normalize_for_compare(value: str) -> str:
    return NORMALIZE_COMPARE_RE.sub("", (value or "").lower())


def normalize_name_for_linking(value: str) -> str:
    text = (value or "").lower()
    text = NON_ALNUM_SPACE_RE.sub(" ", text)
    text = MULTI_SPACE_RE.sub(" ", text).strip()
    return text


def trigram_set(value: str) -> Set[str]:
    text = normalize_name_for_linking(value)
    if not text:
        return set()
    padded = f"  {text}  "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """Dice coefficient over trigram sets."""
    ta = trigram_set(a)
    tb = trigram_set(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta.intersection(tb))
    return (2.0 * overlap) / (len(ta) + len(tb))


def trigram_similarity_sets(ta: Set[str], tb: Set[str]) -> float:
    if not ta or not tb:
        return 0.0
    overlap = len(ta.intersection(tb))
    return (2.0 * overlap) / (len(ta) + len(tb))


def normalize_lr_name_for_match(display_name: str) -> str:
    """
    Normalize LR display names for matching only.

    Parenthetical suffixes (sub-category hints) are stripped first:
      "Iron Brigandine (Brigandine)" -> "Iron Brigandine" -> "ironbrigandine"
    """
    text = (display_name or "").strip()
    text = PAREN_SUFFIX_RE.sub("", text).strip()
    return normalize_for_compare(text)


def build_lr_match_index(lr_items: Sequence[LRItem]) -> LRMatchIndex:
    """Build precomputed LR lookup structures.

    normalize_lr_name_for_match is called exactly once per LR item here
    and never again during matching.  Precomputes norm lengths and, when
    the difflib backend is active, reusable SequenceMatcher objects that
    index seq2 once to avoid 796k+ redundant __chain_b rebuilds.
    """
    by_normalized_name: Dict[str, List[LRItem]] = {}
    by_lowercase_name: Dict[str, List[LRItem]] = {}
    items_with_norm: List[Tuple[LRItem, str]] = []
    token_sets: Dict[int, Set[str]] = {}
    norm_lengths: List[int] = []
    reusable_matchers: List[Optional[Any]] = []

    use_reusable = _FUZZY_BACKEND == "difflib"

    for item in lr_items:
        norm = normalize_lr_name_for_match(item.display_name)
        items_with_norm.append((item, norm))
        by_normalized_name.setdefault(norm, []).append(item)
        norm_lengths.append(len(norm))

        lower = item.display_name.lower().strip()
        by_lowercase_name.setdefault(lower, []).append(item)

        tokens = {t.lower() for t in TOKEN_SPLIT_RE.split(item.display_name) if t}
        token_sets[item.id] = tokens

        # Pre-build a reusable SequenceMatcher with this LR norm as seq2
        # so __chain_b runs once per LR item, not once per comparison.
        if use_reusable and norm:
            reusable_matchers.append(_ReusableMatcher(norm))  # type: ignore[possibly-undefined]
        else:
            reusable_matchers.append(None)

    return LRMatchIndex(
        items=lr_items,
        by_normalized_name=by_normalized_name,
        by_lowercase_name=by_lowercase_name,
        items_with_norm=items_with_norm,
        token_sets=token_sets,
        norm_lengths=norm_lengths,
        reusable_matchers=reusable_matchers,
    )


def strip_admin_note_parentheticals(display_name: str) -> str:
    text = (display_name or "").strip()
    if not text:
        return text

    def should_strip(content: str) -> bool:
        cleaned = (content or "").strip()
        if not cleaned:
            return False

        if ADMIN_NOTE_CONTENT_RE.match(cleaned):
            return True

        return bool(UPPERCASE_PAREN_CONTENT_RE.match(cleaned) and re.search(r"[A-Z]", cleaned))

    cleaned_name = text
    for match in list(PAREN_CONTENT_RE.finditer(text)):
        content = match.group(1)
        if should_strip(content):
            full_paren = match.group(0)
            cleaned_name = cleaned_name.replace(full_paren, " ")

    cleaned_name = re.sub(r"\s+", " ", cleaned_name).strip()
    return cleaned_name


def canonical_display_name_from_lr(lr_display_name: str, fallback_tail: str) -> str:
    cleaned = strip_admin_note_parentheticals(lr_display_name)
    return cleaned or humanize_game_tail(fallback_tail)


def lr_name_overlaps_game_code(lr_name: str, game_code_tail: str) -> bool:
    """
    For low-tier matches, determine whether LR-name inheritance is still safe.

    Rules:
      - extract LR words longer than 3 chars
      - strip trailing 's' from each word (plural handling)
      - if ANY normalized word appears in game-code tail (case-insensitive), overlap=True
    """
    tail = (game_code_tail or "").lower()
    if not tail:
        return False

    words = re.findall(r"[A-Za-z]+", lr_name or "")
    for word in words:
        if len(word) <= 3:
            continue
        stem = word.lower()
        if stem.endswith("s") and len(stem) > 1:
            stem = stem[:-1]
        if stem and stem in tail:
            return True
    return False


def canonical_display_name_for_match(
    *,
    tier: str,
    tail: str,
    lr_display_name: Optional[str],
) -> str:
    """
    Select canonical display_name from match confidence.

    Low-confidence LR fuzzy matches should retain game-code-derived naming to avoid
    propagating potentially incorrect LR display-name aliases.
    """
    if (tier or "").lower() == "low":
        if lr_display_name and lr_name_overlaps_game_code(lr_display_name, tail):
            return canonical_display_name_from_lr(lr_display_name, tail)
        return humanize_game_tail(tail)
    if lr_display_name:
        return canonical_display_name_from_lr(lr_display_name, tail)
    return humanize_game_tail(tail)


def humanize_game_tail(tail: str) -> str:
    text = (tail or "").strip()
    # Remove template placeholders entirely (e.g. "metalplate-{metal}" -> "metalplate-").
    text = PLACEHOLDER_RE.sub("", text)
    text = re.sub(r"[-_]{2,}", "-", text)
    text = text.strip("-_ ")

    text = text.replace("-", " ").replace("_", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text.title() if text else "Unknown Item"


def disambiguate_slug(base_slug: str, seen: Set[str]) -> str:
    slug = base_slug or "item"
    if slug not in seen:
        seen.add(slug)
        return slug

    index = 2
    while True:
        candidate = f"{slug}_{index}"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        index += 1


def _jaccard_overlap(a: str, b: str) -> float:
    """Token-level Jaccard similarity: |intersection| / |union|.

    Tokenizes by splitting on non-alphanumeric characters and lowercasing.
    Returns 0.0 when the union is empty.
    """
    tokens_a = {t.lower() for t in TOKEN_SPLIT_RE.split(a) if t}
    tokens_b = {t.lower() for t in TOKEN_SPLIT_RE.split(b) if t}
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


def _guardrail_reject(
    *,
    game_code: str,
    tier: str,
    lr_name: str,
    rule: int,
    counters: Optional[Dict[str, int]],
    fuzzy_calls: int,
) -> Dict[str, Any]:
    print(f"[GUARDRAIL] game_code={game_code} rejected tier={tier} lr={lr_name} rule={rule}")
    if counters is not None:
        key = f"guardrail_rule_{rule}_rejections"
        counters[key] = counters.get(key, 0) + 1
    return {
        "item": None,
        "tier": "none",
        "score": None,
        "fuzzy_calls": fuzzy_calls,
        "confidence": "low",
        "overlap_score": None,
        "length_ratio": None,
    }


def choose_best_lr_match(
    game_tail: str,
    lr_index: LRMatchIndex,
    *,
    game_code: Optional[str] = None,
    counters: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Return a confidence-annotated match result dict.

    Keys::

        item          – Optional[LRItem]
        tier          – str   (internal: "exact","normalized","contains","fuzzy","none")
        score         – Optional[float]
        fuzzy_calls   – int
        confidence    – "high" | "medium" | "low"
        overlap_score – Optional[float]  (Jaccard token overlap, or None)
        length_ratio  – Optional[float]  (max_len / min_len chars, or None)

    ``fuzzy_calls`` counts the number of actual fuzzy ratio evaluations
    executed (after length-ratio pre-filtering).

    Tiered matching — strict order, short-circuits on first match:

      Tier 1 – "exact":
        game_tail (lowercased) matches LR display name directly.
        confidence = "high".

      Tier 2 – "normalized":
        normalize_for_compare(game_tail) matches normalized LR name.
        Includes suffix/shape matching for short type-prefixed tails.
        confidence = "high".

      Tier 3 – "contains":
        Substring containment (either direction, >50 % length ratio).
        Best candidate selected by token-overlap scoring.
        confidence = "medium" when Jaccard >= 0.6 AND length_ratio <= 2.0,
        otherwise "low".

      Tier 4 – "fuzzy" (LAST RESORT):
        SequenceMatcher run only against a token-overlap reduced candidate
        set.  Falls back to full scan only when no token candidates exist.
        Score thresholds: >=0.75 to qualify.
        confidence = "medium" when score > 0.9, otherwise "low".

      "none":
        No acceptable match found.  confidence = "low".

    Uses precomputed LRMatchIndex — normalize_lr_name_for_match is never
    called here; all normalized names come from the index built at startup.
    """
    _NONE_RESULT: Dict[str, Any] = {
        "item": None,
        "tier": "none",
        "score": None,
        "fuzzy_calls": 0,
        "confidence": "low",
        "overlap_score": None,
        "length_ratio": None,
    }
    resolved_game_code = game_code or game_tail
    normalized_code_for_guard = normalize_mapping_game_code(resolved_game_code)
    if normalized_code_for_guard.startswith("item:metalplate-") or normalized_code_for_guard == "item:metalplate-*":
        if counters is not None:
            counters["force_unlinked"] = counters.get("force_unlinked", 0) + 1
        return dict(_NONE_RESULT)

    norm_tail = normalize_for_compare(game_tail)
    if not norm_tail:
        return dict(_NONE_RESULT)

    segments = [seg.strip() for seg in (game_tail or "").split("-") if seg.strip()]

    # ── Tier 1: Exact match (case-insensitive direct) ──────────────────
    lower_tail = (game_tail or "").lower().strip()
    exact_lc = lr_index.by_lowercase_name.get(lower_tail)
    if exact_lc:
        return {
            "item": exact_lc[0], "tier": "exact", "score": 1.0,
            "fuzzy_calls": 0, "confidence": "high",
            "overlap_score": None, "length_ratio": None,
        }

    # ── Tier 2: Normalized exact match ─────────────────────────────────
    # 2a: Direct normalized lookup
    norm_matches = lr_index.by_normalized_name.get(norm_tail)
    if norm_matches:
        return {
            "item": norm_matches[0], "tier": "normalized", "score": 1.0,
            "fuzzy_calls": 0, "confidence": "high",
            "overlap_score": None, "length_ratio": None,
        }

    # 2b: Suffix / shape match (short tails with type prefix)
    if segments and len(segments) <= 2:
        prefix = normalize_for_compare(segments[0]) if len(segments) == 2 else ""
        if prefix in SUFFIX_MATCH_PREFIXES:
            for token in SUFFIX_MATCH_PREFIXES:
                if prefix.endswith(token):
                    norm_full = normalize_for_compare(f"{segments[-1]}{token}")
                    shape_matches = lr_index.by_normalized_name.get(norm_full)
                    if shape_matches:
                        return {
                            "item": shape_matches[0], "tier": "normalized", "score": 1.0,
                            "fuzzy_calls": 0, "confidence": "high",
                            "overlap_score": None, "length_ratio": None,
                        }

            norm_suffix = normalize_for_compare(segments[-1])
            suffix_matches = lr_index.by_normalized_name.get(norm_suffix)
            if suffix_matches:
                return {
                    "item": suffix_matches[0], "tier": "normalized", "score": 1.0,
                    "fuzzy_calls": 0, "confidence": "high",
                    "overlap_score": None, "length_ratio": None,
                }

    # ── Tier 3: Contains / token-overlap ───────────────────────────────
    tail_tokens = {t.lower() for t in TOKEN_SPLIT_RE.split(game_tail) if t}

    best_contains_item: Optional[LRItem] = None
    best_contains_overlap = 0

    for item, norm_name in lr_index.items_with_norm:
        if not norm_name:
            continue

        is_contained = False
        if norm_name in norm_tail and (len(norm_name) / len(norm_tail)) > 0.50:
            is_contained = True
        elif norm_tail in norm_name and (len(norm_tail) / len(norm_name)) > 0.50:
            is_contained = True

        if is_contained:
            item_tokens = lr_index.token_sets.get(item.id, set())
            overlap = len(tail_tokens & item_tokens)
            if overlap > best_contains_overlap or (
                overlap == best_contains_overlap and best_contains_item is None
            ):
                best_contains_overlap = overlap
                best_contains_item = item

    if best_contains_item is not None:
        _co = _jaccard_overlap(game_tail, best_contains_item.display_name)
        _len_a = len(game_tail)
        _len_b = len(best_contains_item.display_name)
        _lr = (max(_len_a, _len_b) / min(_len_a, _len_b)) if min(_len_a, _len_b) > 0 else None

        # Guardrail 1: reject material-only contains matches with zero token overlap,
        # except single-token game tails.
        if len(tail_tokens) > 1 and _co == 0.0:
            return _guardrail_reject(
                game_code=resolved_game_code,
                tier="contains",
                lr_name=best_contains_item.display_name,
                rule=1,
                counters=counters,
                fuzzy_calls=0,
            )

        # Guardrail 3: reject severe contains length mismatch.
        if _lr is not None and _lr > 2.5:
            return _guardrail_reject(
                game_code=resolved_game_code,
                tier="contains",
                lr_name=best_contains_item.display_name,
                rule=3,
                counters=counters,
                fuzzy_calls=0,
            )

        _conf = "medium" if (_co >= 0.6 and _lr is not None and _lr <= 2.0) else "low"
        return {
            "item": best_contains_item, "tier": "contains", "score": 1.0,
            "fuzzy_calls": 0, "confidence": _conf,
            "overlap_score": _co, "length_ratio": _lr,
        }

    # ── Tier 4: Fuzzy match (LAST RESORT) ──────────────────────────────
    # Build reduced candidate set via shared tokens (track index for
    # precomputed norm_lengths / reusable_matchers access).
    norm_tail_len = len(norm_tail)
    candidate_idxs: List[int] = []
    for idx, (item, norm_name) in enumerate(lr_index.items_with_norm):
        if not norm_name:
            continue
        item_tokens = lr_index.token_sets.get(item.id, set())
        if tail_tokens & item_tokens:
            candidate_idxs.append(idx)

    # Fall back to full set only when no token-overlap candidates exist
    if not candidate_idxs:
        candidate_idxs = [
            idx for idx, (_, norm) in enumerate(lr_index.items_with_norm) if norm
        ]

    # Length-ratio early skip threshold: if one string is >2x the other,
    # no edit-distance scorer can produce a ratio >= 0.75 anyway — skip
    # the expensive comparison entirely.
    _LENGTH_RATIO_CUTOFF = 2.0

    best_item: Optional[LRItem] = None
    best_ratio = -1.0
    use_reusable = bool(lr_index.reusable_matchers)
    fuzzy_calls = 0

    for idx in candidate_idxs:
        cand_len = lr_index.norm_lengths[idx]
        # Early skip: length disparity too large for a >=0.75 match
        if cand_len == 0:
            continue
        if norm_tail_len > cand_len:
            if norm_tail_len > cand_len * _LENGTH_RATIO_CUTOFF:
                continue
        else:
            if cand_len > norm_tail_len * _LENGTH_RATIO_CUTOFF:
                continue

        # Score using optimal backend
        fuzzy_calls += 1
        if use_reusable:
            matcher = lr_index.reusable_matchers[idx]
            ratio = matcher.ratio(norm_tail) if matcher is not None else 0.0
        else:
            ratio = _fuzzy_ratio(norm_tail, lr_index.items_with_norm[idx][1])

        if ratio > best_ratio:
            best_ratio = ratio
            best_item = lr_index.items_with_norm[idx][0]

    if best_item is None:
        result = dict(_NONE_RESULT)
        result["fuzzy_calls"] = fuzzy_calls
        return result

    if best_ratio >= 0.75:
        _co = _jaccard_overlap(game_tail, best_item.display_name)
        _len_a = len(game_tail)
        _len_b = len(best_item.display_name)
        _lr = (max(_len_a, _len_b) / min(_len_a, _len_b)) if min(_len_a, _len_b) > 0 else None

        # Guardrail 2: reject weak fuzzy matches against parenthetical LR categories.
        if best_ratio < 0.82 and PAREN_CONTENT_RE.search(best_item.display_name):
            return _guardrail_reject(
                game_code=resolved_game_code,
                tier="fuzzy",
                lr_name=best_item.display_name,
                rule=2,
                counters=counters,
                fuzzy_calls=fuzzy_calls,
            )

        _conf = "medium" if best_ratio > 0.9 else "low"
        return {
            "item": best_item, "tier": "fuzzy", "score": float(best_ratio),
            "fuzzy_calls": fuzzy_calls, "confidence": _conf,
            "overlap_score": _co, "length_ratio": _lr,
        }

    result = dict(_NONE_RESULT)
    result["fuzzy_calls"] = fuzzy_calls
    return result


def load_candidates(cur) -> Tuple[List[str], List[str], List[LRItem], List[FTAItem]]:
    cur.execute(
        """
        SELECT DISTINCT output_game_code
        FROM recipes
        WHERE output_game_code IS NOT NULL
          AND btrim(output_game_code) <> ''
        ORDER BY output_game_code
        """
    )
    game_codes = [row[0] for row in cur.fetchall()]

    cur.execute(
        """
        SELECT DISTINCT input_game_code
        FROM recipe_ingredients
        WHERE input_game_code IS NOT NULL
          AND btrim(input_game_code) <> ''
        ORDER BY input_game_code
        """
    )
    ingredient_codes = [row[0] for row in cur.fetchall()]

    cur.execute(
        """
        SELECT item_id, id, display_name, lr_category, lr_sub_category
        FROM lr_items
        ORDER BY id
        """
    )
    lr_items = [
        LRItem(
            item_id=(row[0] or "").strip().upper(),
            id=row[1],
            display_name=row[2] or "",
            lr_category=row[3],
            lr_sub_category=row[4],
        )
        for row in cur.fetchall()
    ]

    fta_items: List[FTAItem] = []
    if table_exists(cur, "fta_items"):
        cur.execute(
            """
            SELECT id, display_name
            FROM fta_items
            WHERE display_name IS NOT NULL
              AND btrim(display_name) <> ''
            ORDER BY id
            """
        )
        fta_items = [FTAItem(id=row[0], display_name=row[1] or "") for row in cur.fetchall()]

    return game_codes, ingredient_codes, lr_items, fta_items


def insert_rows(cur, rows: Sequence[CanonicalRow]) -> None:
    cur.executemany(
        """
        INSERT INTO canonical_items (
            id,
            display_name,
            game_code,
            lr_item_id,
            fta_item_id,
            match_tier,
            match_score
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                row.slug,
                row.display_name,
                row.game_code,
                row.lr_item_id,
                row.fta_item_id,
                row.match_tier,
                row.match_score,
            )
            for row in rows
        ],
    )


def table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    row = cur.fetchone()
    return bool(row and row[0])


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


def apply_lang_display_name_overrides(rows: Sequence[CanonicalRow], lang_alias_map: Dict[str, str]) -> int:
    """
    Apply lang-file display name overrides for game-code-backed canonical rows.

    Priority policy:
      - exact/high LR display names win (no override)
      - lang overrides game-code-derived names
      - for low tier, preserve LR name if low-tier overlap logic selected LR display
    """
    overrides = 0
    for row in rows:
        game_code = row.game_code
        if not game_code:
            continue

        lang_name = lang_alias_map.get(game_code)
        if not lang_name:
            continue

        tier = (row.match_tier or "").lower()
        if tier in {"exact", "high"}:
            continue

        if tier == "low":
            # Only override low tier when current display_name is still game-code-derived.
            tail = game_code_tail(game_code)
            if row.display_name != humanize_game_tail(tail):
                continue

        if row.display_name == lang_name:
            continue

        row.display_name = lang_name
        overrides += 1

    return overrides


def snapshot_price_overrides(cur) -> List[Tuple[Optional[str], Optional[str], float, Optional[str]]]:
    """Capture manual overrides so canonical rebuild can safely recreate referenced IDs."""
    if not table_exists(cur, "price_overrides"):
        return []

    cur.execute(
        """
        SELECT po.canonical_id, ci.game_code, po.unit_price, po.note
        FROM price_overrides po
        LEFT JOIN canonical_items ci ON ci.id = po.canonical_id
        """
    )
    rows = cur.fetchall()

    # canonical_items rebuild currently deletes rows; clear dependent FK rows first.
    cur.execute("DELETE FROM price_overrides")
    return rows


def restore_price_overrides(cur, snapshots: Sequence[Tuple[Optional[str], Optional[str], float, Optional[str]]]) -> None:
    if not snapshots or not table_exists(cur, "price_overrides"):
        return

    restored = 0
    skipped = 0

    for old_canonical_id, old_game_code, unit_price, note in snapshots:
        new_canonical_id: Optional[str] = None

        if old_game_code:
            normalized_code = normalize_game_code(old_game_code)
            cur.execute(
                "SELECT id FROM canonical_items WHERE game_code = %s LIMIT 1",
                (normalized_code,),
            )
            row = cur.fetchone()
            if row:
                new_canonical_id = row[0]

        if new_canonical_id is None and old_canonical_id:
            cur.execute(
                "SELECT id FROM canonical_items WHERE id = %s LIMIT 1",
                (old_canonical_id,),
            )
            row = cur.fetchone()
            if row:
                new_canonical_id = row[0]

        if new_canonical_id is None:
            skipped += 1
            continue

        cur.execute(
            """
            INSERT INTO price_overrides (canonical_id, unit_price, note)
            VALUES (%s, %s, %s)
            ON CONFLICT (canonical_id)
            DO UPDATE SET unit_price = EXCLUDED.unit_price, note = EXCLUDED.note
            """,
            (new_canonical_id, unit_price, note),
        )
        restored += 1

    print(f"Price overrides restored: {restored}")
    print(f"Price overrides skipped (unmapped): {skipped}")


def ensure_variant_family_columns(cur) -> None:
    """Ensure variant-family columns exist on canonical_items."""
    cur.execute("ALTER TABLE canonical_items ADD COLUMN IF NOT EXISTS variant_family TEXT")
    cur.execute("ALTER TABLE canonical_items ADD COLUMN IF NOT EXISTS variant_material TEXT")


def assign_variant_families(cur) -> int:
    """Populate variant_family/material for metal-suffixed item:<base>-<material> codes."""
    cur.execute(
        """
        SELECT id, game_code
        FROM canonical_items
        WHERE game_code IS NOT NULL
          AND game_code LIKE 'item:%'
        """
    )
    rows = cur.fetchall()

    updates: List[Tuple[str, str, str]] = []
    for canonical_id, game_code in rows:
        tail = game_code_tail(game_code)
        if "-" not in tail:
            continue

        base, material = tail.rsplit("-", 1)
        material = (material or "").lower().strip()
        if not base or material not in METAL_VARIANT_MATERIALS:
            continue

        updates.append((base, material, canonical_id))

    if not updates:
        return 0

    cur.executemany(
        """
        UPDATE canonical_items
        SET variant_family = %s,
            variant_material = %s
        WHERE id = %s
        """,
        updates,
    )
    return len(updates)


def _evaluate_anomaly_reasons(
    *,
    internal_tier: str,
    score: Optional[float],
    confidence: str,
    overlap_score: Optional[float],
    length_ratio: Optional[float],
) -> List[str]:
    """Return list of anomaly reason labels triggered for a match result.

    Conditions are evaluated independently — a single match may trigger
    multiple reasons.  Returns an empty list when no anomaly conditions fire.
    """
    reasons: List[str] = []
    if internal_tier == "contains" and overlap_score is not None and overlap_score < 0.5:
        reasons.append("contains_low_overlap")
    if internal_tier == "fuzzy" and score is not None and score < 0.8:
        reasons.append("fuzzy_low_score")
    if length_ratio is not None and length_ratio > 2.0:
        reasons.append("length_mismatch")
    if confidence == "low" and internal_tier not in ("none", "fuzzy"):
        reasons.append("low_confidence_nonfuzzy")
    return reasons


def append_canonical_row_for_game_code(
    *,
    game_code: str,
    rows: List[CanonicalRow],
    seen_slugs: Set[str],
    lr_index: LRMatchIndex,
    matched_lr_ids: Set[int],
    mapping_reverse_index: Dict[str, Tuple[int, str]],
    force_unlinked_codes: Set[str],
    counters: Dict[str, int],
) -> Optional[Dict[str, Any]]:
    """Append a canonical row and return match metadata for anomaly evaluation.

    Returns None for force-unlinked / mapped paths (no anomaly candidate).
    Returns a dict with match metadata for all choose_best_lr_match paths.
    """
    tail = game_code_tail(game_code)
    base_slug = slug_from_game_code(game_code)
    slug = disambiguate_slug(base_slug, seen_slugs)
    normalized_code = normalize_mapping_game_code(game_code)

    if normalized_code in force_unlinked_codes:
        counters["force_unlinked"] += 1
        counters["unmatched_game_codes"] += 1
        rows.append(
            CanonicalRow(
                slug=slug,
                display_name=humanize_game_tail(tail),
                game_code=game_code,
                lr_item_id=None,
                fta_item_id=None,
                match_tier="unmatched",
                match_score=None,
            )
        )
        return

    mapped = mapping_reverse_index.get(normalized_code)
    if mapped is not None:
        mapped_lr_id, mapped_lr_display = mapped
        counters["mapped_matches"] += 1
        matched_lr_ids.add(mapped_lr_id)
        display_name = canonical_display_name_for_match(
            tier="exact",
            tail=tail,
            lr_display_name=mapped_lr_display,
        )
        rows.append(
            CanonicalRow(
                slug=slug,
                display_name=display_name,
                game_code=game_code,
                lr_item_id=mapped_lr_id,
                fta_item_id=None,
                match_tier="mapped",
                match_score=1.0,
            )
        )
        return

    match_result = choose_best_lr_match(
        tail,
        lr_index,
        game_code=game_code,
        counters=counters,
    )
    best_item = match_result["item"]
    tier = match_result["tier"]
    score = match_result["score"]
    call_fuzzy_calls = match_result["fuzzy_calls"]
    confidence = match_result["confidence"]
    overlap_score = match_result["overlap_score"]
    length_ratio = match_result["length_ratio"]
    counters["fuzzy_calls"] = counters.get("fuzzy_calls", 0) + call_fuzzy_calls
    counters["total_items"] = counters.get("total_items", 0) + 1
    counters[f"confidence_{confidence}"] = counters.get(f"confidence_{confidence}", 0) + 1
    if tier in ("contains", "fuzzy"):
        counters[f"{tier}_confidence_{confidence}"] = counters.get(f"{tier}_confidence_{confidence}", 0) + 1

    # Determine db_tier and append canonical row
    db_tier: Optional[str] = None
    matched_lr_name: Optional[str] = None

    # Tiers "exact", "normalized", "contains" all map to DB match_tier="exact"
    if tier in ("exact", "normalized", "contains") and best_item is not None:
        db_tier = "exact"
        matched_lr_name = best_item.display_name
        counters[f"{tier}_matches"] += 1
        matched_lr_ids.add(best_item.id)
        display_name = canonical_display_name_for_match(
            tier="exact",
            tail=tail,
            lr_display_name=best_item.display_name,
        )
        rows.append(
            CanonicalRow(
                slug=slug,
                display_name=display_name,
                game_code=game_code,
                lr_item_id=best_item.id,
                fta_item_id=None,
                match_tier="exact",
                match_score=1.0,
            )
        )
    elif tier == "fuzzy" and best_item is not None and score is not None:
        # Classify fuzzy score into high/low DB tiers
        if score >= 0.90:
            db_tier = "high"
            counters["fuzzy_high_matches"] += 1
        else:
            db_tier = "low"
            counters["fuzzy_low_matches"] += 1
        matched_lr_name = best_item.display_name
        matched_lr_ids.add(best_item.id)
        display_name = canonical_display_name_for_match(
            tier=db_tier,
            tail=tail,
            lr_display_name=best_item.display_name,
        )
        rows.append(
            CanonicalRow(
                slug=slug,
                display_name=display_name,
                game_code=game_code,
                lr_item_id=best_item.id,
                fta_item_id=None,
                match_tier=db_tier,
                match_score=score,
            )
        )
    else:
        db_tier = "unmatched"
        counters["unmatched_game_codes"] += 1
        rows.append(
            CanonicalRow(
                slug=slug,
                display_name=humanize_game_tail(tail),
                game_code=game_code,
                lr_item_id=None,
                fta_item_id=None,
                match_tier="unmatched",
                match_score=None,
            )
        )

    return {
        "game_code": game_code,
        "matched_lr_name": matched_lr_name,
        "internal_tier": tier,
        "db_tier": db_tier,
        "score": score,
        "confidence": confidence,
        "overlap_score": overlap_score,
        "length_ratio": length_ratio,
    }


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    t0 = time.perf_counter()

    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                ensure_match_tier_allowed(cur)
                ensure_variant_family_columns(cur)
                game_codes, ingredient_codes, lr_items, fta_items = load_candidates(cur)
                lr_index = build_lr_match_index(lr_items)
                mapping_reverse_index, force_unlinked_codes, mapping_active, mapping_warnings = load_lr_item_mapping(
                    cur,
                    lr_items=lr_items,
                )
                lang_alias_map = load_lang_alias_map()
                price_override_snapshots = snapshot_price_overrides(cur)

                if not mapping_active:
                    print(
                        "[WARN] *** MAPPING FILE NOT ACTIVE — all LR linkage using fallback fuzzy ***"
                    )
                elif mapping_warnings.get("missing_lr_item_ids"):
                    print(
                        "[WARN] Mapping references LR item_id values not found in DB: "
                        f"{mapping_warnings.get('missing_lr_item_ids')}"
                    )

                cur.execute("UPDATE recipe_ingredients SET input_canonical_id = NULL")
                cur.execute("UPDATE recipes SET output_canonical_id = NULL")
                # NOTE: TRUNCATE is blocked while FK constraints reference this table,
                # even when all referencing values are NULL. Use DELETE for safe rebuild.
                cur.execute("DELETE FROM canonical_items")

                rows: List[CanonicalRow] = []
                seen_slugs: Set[str] = set()
                matched_lr_ids: Set[int] = set()

                counters: Dict[str, int] = {
                    "exact_matches": 0,
                    "normalized_matches": 0,
                    "contains_matches": 0,
                    "fuzzy_high_matches": 0,
                    "fuzzy_low_matches": 0,
                    "mapped_matches": 0,
                    "force_unlinked": 0,
                    "unmatched_game_codes": 0,
                    "guardrail_rule_1_rejections": 0,
                    "guardrail_rule_2_rejections": 0,
                    "guardrail_rule_3_rejections": 0,
                }

                # Anomaly candidate metadata collected from Source A + C
                match_metadata_list: List[Dict[str, Any]] = []

                # Source A: game-code-driven rows (matched or unmatched)
                processed_game_codes: Set[str] = set()
                for game_code_raw in game_codes:
                    game_code = normalize_game_code(game_code_raw)
                    if not game_code or game_code in processed_game_codes:
                        continue
                    processed_game_codes.add(game_code)
                    meta = append_canonical_row_for_game_code(
                        game_code=game_code,
                        rows=rows,
                        seen_slugs=seen_slugs,
                        lr_index=lr_index,
                        matched_lr_ids=matched_lr_ids,
                        mapping_reverse_index=mapping_reverse_index,
                        force_unlinked_codes=force_unlinked_codes,
                        counters=counters,
                    )
                    if meta is not None:
                        match_metadata_list.append(meta)

                # Source B: LR-only rows (not matched by any game code)
                lr_only_items = 0
                for item in lr_items:
                    if item.id in matched_lr_ids:
                        continue

                    base_slug = slug_from_display_name(item.display_name)
                    if not base_slug:
                        base_slug = f"lr_item_{item.id}"
                    slug = disambiguate_slug(base_slug, seen_slugs)

                    rows.append(
                        CanonicalRow(
                            slug=slug,
                            display_name=strip_admin_note_parentheticals(item.display_name) or f"LR Item {item.id}",
                            game_code=None,
                            lr_item_id=item.id,
                            fta_item_id=None,
                            match_tier=None,
                            match_score=None,
                        )
                    )
                    lr_only_items += 1

                # Source C: ingredient-only game codes not already represented by Source A/B canonical rows.
                ingredient_only_items = 0
                existing_canonical_codes: Set[str] = {
                    row.game_code for row in rows if row.game_code is not None
                }

                for ingredient_code_raw in ingredient_codes:
                    # Skip templates and mod schematic-like codes.
                    if "{" in ingredient_code_raw:
                        continue

                    ingredient_code = normalize_game_code(ingredient_code_raw)
                    if not ingredient_code:
                        continue
                    if ingredient_code in existing_canonical_codes:
                        continue

                    existing_canonical_codes.add(ingredient_code)
                    ingredient_only_items += 1
                    meta = append_canonical_row_for_game_code(
                        game_code=ingredient_code,
                        rows=rows,
                        seen_slugs=seen_slugs,
                        lr_index=lr_index,
                        matched_lr_ids=matched_lr_ids,
                        mapping_reverse_index=mapping_reverse_index,
                        force_unlinked_codes=force_unlinked_codes,
                        counters=counters,
                    )
                    if meta is not None:
                        match_metadata_list.append(meta)

                # Source D: FTA linking pass (match FTA display names to existing canonical display names).
                fta_exact_links = 0
                fta_trigram_links = 0
                fta_only_items = 0

                # Pre-index canonical rows for faster FTA linking.
                canonical_name_norms: List[str] = [
                    normalize_name_for_linking(row.display_name) for row in rows
                ]
                canonical_trigrams: List[Set[str]] = [trigram_set(row.display_name) for row in rows]
                trigram_to_indexes: Dict[str, Set[int]] = {}
                exact_name_to_indexes: Dict[str, List[int]] = {}
                for idx, name_norm in enumerate(canonical_name_norms):
                    if not name_norm:
                        continue
                    exact_name_to_indexes.setdefault(name_norm, []).append(idx)
                    for tri in canonical_trigrams[idx]:
                        trigram_to_indexes.setdefault(tri, set()).add(idx)

                for fta_item in fta_items:
                    fta_name_norm = normalize_name_for_linking(fta_item.display_name)
                    if not fta_name_norm:
                        continue

                    matched_index: Optional[int] = None

                    # Exact display_name match first.
                    for idx in exact_name_to_indexes.get(fta_name_norm, []):
                        if rows[idx].fta_item_id is not None:
                            continue
                        matched_index = idx
                        break

                    # Trigram similarity fallback (0.5+).
                    if matched_index is None:
                        best_idx: Optional[int] = None
                        best_score = 0.0
                        fta_trigrams = trigram_set(fta_item.display_name)

                        candidate_indexes: Set[int] = set()
                        for tri in fta_trigrams:
                            candidate_indexes.update(trigram_to_indexes.get(tri, set()))

                        for idx in candidate_indexes:
                            row = rows[idx]
                            if row.fta_item_id is not None:
                                continue
                            score = trigram_similarity_sets(fta_trigrams, canonical_trigrams[idx])
                            if score > best_score:
                                best_score = score
                                best_idx = idx

                        if best_idx is not None and best_score >= 0.5:
                            matched_index = best_idx
                            fta_trigram_links += 1

                    if matched_index is not None:
                        rows[matched_index].fta_item_id = fta_item.id
                        if normalize_name_for_linking(rows[matched_index].display_name) == fta_name_norm:
                            fta_exact_links += 1
                        continue

                    # FTA-only: create new canonical when no match exists.
                    base_slug = slug_from_display_name(fta_item.display_name) or f"fta_item_{fta_item.id}"
                    slug = disambiguate_slug(base_slug, seen_slugs)
                    rows.append(
                        CanonicalRow(
                            slug=slug,
                            display_name=fta_item.display_name,
                            game_code=None,
                            lr_item_id=None,
                            fta_item_id=fta_item.id,
                            match_tier=None,
                            match_score=None,
                        )
                    )
                    new_idx = len(rows) - 1
                    canonical_name_norms.append(fta_name_norm)
                    new_trigrams = trigram_set(fta_item.display_name)
                    canonical_trigrams.append(new_trigrams)
                    exact_name_to_indexes.setdefault(fta_name_norm, []).append(new_idx)
                    for tri in new_trigrams:
                        trigram_to_indexes.setdefault(tri, set()).add(new_idx)
                    fta_only_items += 1

                lang_overrides = apply_lang_display_name_overrides(rows, lang_alias_map)

                insert_rows(cur, rows)
                variant_family_count = assign_variant_families(cur)
                restore_price_overrides(cur, price_override_snapshots)

                print(f"  Fuzzy backend: {_FUZZY_BACKEND}")
                print(f"Total canonical items created: {len(rows)}")
                print(f"  Exact matches (tier 1): {counters['exact_matches']}")
                print(f"  Normalized matches (tier 2): {counters['normalized_matches']}")
                print(f"  Contains matches (tier 3): {counters['contains_matches']}")
                print(f"  Fuzzy high matches (tier 4, >=0.90): {counters['fuzzy_high_matches']}")
                print(f"  Fuzzy low matches (tier 4, 0.75-0.89): {counters['fuzzy_low_matches']}")
                print(f"  Mapped matches: {counters['mapped_matches']}")
                print(f"  Force-unlinked entries: {counters['force_unlinked']}")
                print(f"  Unmatched game codes: {counters['unmatched_game_codes']}")
                print(f"  LR-only items: {lr_only_items}")
                print(f"  Ingredient-only items: {ingredient_only_items}")
                print(f"  FTA exact display-name links: {fta_exact_links}")
                print(f"  FTA trigram links: {fta_trigram_links}")
                print(f"  FTA-only items: {fta_only_items}")
                print(f"  Lang-derived game-code labels loaded: {len(lang_alias_map)}")
                print(f"  Lang display_name overrides applied: {lang_overrides}")
                print(f"  Variant-family rows assigned: {variant_family_count}")
                tier_counts = Counter((row.match_tier or "none") for row in rows)
                print("  Match-tier distribution:")
                for tier in sorted(tier_counts):
                    print(f"    - {tier}: {tier_counts[tier]}")
                if not mapping_active:
                    print("  ⚠ Mapping file was NOT active — 0 mapped links produced")
                elif mapping_warnings.get("missing_lr_item_ids"):
                    print(
                        "  ⚠ Mapping references missing LR item_id values: "
                        f"{mapping_warnings.get('missing_lr_item_ids')}"
                    )

                cur.execute("SELECT COUNT(*) FROM recipes WHERE output_canonical_id IS NULL")
                recipe_unlinked_count_row = cur.fetchone()
                recipe_unlinked_count = int(recipe_unlinked_count_row[0]) if recipe_unlinked_count_row else 0
                if recipe_unlinked_count > 100:
                    print(
                        f"WARNING: Recipe linkage appears broken - {recipe_unlinked_count} recipes have no canonical link. "
                        "Run: python scripts/link_recipes.py"
                    )

                print("[OK] Done.")

                # ── Instrumentation summary ────────────────────────────
                runtime_seconds = round(time.perf_counter() - t0, 3)
                total_items = counters.get("total_items", 0)
                i_exact = counters["exact_matches"]
                i_normalized = counters["normalized_matches"]
                i_contains = counters["contains_matches"]
                i_fuzzy = counters["fuzzy_high_matches"] + counters["fuzzy_low_matches"]
                i_none = counters["unmatched_game_codes"]
                i_fuzzy_calls = counters.get("fuzzy_calls", 0)

                summary = {
                    "total_items": total_items,
                    "exact": i_exact,
                    "normalized": i_normalized,
                    "contains": i_contains,
                    "fuzzy": i_fuzzy,
                    "none": i_none,
                    "fuzzy_calls": i_fuzzy_calls,
                    "runtime_seconds": runtime_seconds,
                    "confidence": {
                        "high": counters.get("confidence_high", 0),
                        "medium": counters.get("confidence_medium", 0),
                        "low": counters.get("confidence_low", 0),
                    },
                    "contains_confidence": {
                        "medium": counters.get("contains_confidence_medium", 0),
                        "low": counters.get("contains_confidence_low", 0),
                    },
                    "fuzzy_confidence": {
                        "medium": counters.get("fuzzy_confidence_medium", 0),
                        "low": counters.get("fuzzy_confidence_low", 0),
                    },
                    "guardrail_rejections": {
                        "rule_1": counters.get("guardrail_rule_1_rejections", 0),
                        "rule_2": counters.get("guardrail_rule_2_rejections", 0),
                        "rule_3": counters.get("guardrail_rule_3_rejections", 0),
                        "total": (
                            counters.get("guardrail_rule_1_rejections", 0)
                            + counters.get("guardrail_rule_2_rejections", 0)
                            + counters.get("guardrail_rule_3_rejections", 0)
                        ),
                    },
                }
                print("\n--- Instrumentation Summary ---")
                print(json.dumps(summary, indent=2))

                # Goal check: fuzzy should be <10% of total matched items
                total_matched = i_exact + i_normalized + i_contains + i_fuzzy
                if total_matched > 0:
                    fuzzy_pct = (i_fuzzy / total_matched) * 100.0
                    status = "PASS" if fuzzy_pct < 10.0 else "FAIL"
                    print(f"  fuzzy_share: {fuzzy_pct:.1f}% of matched items [{status} — target <10%]")
                else:
                    print("  fuzzy_share: N/A (no matched items)")

                # ── Match anomaly persistence ──────────────────────────
                anomaly_records: List[Dict[str, Any]] = []
                total_processed = len(match_metadata_list)

                for meta in match_metadata_list:
                    # Exempt: mapped / manual DB tiers never flagged
                    if (meta["db_tier"] or "") in _ANOMALY_EXEMPT_DB_TIERS:
                        continue

                    reasons = _evaluate_anomaly_reasons(
                        internal_tier=meta["internal_tier"],
                        score=meta["score"],
                        confidence=meta["confidence"],
                        overlap_score=meta["overlap_score"],
                        length_ratio=meta["length_ratio"],
                    )
                    if not reasons:
                        continue

                    anomaly_records.append({
                        "game_code": meta["game_code"],
                        "matched_lr_name": meta["matched_lr_name"],
                        "internal_tier": meta["internal_tier"],
                        "db_tier": meta["db_tier"],
                        "score": meta["score"],
                        "confidence": meta["confidence"],
                        "overlap_score": round(meta["overlap_score"], 4) if meta["overlap_score"] is not None else None,
                        "length_ratio": round(meta["length_ratio"], 4) if meta["length_ratio"] is not None else None,
                        "reasons": reasons,
                    })

                total_flagged = len(anomaly_records)
                flagged_pct = round((total_flagged / total_processed) * 100.0, 2) if total_processed > 0 else 0.0

                anomaly_payload: Dict[str, Any] = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "total_processed": total_processed,
                    "total_flagged": total_flagged,
                    "flagged_pct": flagged_pct,
                    "anomalies": anomaly_records,
                }

                os.makedirs(os.path.dirname(MATCH_ANOMALIES_PATH), exist_ok=True)
                with open(MATCH_ANOMALIES_PATH, "w", encoding="utf-8") as f:
                    json.dump(anomaly_payload, f, indent=2, ensure_ascii=False)

                print(f"\n--- Match Anomaly Report ---")
                print(f"Total processed : {total_processed}")
                print(f"Flagged         : {total_flagged} ({flagged_pct:.2f}%)")
                print(f"Written to      : {MATCH_ANOMALIES_PATH}")

    except Exception as exc:
        print(f"[ERROR] Failed to build canonical_items: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
