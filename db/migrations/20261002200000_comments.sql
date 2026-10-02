-- migrate:up

-- Self-documentation, visible with \dt+ / \d+ in psql and in GUI clients.
-- See docs/ingestion.md for the full pipeline description.

comment on table fund is 'FII registry. Source: CVM informe mensal (geral CSV); latest report wins.';
comment on column fund.cnpj is 'Fund (or class) CNPJ, digits only. Natural key across CVM files.';
comment on column fund.type is 'FII | FIAGRO | FIINFRA. Only FII is ingested for now.';
comment on column fund.isin is 'ISIN as self-reported to CVM. Often missing or wrong; see security.link_method.';
comment on column fund.segment is 'CVM Segmento_Atuacao (self-reported, coarse: most say Multicategoria). Prefer fund_profile.segment.';
comment on column fund.listed is 'CVM Mercado_Negociacao_Bolsa (S/N).';
comment on column fund.last_report_month is 'Month of the report the registry fields came from.';

comment on table security is 'Tickers traded at B3. Source: COTAHIST (BDI 12 = FII). fund_id is resolved by the pipeline.';
comment on column security.isin is 'ISIN from COTAHIST (CODISI) on the most recent trade.';
comment on column security.bdi_code is 'B3 BDI code: 12 = FII; 14 = Fiagro/FI-Infra/ETFs/FIPs (not ingested yet).';
comment on column security.fund_id is 'Linked fund. Null when no evidence matched (see link_method).';
comment on column security.link_method is 'How fund_id was resolved: fnet (FundosNET notice, strongest) > isin (exact) > isin_issuer (ISIN chars 3-6).';

comment on table quote_daily is 'Daily OHLC per ticker. Source: B3 COTAHIST (yearly files for closed years, daily files after). Not adjusted.';
comment on column quote_daily.close is 'Closing price in BRL (PREULT / quote factor).';
comment on column quote_daily.avg is 'Volume-weighted average price (PREMED).';
comment on column quote_daily.volume is 'Traded value in BRL (VOLTOT).';
comment on column quote_daily.quantity is 'Shares traded (QUATOT).';

comment on table monthly_report is 'One row per fund per month. Source: CVM informe mensal (complemento + ativo_passivo CSVs). Highest Versao wins.';
comment on column monthly_report.ref_month is 'Reference month (first day).';
comment on column monthly_report.version is 'CVM Versao; resubmissions increase it.';
comment on column monthly_report.net_assets is 'Patrimonio Liquido (PL) in BRL.';
comment on column monthly_report.nav_per_share is 'Valor patrimonial da cota (VP/cota) in BRL.';
comment on column monthly_report.dividend_yield_ratio is 'Monthly DY as a fraction (0.0075 = 0.75%), as self-reported. |ratio| > 1 is nulled.';
comment on column monthly_report.raw is 'All other non-empty source columns (asset breakdown, shareholder types...) as text, plus any rejected ratio values.';

comment on table distribution is 'Distributions (rendimentos/amortizacoes). Source: B3 FundosNET "Aviso aos Cotistas - Estruturado" XML.';
comment on column distribution.base_date is 'Data-com: holders at the close of this date receive the distribution.';
comment on column distribution.kind is 'income (rendimento) | amortization (amortizacao).';

comment on table fund_override is 'Manual corrections by ticker. Source: seeds/fund_overrides.csv (table is replaced on each load).';

comment on table ingestion_run is 'One row per source file processed. Drives idempotent catch-up (sha256 comparison) and auditing.';
comment on column ingestion_run.period is 'Source-specific: CVM year (2026), COTAHIST yearly (A2026) or daily (D20261002).';

comment on view fund_profile is 'Category (Tijolo/Papel/FoF/Híbrido/Desenvolvimento) from the latest asset breakdown, plus segment; fund_override wins.';

-- migrate:down

do $$
declare
    rel record;
    col record;
begin
    for rel in select c.oid, c.relname, c.relkind from pg_class c join pg_namespace n on n.oid = c.relnamespace
               where n.nspname = 'public' and c.relkind in ('r', 'v') loop
        execute format('comment on %s %I is null', case rel.relkind when 'v' then 'view' else 'table' end, rel.relname);
        for col in select attname from pg_attribute where attrelid = rel.oid and attnum > 0 and not attisdropped loop
            execute format('comment on column %I.%I is null', rel.relname, col.attname);
        end loop;
    end loop;
end $$;
