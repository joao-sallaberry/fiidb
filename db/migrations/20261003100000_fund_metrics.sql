-- migrate:up

-- One row per ticker with the figures the spreadsheet consumes. Distributions only exist for
-- watchlist funds, so DY 12m is filled only when their history covers the last 12 months.
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

comment on view fund_metrics is 'Per-ticker figures for consumers (price, P/VP, last income, DY 12m, liquidity). DY 12m only for watchlist funds with >= 12 months of history.';
comment on column fund_metrics.price is 'Last close (not adjusted), from quote_daily.';
comment on column fund_metrics.price_to_nav is 'P/VP: price / nav_per_share of the latest CVM report.';
comment on column fund_metrics.last_income is 'Latest income (rendimento) per share; amortizations excluded.';
comment on column fund_metrics.income_12m is 'Sum of income per share with base date (data-com) in the last 12 months. Null unless the history is complete.';
comment on column fund_metrics.dividend_yield_12m is 'income_12m / price, as a fraction. Null unless the history is complete.';
comment on column fund_metrics.avg_volume_21d is 'Average daily traded value (BRL) over the last 21 sessions within 60 days.';
comment on column fund_metrics.active is 'Traded in the last 30 days.';
comment on column fund_metrics.watched is 'In the watchlist (distributions are ingested).';

-- migrate:down

drop view fund_metrics;
