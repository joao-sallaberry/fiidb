-- migrate:up

-- Share splits/groupings and bonus shares, from the B3 listed-fund page (stockDividends).
create table corporate_action (
    isin                text not null,
    last_date_prior     date not null,          -- data-com: last day the old share traded
    kind                text not null,          -- B3 label: DESDOBRAMENTO, GRUPAMENTO, BONIFICACAO...
    ticker              text,
    factor_raw          text not null,          -- as published by B3 (e.g. '900,00000000000')
    multiplier          numeric,                -- shares after per share before; null = unknown convention
    approved_on         date,
    fetched_at          timestamptz not null default now(),
    primary key (isin, last_date_prior, kind)
);

comment on table corporate_action is 'Share splits, groupings and bonus shares per ISIN. Source: B3 listed-fund page (stockDividends).';
comment on column corporate_action.last_date_prior is 'Data-com of the event: the last trading day of the old share; prices and per-share values change from the next day.';
comment on column corporate_action.factor_raw is 'Factor exactly as published by B3. For DESDOBRAMENTO/BONIFICACAO it is the % of new shares (900 = 9 new per share).';
comment on column corporate_action.multiplier is 'Shares held after the event per share held before (split 1:10 = 10). Null when the B3 convention for the kind is not confirmed; such events are not applied.';

-- Distributions from active notices, deduplicated, with amounts restated per current share.
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

create or replace view fund_metrics as
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


-- migrate:down

create or replace view fund_metrics as
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
-- Active documents only; when two notices state the same payment, the latest delivery wins.
dist as (
    select distinct on (d.isin, d.kind, d.base_date) d.*
    from distribution d
    join fnet_document doc on doc.id = d.fnet_document_id and doc.status = 'A'
    order by d.isin, d.kind, d.base_date, d.delivered_at desc nulls last
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


drop view distribution_adjusted;
drop table corporate_action;
