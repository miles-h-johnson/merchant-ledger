#!/usr/bin/env python3
import os
import subprocess
import sys

import psycopg2
from dotenv import load_dotenv


SCRIPTS = [
    "scripts/ingest_lr_prices.py",
    "scripts/compute_primitive_prices.py",
    "scripts/parse_recipes_json.py",
    "scripts/build_canonical_items.py",
    "scripts/apply_manual_lr_links.py",
    "scripts/build_aliases.py",
    "scripts/link_recipes.py",
]


QUERIES = {
    "lr_items_loaded": "SELECT COUNT(*) FROM lr_items",
    "canonical_with_lr_link": "SELECT COUNT(*) FROM canonical_items WHERE lr_item_id IS NOT NULL",
}


def run_script(path: str) -> None:
    print(f"\n=== Running: python {path} ===")
    completed = subprocess.run([sys.executable, path], check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Script failed ({completed.returncode}): {path}")


def run_query() -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not set")

    print("\n=== Verification query results ===")
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cur:
            for label, sql in QUERIES.items():
                cur.execute(sql)
                value = cur.fetchone()[0]
                print(f"{label}: {value}")


def main() -> int:
    try:
        for script in SCRIPTS:
            run_script(script)
        run_query()
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        return 1

    print("\n[OK] Pipeline + verification complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())