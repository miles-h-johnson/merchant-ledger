#!/usr/bin/env python3
"""
Ingest Settlement Specialization multipliers into settlement_multipliers.

Reads DATABASE_URL from environment and upserts rows from the published
Google Sheets CSV endpoint.
"""

import csv
import io
import os
import sys
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

import psycopg2
from dotenv import load_dotenv


load_dotenv()


SOURCE_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vR73BLsil5C-xCp_P-_VCMWhSRliwou5FWfyhMmAbOTc4a91OcwpkxC0J_R0tdmuYilv8_OVWtJqtlj/"
    "pub?gid=174237392&single=true&output=csv"
)

CATEGORY_COLUMNS: List[Tuple[str, int]] = [
    ("Agricultural Goods", 2),
    ("Industrial Goods", 3),
    ("Artisanal Goods", 4),
]


def fetch_csv(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def normalize_cell(value: Optional[str]) -> str:
    return (value or "").strip()


def parse_numeric(value: Optional[str]) -> Optional[Decimal]:
    text = normalize_cell(value)
    if text in ("", "-"):
        return None

    text = text.replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid multiplier value: {value!r}") from exc


def get_col(row: List[str], idx: int) -> str:
    if idx < len(row):
        return normalize_cell(row[idx])
    return ""


def parse_rows(csv_text: str) -> Tuple[List[Dict[str, object]], int, int]:
    """
    Parse specialization CSV rows into upsert records.

    Returns:
      (records, parsed_source_rows, skipped_rows)
    """
    reader = csv.reader(io.StringIO(csv_text))
    records: List[Dict[str, object]] = []
    parsed_source_rows = 0
    skipped_rows = 0

    for line_no, row in enumerate(reader, start=1):
        # Skip header row
        if line_no == 1:
            continue

        settlement_type = get_col(row, 0)

        # Skip rows where settlement_type (column 0) is blank
        if settlement_type == "":
            skipped_rows += 1
            continue

        try:
            for category, col_idx in CATEGORY_COLUMNS:
                records.append(
                    {
                        "settlement_type": settlement_type,
                        "category": category,
                        "multiplier": parse_numeric(get_col(row, col_idx)),
                    }
                )
            parsed_source_rows += 1
        except ValueError as err:
            skipped_rows += 1
            print(
                f"[WARN] Skipping malformed row (line {line_no}): {err}",
                file=sys.stderr,
            )

    return records, parsed_source_rows, skipped_rows


UPSERT_SQL = """
INSERT INTO settlement_multipliers (
    settlement_type,
    category,
    multiplier
) VALUES (
    %(settlement_type)s,
    %(category)s,
    %(multiplier)s
)
ON CONFLICT (settlement_type, category) DO UPDATE
SET
    multiplier = EXCLUDED.multiplier
RETURNING (xmax = 0) AS inserted;
"""


def ingest_rows(cur, rows: List[Dict[str, object]]) -> Tuple[int, int]:
    inserted = 0
    updated = 0

    for row in rows:
        cur.execute(UPSERT_SQL, row)
        was_inserted = cur.fetchone()[0]
        if was_inserted:
            inserted += 1
        else:
            updated += 1

    return inserted, updated


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    print("Starting settlement multiplier ingestion...")
    print(f"Fetching CSV from: {SOURCE_URL}")

    try:
        csv_text = fetch_csv(SOURCE_URL)
    except Exception as exc:
        print(f"[ERROR] Failed to fetch CSV: {exc}", file=sys.stderr)
        return 1

    records, parsed_source_rows, skipped_rows = parse_rows(csv_text)
    print(
        f"Parsed {parsed_source_rows} specialization rows into {len(records)} upsert records"
        + (f" (skipped {skipped_rows})" if skipped_rows else "")
    )

    if not records:
        print("[WARN] No records parsed; nothing to ingest.")
        return 0

    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                inserted, updated = ingest_rows(cur, records)
                print(f"Ingestion summary: inserted={inserted}, updated={updated}")
    except Exception as exc:
        print(f"[ERROR] Database ingestion failed: {exc}", file=sys.stderr)
        return 1

    print("[OK] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
