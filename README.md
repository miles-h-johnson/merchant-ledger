# Merchant Ledger

A pricing service for a player-run economy in the game Vintage Story. Type an order like `12 iron ingot, 4 bread` and it returns a cost for each line. An item uses the server's official list price when it has one. Otherwise its cost is built up from its crafting recipe, recursively, down to items that do have prices.

The hard part is that the two data sources don't share identifiers. The server's price list uses free-text names ("Iron Ingot (Rare)"), while the game's recipes use internal codes (`game:ingot-iron`), and installed mods add more recipes on top. The pipeline reconciles them into one catalogue of canonical items before anything can be priced.

## Pipeline

`run_pipeline.py` runs these steps in order against PostgreSQL:

1. `ingest_lr_prices.py`: loads the price workbook, including per-settlement prices and quality tiers.
2. `compute_primitive_prices.py`: derives fallback override prices for basic materials from fixed rules based on the price list.
3. `parse_recipes_json.py`: reads every recipe JSON5 file from the game install and the unpacked mod cache, with one handler per recipe type (grid, smithing, barrel, cooking, alloy, clayforming, knapping).
4. `build_canonical_items.py`: builds the canonical item table from recipe outputs and price-list names, matching names to game codes with fuzzy string matching (rapidfuzz) and the game's English language files. It writes anomalies and low-confidence matches to `data/` for review.
5. `apply_manual_lr_links.py`: applies hand-checked links for known misses.
6. `build_aliases.py`: generates search aliases for each item.
7. `link_recipes.py`: links recipe ingredients to canonical items and marks the primary variant where a recipe accepts several.

`ingest_settlement_multipliers.py` loads per-settlement price multipliers from a published spreadsheet and is run separately.

## Resolver

`scripts/resolver.py` prices one item in this order:

1. Earlier result for the same item and settlement in this request (memoised, then scaled by quantity).
2. The list price for the settlement type, with quality-tier prices where they exist.
3. A manual override price.
4. A crafting recipe. Each ingredient is priced recursively, wildcard ingredients (currently `clay-*`) resolve to their cheapest priced variant, and tools that are not consumed are excluded. Among complete recipes it prefers ones matching a requested material, then the lowest cost.

A visited set stops recipe cycles. An item reached again during its own resolution returns its override price if one exists, and otherwise is marked `circular_reference`. A recipe with unresolved ingredients is never chosen over a complete one; if none is complete, the most complete recipe is returned flagged `is_partial`, with its resolved and total ingredient counts. An optional 20% labour markup can be applied.

Order lines are matched to items with PostgreSQL trigram similarity (`pg_trgm`) over the alias table, preferring exact matches.

## API

Flask app in `api/app.py`, which also serves the built React front end from `webapp/dist`.

- `POST /calculate` with `{"order": "12 iron ingot, 4 bread", "settlement_type": "market_town", "labor_markup": false}`
- `GET /search?q=iron&limit=10`
- `GET /health`

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env              # set DATABASE_URL and the game paths
psql "$DATABASE_URL" -f migrations/001_create_schema.sql   # then the remaining files in order
python run_pipeline.py
python api/app.py
cd webapp && npm install && npm run dev
```

The price workbook belongs to the server and is not included. `data/lr_item_mapping.json` holds the hand-maintained name-to-code mappings the pipeline reads.

## Deployment

It was deployed on Railway: `nixpacks.toml` builds the front end and installs the Python dependencies, and `railway.toml` health-checks `/health`.

Imported from a private repository; earlier history and development diagnostics are not included.
