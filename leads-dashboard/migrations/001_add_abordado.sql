-- Execute no Postgres (ajuste o nome do schema se necessário).
-- Ex.: psql -U postgres -d sua_base -f migrations/001_add_abordado.sql

ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS abordado boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_leads_abordado ON leads (abordado);
