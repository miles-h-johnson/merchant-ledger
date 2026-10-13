-- Adds is_buyable flag from Buying? column in new LR workbook.
-- Items with is_buyable=FALSE still have prices (used by derivation rules)
-- but the Empire does not purchase them directly.
ALTER TABLE lr_items
ADD COLUMN IF NOT EXISTS is_buyable BOOLEAN NOT NULL DEFAULT TRUE;