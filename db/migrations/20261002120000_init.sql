-- migrate:up

-- Fund registry, sourced from CVM monthly reports (latest report wins).
create table fund (
    id                  bigint generated always as identity primary key,
    cnpj                text not null unique,           -- digits only
    type                text not null default 'FII' check (type in ('FII', 'FIAGRO', 'FIINFRA')),
    name                text not null,
    isin                text,                           -- fund-level ISIN reported to CVM
    segment             text,                           -- CVM "Segmento_Atuacao"
    mandate             text,
    management_type     text,
    target_audience     text,
    administrator_name  text,
    administrator_cnpj  text,
    started_at          date,
    listed              boolean,                        -- CVM "Mercado_Negociacao_Bolsa"
    last_report_month   date,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create index fund_isin_idx on fund (isin);

-- Exchange-listed securities seen in B3 COTAHIST. Linked to a fund through the ISIN.
create table security (
    ticker              text primary key,
    isin                text not null,
    bdi_code            text not null,
    short_name          text,
    first_trade_date    date not null,
    last_trade_date     date not null,
    -- Resolved by the pipeline: the ISIN reported to CVM is often missing or mistyped.
    fund_id             bigint references fund (id),
    link_method         text check (link_method in ('fnet', 'isin', 'isin_issuer'))
);
create index security_isin_idx on security (isin);
create index security_fund_idx on security (fund_id);

-- Daily OHLC from COTAHIST. Prices are as traded (not adjusted), in BRL.
create table quote_daily (
    ticker              text not null,
    trade_date          date not null,
    isin                text not null,
    open                numeric(18, 4),
    high                numeric(18, 4),
    low                 numeric(18, 4),
    avg                 numeric(18, 4),
    close               numeric(18, 4) not null,
    trades              integer,
    quantity            bigint,
    volume              numeric(20, 2),
    primary key (ticker, trade_date)
);
create index quote_daily_date_idx on quote_daily (trade_date);

-- CVM monthly report (inf_mensal). Ratio columns are fractions (0.0075 = 0.75%); implausible
-- self-reported values (|ratio| > 1) are nulled by the pipeline and kept verbatim in raw.
-- Unbounded numerics: the source data is unvalidated and must never fail a load.
create table monthly_report (
    fund_id                 bigint not null references fund (id),
    ref_month               date not null,              -- first day of the month
    version                 integer not null,
    delivered_at            date,
    shares_outstanding      numeric,
    total_assets            numeric,
    net_assets              numeric,
    nav_per_share           numeric,
    shareholders            integer,
    admin_fee_ratio         numeric,
    return_effective_ratio  numeric,
    return_nav_ratio        numeric,
    dividend_yield_ratio    numeric,
    amortization_ratio      numeric,
    raw                     jsonb not null default '{}', -- non-null source columns not modeled above
    primary key (fund_id, ref_month)
);

-- Distributions from FundosNET "Aviso aos Cotistas - Estruturado".
create table distribution (
    id                  bigint generated always as identity primary key,
    fnet_document_id    bigint not null,
    isin                text not null,
    ticker              text,
    kind                text not null check (kind in ('income', 'amortization')),
    base_date           date not null,                  -- data-com
    payment_date        date,
    amount_per_share    numeric(18, 8) not null,
    reference_period    text,
    tax_exempt          boolean,
    fund_cnpj           text,
    delivered_at        timestamptz,
    unique (fnet_document_id, isin, kind)
);
create index distribution_isin_base_idx on distribution (isin, base_date);

-- One row per source file/period processed; drives idempotent catch-up.
create table ingestion_run (
    id                  bigint generated always as identity primary key,
    source              text not null,
    period              text not null,
    url                 text,
    sha256              text,
    rows                integer,
    status              text not null check (status in ('running', 'ok', 'error', 'skipped')),
    error               text,
    started_at          timestamptz not null default now(),
    finished_at         timestamptz
);
create index ingestion_run_source_period_idx on ingestion_run (source, period, started_at desc);

-- migrate:down

drop table ingestion_run;
drop table distribution;
drop table monthly_report;
drop table quote_daily;
drop table security;
drop table fund;
