-- migrate:up

-- CVM daily report (inf_diario_fi) for non-FII funds in the watchlist (FI-Infra).
create table daily_report (
    fund_id             bigint not null references fund (id),
    ref_date            date not null,
    nav_per_share       numeric,
    net_assets          numeric,
    total_value         numeric,
    shareholders        integer,
    primary key (fund_id, ref_date)
);
comment on table daily_report is 'CVM daily report (inf_diario_fi) of FI-Infra funds in the watchlist. Source: CVM FI/DOC/INF_DIARIO.';
comment on column daily_report.nav_per_share is 'VL_QUOTA: NAV per share on ref_date (not adjusted for splits). Non-positive values are nulled.';
comment on column daily_report.net_assets is 'VL_PATRIM_LIQ: net assets (BRL).';
comment on column daily_report.total_value is 'VL_TOTAL: total value of the portfolio (BRL).';
comment on column daily_report.shareholders is 'NR_COTST: number of shareholders.';

-- Distributions also come from the B3 listed-fund page (cashDividends), for funds FundosNET does not cover.
alter table distribution add column source text not null default 'fnet' check (source in ('fnet', 'b3'));
alter table distribution alter column fnet_document_id drop not null;
alter table distribution add constraint distribution_fnet_document_check
    check (source <> 'fnet' or fnet_document_id is not null);
create unique index distribution_b3_key on distribution (isin, kind, base_date) where source = 'b3';
comment on table distribution is 'Distributions (rendimentos/amortizacoes). Sources: B3 FundosNET "Aviso aos Cotistas - Estruturado" XML (FII) and the B3 listed-fund page cashDividends (FI-Infra).';
comment on column distribution.source is 'fnet = FundosNET notice (fnet_document_id set); b3 = B3 listed-fund page cashDividends (rolling ~12-month window, rows accumulate).';

alter table security drop constraint security_link_method_check;
alter table security add constraint security_link_method_check
    check (link_method in ('fnet', 'watchlist', 'isin', 'isin_issuer'));
comment on column security.bdi_code is 'B3 BDI code: 12 = FII; 14 = Fiagro/FI-Infra/ETFs/FIPs/FIDCs.';
comment on column security.link_method is 'How fund_id was resolved: fnet (FundosNET notice, strongest) > watchlist (CNPJ given in `fiidb watch add --cnpj`, checked against B3 shares x NAV) > isin (exact) > isin_issuer (ISIN chars 3-6).';
comment on column fund.type is 'FII (CVM monthly FII report) | FIINFRA (CVM fund registry, created by `fiidb watch add --cnpj`) | FIAGRO (not ingested yet).';

create or replace view fund_profile as
with latest as (
    select distinct on (fund_id)
        fund_id,
        ref_month,
        (raw ->> 'Total_Investido')::numeric as invested,
        coalesce((raw ->> 'Direitos_Bens_Imoveis')::numeric, 0)
            + coalesce((raw ->> 'Acoes_Sociedades_Atividades_FII')::numeric, 0)
            + coalesce((raw ->> 'Cotas_Sociedades_Atividades_FII')::numeric, 0) as real_estate,
        coalesce((raw ->> 'Imoveis_Venda_Acabados')::numeric, 0)
            + coalesce((raw ->> 'Imoveis_Venda_Construcao')::numeric, 0)
            + coalesce((raw ->> 'Terrenos')::numeric, 0) as for_sale,
        coalesce((raw ->> 'CRI')::numeric, 0)
            + coalesce((raw ->> 'CRI_CRA')::numeric, 0)
            + coalesce((raw ->> 'LCI')::numeric, 0)
            + coalesce((raw ->> 'LCI_LCA')::numeric, 0)
            + coalesce((raw ->> 'LIG')::numeric, 0)
            + coalesce((raw ->> 'Letras_Hipotecarias')::numeric, 0)
            + coalesce((raw ->> 'Debentures')::numeric, 0)
            + coalesce((raw ->> 'Notas_Promissorias')::numeric, 0) as receivables,
        coalesce((raw ->> 'FII')::numeric, 0) as fii_shares
    from monthly_report
    where (raw ->> 'Total_Investido')::numeric > 0
    order by fund_id, ref_month desc
),
shares as (
    select
        fund_id,
        ref_month as composition_month,
        real_estate / invested as real_estate_share,
        receivables / invested as receivables_share,
        fii_shares / invested as fii_shares_share,
        case when real_estate > 0 then for_sale / real_estate end as for_sale_share
    from latest
),
computed as (
    select
        fund_id,
        composition_month,
        round(real_estate_share, 4) as real_estate_share,
        round(receivables_share, 4) as receivables_share,
        round(fii_shares_share, 4) as fii_shares_share,
        case
            when real_estate_share >= 0.6 and for_sale_share > 0.5 then 'Desenvolvimento'
            when real_estate_share >= 0.6 then 'Tijolo'
            when receivables_share >= 0.6 then 'Papel'
            when fii_shares_share >= 0.6 then 'FoF'
            else 'Híbrido'
        end as category
    from shares
)
select
    f.id as fund_id,
    s.ticker,
    coalesce(o.category, case when f.type = 'FIINFRA' then 'FI-Infra' end, c.category) as category,
    coalesce(
        o.segment,
        case
            when coalesce(o.category, c.category) in ('Tijolo', 'Desenvolvimento')
                and f.segment not in ('Multicategoria', 'Outros', 'Títulos e Val. Mob.')
            then f.segment
        end
    ) as segment,
    case when o.category is not null or o.segment is not null then 'override' else 'computed' end as source,
    c.category as computed_category,
    f.segment as cvm_segment,
    c.real_estate_share,
    c.receivables_share,
    c.fii_shares_share,
    c.composition_month
from fund f
left join security s on s.fund_id = f.id
left join computed c on c.fund_id = f.id
left join fund_override o on o.ticker = s.ticker;

comment on view fund_profile is 'Category (Tijolo/Papel/FoF/Híbrido/Desenvolvimento, or FI-Infra by fund.type) from the latest asset breakdown, plus segment; fund_override wins.';

-- distribution_adjusted gains the source column, so both views are recreated.
drop view fund_metrics;
drop view distribution_adjusted;

create view distribution_adjusted as
select
    d.*,
    coalesce(a.factor, 1) as split_factor,
    (d.amount_per_share / coalesce(a.factor, 1))::numeric(18, 8) as amount_adjusted
from (
    -- FundosNET rows count only from active notices; when two state the same payment, the latest
    -- delivery wins, and FundosNET wins over the B3 page.
    select distinct on (d.isin, d.kind, d.base_date) d.*
    from distribution d
    left join fnet_document doc on doc.id = d.fnet_document_id
    where d.source = 'b3' or doc.status = 'A'
    order by d.isin, d.kind, d.base_date, d.source = 'fnet' desc, d.delivered_at desc nulls last
) d
left join lateral (
    -- Events after the distribution's data-com change the share it was paid on.
    select exp(sum(ln(c.multiplier))) as factor
    from corporate_action c
    where c.isin = d.isin and c.multiplier > 0 and c.last_date_prior >= d.base_date
) a on true;

comment on view distribution_adjusted is 'Active, deduplicated distributions (FundosNET and B3 page); amount_adjusted is per current share (divided by later splits).';
comment on column distribution_adjusted.split_factor is 'Product of corporate_action multipliers with data-com on/after this distribution''s data-com.';
comment on column distribution_adjusted.amount_adjusted is 'amount_per_share / split_factor: the amount per share as it trades today.';

create view fund_metrics as
with today as (
    select (now() at time zone 'America/Sao_Paulo')::date as d
),
last_quote as (
    select distinct on (ticker) ticker, trade_date, close
    from quote_daily
    order by ticker, trade_date desc
),
liquidity as (
    select ticker, avg(volume) as avg_volume_21d
    from (
        select ticker, volume, row_number() over (partition by ticker order by trade_date desc) as n
        from quote_daily, today
        where trade_date >= today.d - 60
    ) recent
    where n <= 21
    group by ticker
),
-- FII: CVM monthly report; FI-Infra: CVM daily report.
last_report as (
    select distinct on (fund_id) fund_id, ref_month, nav_per_share, net_assets, shareholders
    from (
        select fund_id, ref_month, nav_per_share, net_assets, shareholders
        from monthly_report
        where nav_per_share is not null
        union all
        select fund_id, ref_date, nav_per_share, net_assets, shareholders
        from daily_report
        where nav_per_share is not null
    ) r
    order by fund_id, ref_month desc
),
-- Amounts per current share (split-adjusted); see distribution_adjusted.
dist as (
    select isin, kind, base_date, payment_date, amount_adjusted as amount_per_share
    from distribution_adjusted
),
last_income as (
    select distinct on (isin) isin, amount_per_share, base_date, payment_date
    from dist
    where kind = 'income'
    order by isin, base_date desc
),
income_12m as (
    select isin, sum(amount_per_share) as total
    from dist, today
    where kind = 'income' and base_date > today.d - interval '12 months'
    group by isin
)
select
    s.ticker,
    f.cnpj,
    f.name,
    p.category,
    p.segment,
    q.trade_date as price_date,
    q.close as price,
    r.ref_month as report_month,
    r.nav_per_share,
    round(q.close / nullif(r.nav_per_share, 0), 4) as price_to_nav,
    r.net_assets,
    r.shareholders,
    li.amount_per_share as last_income,
    li.base_date as last_income_base_date,
    li.payment_date as last_income_payment_date,
    case when history.complete then coalesce(i.total, 0) end as income_12m,
    case when history.complete then round(coalesce(i.total, 0) / nullif(q.close, 0), 6) end as dividend_yield_12m,
    round(l.avg_volume_21d, 2) as avg_volume_21d,
    s.last_trade_date >= today.d - 30 as active,
    w.ticker is not null as watched
from security s
cross join today
left join fund f on f.id = s.fund_id
left join fund_profile p on p.ticker = s.ticker
left join last_quote q on q.ticker = s.ticker
left join liquidity l on l.ticker = s.ticker
left join last_report r on r.fund_id = s.fund_id
left join last_income li on li.isin = s.isin
left join income_12m i on i.isin = s.isin
left join watchlist w on w.ticker = s.ticker
cross join lateral (select w.history_since <= today.d - interval '12 months' as complete) history;

comment on view fund_metrics is 'Per-ticker figures for consumers (price, P/VP, last income, DY 12m, liquidity). DY 12m only for watchlist funds with >= 12 months of history.';
comment on column fund_metrics.price is 'Last close (not adjusted), from quote_daily.';
comment on column fund_metrics.report_month is 'Date of nav_per_share: first day of the month of the CVM monthly report (FII) or the day of the CVM daily report (FI-Infra).';
comment on column fund_metrics.price_to_nav is 'P/VP: price / nav_per_share of the latest CVM report (monthly for FII, daily for FI-Infra).';
comment on column fund_metrics.last_income is 'Latest income (rendimento) per share; amortizations excluded.';
comment on column fund_metrics.income_12m is 'Sum of income per share with base date (data-com) in the last 12 months. Null unless the history is complete.';
comment on column fund_metrics.dividend_yield_12m is 'income_12m / price, as a fraction. Null unless the history is complete.';
comment on column fund_metrics.avg_volume_21d is 'Average daily traded value (BRL) over the last 21 sessions within 60 days.';
comment on column fund_metrics.active is 'Traded in the last 30 days.';
comment on column fund_metrics.watched is 'In the watchlist (distributions are ingested).';

-- migrate:down

drop view fund_metrics;
drop view distribution_adjusted;
delete from distribution where source = 'b3';
drop index distribution_b3_key;
alter table distribution drop constraint distribution_fnet_document_check;
alter table distribution alter column fnet_document_id set not null;
alter table distribution drop column source;
comment on table distribution is 'Distributions (rendimentos/amortizacoes). Source: B3 FundosNET "Aviso aos Cotistas - Estruturado" XML.';

create view distribution_adjusted as
select
    d.*,
    coalesce(a.factor, 1) as split_factor,
    (d.amount_per_share / coalesce(a.factor, 1))::numeric(18, 8) as amount_adjusted
from (
    -- When two active notices state the same payment, the latest delivery wins.
    select distinct on (d.isin, d.kind, d.base_date) d.*
    from distribution d
    join fnet_document doc on doc.id = d.fnet_document_id and doc.status = 'A'
    order by d.isin, d.kind, d.base_date, d.delivered_at desc nulls last
) d
left join lateral (
    -- Events after the distribution's data-com change the share it was paid on.
    select exp(sum(ln(c.multiplier))) as factor
    from corporate_action c
    where c.isin = d.isin and c.multiplier > 0 and c.last_date_prior >= d.base_date
) a on true;

comment on view distribution_adjusted is 'Active, deduplicated distributions; amount_adjusted is per current share (divided by later splits).';
comment on column distribution_adjusted.split_factor is 'Product of corporate_action multipliers with data-com on/after this distribution''s data-com.';
comment on column distribution_adjusted.amount_adjusted is 'amount_per_share / split_factor: the amount per share as it trades today.';

create view fund_metrics as
with today as (
    select (now() at time zone 'America/Sao_Paulo')::date as d
),
last_quote as (
    select distinct on (ticker) ticker, trade_date, close
    from quote_daily
    order by ticker, trade_date desc
),
liquidity as (
    select ticker, avg(volume) as avg_volume_21d
    from (
        select ticker, volume, row_number() over (partition by ticker order by trade_date desc) as n
        from quote_daily, today
        where trade_date >= today.d - 60
    ) recent
    where n <= 21
    group by ticker
),
last_report as (
    select distinct on (fund_id) fund_id, ref_month, nav_per_share, net_assets, shareholders
    from monthly_report
    where nav_per_share is not null
    order by fund_id, ref_month desc
),
-- Amounts per current share (split-adjusted); see distribution_adjusted.
dist as (
    select isin, kind, base_date, payment_date, amount_adjusted as amount_per_share
    from distribution_adjusted
),
last_income as (
    select distinct on (isin) isin, amount_per_share, base_date, payment_date
    from dist
    where kind = 'income'
    order by isin, base_date desc
),
income_12m as (
    select isin, sum(amount_per_share) as total
    from dist, today
    where kind = 'income' and base_date > today.d - interval '12 months'
    group by isin
)
select
    s.ticker,
    f.cnpj,
    f.name,
    p.category,
    p.segment,
    q.trade_date as price_date,
    q.close as price,
    r.ref_month as report_month,
    r.nav_per_share,
    round(q.close / nullif(r.nav_per_share, 0), 4) as price_to_nav,
    r.net_assets,
    r.shareholders,
    li.amount_per_share as last_income,
    li.base_date as last_income_base_date,
    li.payment_date as last_income_payment_date,
    case when history.complete then coalesce(i.total, 0) end as income_12m,
    case when history.complete then round(coalesce(i.total, 0) / nullif(q.close, 0), 6) end as dividend_yield_12m,
    round(l.avg_volume_21d, 2) as avg_volume_21d,
    s.last_trade_date >= today.d - 30 as active,
    w.ticker is not null as watched
from security s
cross join today
left join fund f on f.id = s.fund_id
left join fund_profile p on p.ticker = s.ticker
left join last_quote q on q.ticker = s.ticker
left join liquidity l on l.ticker = s.ticker
left join last_report r on r.fund_id = s.fund_id
left join last_income li on li.isin = s.isin
left join income_12m i on i.isin = s.isin
left join watchlist w on w.ticker = s.ticker
cross join lateral (select w.history_since <= today.d - interval '12 months' as complete) history;

comment on view fund_metrics is 'Per-ticker figures for consumers (price, P/VP, last income, DY 12m, liquidity). DY 12m only for watchlist funds with >= 12 months of history.';
comment on column fund_metrics.price is 'Last close (not adjusted), from quote_daily.';
comment on column fund_metrics.price_to_nav is 'P/VP: price / nav_per_share of the latest CVM report.';
comment on column fund_metrics.last_income is 'Latest income (rendimento) per share; amortizations excluded.';
comment on column fund_metrics.income_12m is 'Sum of income per share with base date (data-com) in the last 12 months. Null unless the history is complete.';
comment on column fund_metrics.dividend_yield_12m is 'income_12m / price, as a fraction. Null unless the history is complete.';
comment on column fund_metrics.avg_volume_21d is 'Average daily traded value (BRL) over the last 21 sessions within 60 days.';
comment on column fund_metrics.active is 'Traded in the last 30 days.';
comment on column fund_metrics.watched is 'In the watchlist (distributions are ingested).';

create or replace view fund_profile as
with latest as (
    select distinct on (fund_id)
        fund_id,
        ref_month,
        (raw ->> 'Total_Investido')::numeric as invested,
        coalesce((raw ->> 'Direitos_Bens_Imoveis')::numeric, 0)
            + coalesce((raw ->> 'Acoes_Sociedades_Atividades_FII')::numeric, 0)
            + coalesce((raw ->> 'Cotas_Sociedades_Atividades_FII')::numeric, 0) as real_estate,
        coalesce((raw ->> 'Imoveis_Venda_Acabados')::numeric, 0)
            + coalesce((raw ->> 'Imoveis_Venda_Construcao')::numeric, 0)
            + coalesce((raw ->> 'Terrenos')::numeric, 0) as for_sale,
        coalesce((raw ->> 'CRI')::numeric, 0)
            + coalesce((raw ->> 'CRI_CRA')::numeric, 0)
            + coalesce((raw ->> 'LCI')::numeric, 0)
            + coalesce((raw ->> 'LCI_LCA')::numeric, 0)
            + coalesce((raw ->> 'LIG')::numeric, 0)
            + coalesce((raw ->> 'Letras_Hipotecarias')::numeric, 0)
            + coalesce((raw ->> 'Debentures')::numeric, 0)
            + coalesce((raw ->> 'Notas_Promissorias')::numeric, 0) as receivables,
        coalesce((raw ->> 'FII')::numeric, 0) as fii_shares
    from monthly_report
    where (raw ->> 'Total_Investido')::numeric > 0
    order by fund_id, ref_month desc
),
shares as (
    select
        fund_id,
        ref_month as composition_month,
        real_estate / invested as real_estate_share,
        receivables / invested as receivables_share,
        fii_shares / invested as fii_shares_share,
        case when real_estate > 0 then for_sale / real_estate end as for_sale_share
    from latest
),
computed as (
    select
        fund_id,
        composition_month,
        round(real_estate_share, 4) as real_estate_share,
        round(receivables_share, 4) as receivables_share,
        round(fii_shares_share, 4) as fii_shares_share,
        case
            when real_estate_share >= 0.6 and for_sale_share > 0.5 then 'Desenvolvimento'
            when real_estate_share >= 0.6 then 'Tijolo'
            when receivables_share >= 0.6 then 'Papel'
            when fii_shares_share >= 0.6 then 'FoF'
            else 'Híbrido'
        end as category
    from shares
)
select
    f.id as fund_id,
    s.ticker,
    coalesce(o.category, c.category) as category,
    coalesce(
        o.segment,
        case
            when coalesce(o.category, c.category) in ('Tijolo', 'Desenvolvimento')
                and f.segment not in ('Multicategoria', 'Outros', 'Títulos e Val. Mob.')
            then f.segment
        end
    ) as segment,
    case when o.category is not null or o.segment is not null then 'override' else 'computed' end as source,
    c.category as computed_category,
    f.segment as cvm_segment,
    c.real_estate_share,
    c.receivables_share,
    c.fii_shares_share,
    c.composition_month
from fund f
left join security s on s.fund_id = f.id
left join computed c on c.fund_id = f.id
left join fund_override o on o.ticker = s.ticker;

comment on view fund_profile is 'Category (Tijolo/Papel/FoF/Híbrido/Desenvolvimento) from the latest asset breakdown, plus segment; fund_override wins.';

-- Unlinks FI-Infra tickers; their funds and daily reports are dropped.
update security set fund_id = null, link_method = null where link_method = 'watchlist';
alter table security drop constraint security_link_method_check;
alter table security add constraint security_link_method_check
    check (link_method in ('fnet', 'isin', 'isin_issuer'));
comment on column security.bdi_code is 'B3 BDI code: 12 = FII; 14 = Fiagro/FI-Infra/ETFs/FIPs (not ingested yet).';
comment on column security.link_method is 'How fund_id was resolved: fnet (FundosNET notice, strongest) > isin (exact) > isin_issuer (ISIN chars 3-6).';
drop table daily_report;
update security s set fund_id = null, link_method = null from fund f where f.id = s.fund_id and f.type = 'FIINFRA';
delete from fund where type = 'FIINFRA';
comment on column fund.type is 'FII | FIAGRO | FIINFRA. Only FII is ingested for now.';
