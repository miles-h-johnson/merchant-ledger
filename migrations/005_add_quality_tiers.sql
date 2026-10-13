ALTER TABLE lr_items ADD COLUMN lr_sub_category TEXT;
ALTER TABLE lr_items ADD COLUMN has_quality_tiers BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE lr_items ADD COLUMN price_uncommon_current NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_uncommon_industrial_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_uncommon_industrial_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_uncommon_market_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_uncommon_market_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_uncommon_religious_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_uncommon_temple_city NUMERIC;

ALTER TABLE lr_items ADD COLUMN price_rare_current NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_rare_industrial_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_rare_industrial_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_rare_market_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_rare_market_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_rare_religious_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_rare_temple_city NUMERIC;

ALTER TABLE lr_items ADD COLUMN price_epic_current NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_epic_industrial_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_epic_industrial_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_epic_market_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_epic_market_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_epic_religious_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_epic_temple_city NUMERIC;

ALTER TABLE lr_items ADD COLUMN price_legendary_current NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_legendary_industrial_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_legendary_industrial_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_legendary_market_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_legendary_market_city NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_legendary_religious_town NUMERIC;
ALTER TABLE lr_items ADD COLUMN price_legendary_temple_city NUMERIC;

ALTER TABLE lr_items ADD COLUMN unit_price_uncommon_current NUMERIC GENERATED ALWAYS AS (price_uncommon_current / count) STORED;