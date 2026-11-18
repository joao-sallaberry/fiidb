-- migrate:up

-- A fund can change its trading code (NAVT11 -> VTOT11, MALL11 -> PMLL11, the old "11B" classes
-- -> 11). COTAHIST then has two tickers and FundosNET/B3 two ISINs for the same share; metrics
-- follow the chain so prices, liquidity and distributions are not split between them.

create table ticker_change_manual (
    old_ticker text primary key,
    new_ticker text not null,
    note text,
    check (old_ticker <> new_ticker)
);

comment on table ticker_change_manual is 'Ticker changes not detected automatically (e.g. new ticker not linked to the fund yet). Source: seeds/ticker_changes.csv, replaced on every seed load.';
comment on column ticker_change_manual.old_ticker is 'Ticker that stopped trading.';
comment on column ticker_change_manual.new_ticker is 'Ticker the same shares trade under afterwards.';
comment on column ticker_change_manual.note is 'Evidence (announcement, dates).';

create view ticker_change as
with candidate as (
    -- Same fund and market, a main share code (XXXX11 or the old XXXX11B, not subscription
    -- receipts/rights such as 12, 13), and the new code starts trading within a month of the
    -- last trade of the old one.
    select distinct on (o.ticker) o.ticker as old_ticker, n.ticker as new_ticker, o.last_trade_date, n.first_trade_date
    from security o
    join security n on n.fund_id = o.fund_id and n.bdi_code = o.bdi_code and n.ticker <> o.ticker
    where n.first_trade_date > o.last_trade_date
        and n.first_trade_date - o.last_trade_date <= 31
        and o.ticker ~ '^[A-Z]{4}11B?$'
        and n.ticker ~ '^[A-Z]{4}11B?$'
    order by o.ticker, n.first_trade_date
),
auto as (
    select distinct on (new_ticker) old_ticker, new_ticker
    from candidate
    order by new_ticker, last_trade_date desc
),
manual as (
    select m.old_ticker, m.new_ticker
    from ticker_change_manual m
    join security o on o.ticker = m.old_ticker
    join security n on n.ticker = m.new_ticker
)
select c.old_ticker, c.new_ticker, n.first_trade_date as change_date, c.source
from (
    select old_ticker, new_ticker, 'manual' as source from manual
    union all
    select old_ticker, new_ticker, 'auto' from auto a
    where not exists (select 1 from manual m where m.old_ticker = a.old_ticker or m.new_ticker = a.new_ticker)
) c
join security n on n.ticker = c.new_ticker;

comment on view ticker_change is 'Ticker changes: detected from COTAHIST (same fund and BDI, main share codes, new code starting within 31 days of the last trade of the old one) or listed in ticker_change_manual, which wins.';
comment on column ticker_change.change_date is 'First trading day of the new ticker.';
comment on column ticker_change.source is 'auto (detected) or manual (seeds/ticker_changes.csv).';

-- Materialized: the planner has no statistics for a recursive query, and its guesses made every
-- join with the lineage look millions of rows big (slow plans, seconds of JIT compilation). The
-- pipeline refreshes it after linking tickers and loading seeds (fiidb.linking.refresh_lineage).
create materialized view ticker_lineage as
with recursive chain as (
    -- Only the (few) changes recurse: starting from the last change of each chain, walk back.
    select c.new_ticker as ticker, c.old_ticker as member_ticker, 1 as depth
    from ticker_change c
    where not exists (select 1 from ticker_change later where later.old_ticker = c.new_ticker)
    union all
    select chain.ticker, c.old_ticker, chain.depth + 1
    from chain
    join ticker_change c on c.new_ticker = chain.member_ticker
    where chain.depth < 20
)
select s.ticker, s.ticker as member_ticker, s.isin as member_isin, 0 as depth
from security s
where not exists (select 1 from ticker_change c where c.old_ticker = s.ticker)
union all
select chain.ticker, chain.member_ticker, s.isin, chain.depth
from chain
join security s on s.ticker = chain.member_ticker;

create unique index ticker_lineage_member_key on ticker_lineage (member_ticker);
create index ticker_lineage_ticker_idx on ticker_lineage (ticker);
create index ticker_lineage_isin_idx on ticker_lineage (member_isin);

comment on materialized view ticker_lineage is 'Each current ticker (one that did not change to another) with itself (depth 0) and every earlier ticker of the same shares. Materialized from ticker_change and security; refreshed by the pipeline after linking and seeding.';
comment on column ticker_lineage.ticker is 'Current ticker.';
comment on column ticker_lineage.member_ticker is 'The current ticker or one it replaced.';
comment on column ticker_lineage.member_isin is 'ISIN of member_ticker (security.isin); the old XXXX11B classes share it with XXXX11.';
comment on column ticker_lineage.depth is '0 for the current ticker, 1 for the one it replaced, and so on.';

-- Both views change (corporate actions and metrics follow the lineage), so both are recreated.
drop view fund_metrics;
drop view distribution_adjusted;

create view distribution_adjusted as
with related as (
    -- ISINs of the same shares under any ticker of the lineage (itself included).
    select l.member_isin as isin, array_agg(distinct m.member_isin) as isins
    from ticker_lineage l
    join ticker_lineage m on m.ticker = l.ticker
    group by l.member_isin
)
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
left join related r on r.isin = d.isin
left join lateral (
    -- Events after the distribution's data-com change the share it was paid on, including events
    -- under a later ticker; an event listed under two ISINs of the same shares counts once.
    select exp(sum(ln(e.multiplier))) as factor
    from (
        select distinct on (c.last_date_prior, c.kind) c.multiplier
        from corporate_action c
        where c.multiplier > 0 and c.last_date_prior >= d.base_date
            and c.isin = any(coalesce(r.isins, array[d.isin]))
        order by c.last_date_prior, c.kind, c.isin = d.isin desc
    ) e
) a on true;

comment on view distribution_adjusted is 'Active, deduplicated distributions (FundosNET and B3 page); amount_adjusted is per current share (divided by later splits, also those under a later ticker).';
comment on column distribution_adjusted.split_factor is 'Product of corporate_action multipliers with data-com on/after this distribution''s data-com, on its ISIN or on the ISIN of another ticker in the same lineage.';
comment on column distribution_adjusted.amount_adjusted is 'amount_per_share / split_factor: the amount per share as it trades today.';

-- A ticker missing from ticker_lineage (COTAHIST loaded, lineage not refreshed yet) still gets a
-- row with its own data: lineage joins are left joins falling back to the ticker itself.
create view fund_metrics as
with today as (
    select (now() at time zone 'America/Sao_Paulo')::date as d
),
-- The current ticker always has the latest trade: earlier tickers stopped before it started.
last_quote as (
    select distinct on (ticker) ticker, trade_date, close
    from quote_daily
    order by ticker, trade_date desc
),
liquidity as (
    select ticker, avg(volume) as avg_volume_21d
    from (
        select
            coalesce(l.ticker, q.ticker) as ticker,
            q.volume,
            row_number() over (partition by coalesce(l.ticker, q.ticker) order by q.trade_date desc) as n
        from quote_daily q
        left join ticker_lineage l on l.member_ticker = q.ticker
        cross join today
        where q.trade_date >= today.d - 60
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
-- Amounts per current share (split-adjusted; see distribution_adjusted), from the ISINs of every
-- ticker in the lineage; a payment stated under two of them counts once (the newest ticker wins).
dist as (
    select distinct on (coalesce(l.ticker, s.ticker), d.kind, d.base_date)
        coalesce(l.ticker, s.ticker) as ticker,
        d.kind,
        d.base_date,
        d.payment_date,
        d.amount_adjusted as amount_per_share
    from distribution_adjusted d
    join security s on s.isin = d.isin
    left join ticker_lineage l on l.member_ticker = s.ticker
    order by coalesce(l.ticker, s.ticker), d.kind, d.base_date, coalesce(l.depth, 0)
),
last_income as (
    select distinct on (ticker) ticker, amount_per_share, base_date, payment_date
    from dist
    where kind = 'income'
    order by ticker, base_date desc
),
income_12m as (
    select ticker, sum(amount_per_share) as total
    from dist, today
    where kind = 'income' and base_date > today.d - interval '12 months'
    group by ticker
),
previous as (
    select ticker, string_agg(member_ticker, ', ' order by depth) as tickers
    from ticker_lineage
    where depth > 0
    group by ticker
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
    w.ticker is not null as watched,
    pv.tickers as previous_tickers
from security s
cross join today
left join fund f on f.id = s.fund_id
left join fund_profile p on p.ticker = s.ticker
left join last_quote q on q.ticker = s.ticker
left join liquidity l on l.ticker = s.ticker
left join last_report r on r.fund_id = s.fund_id
left join last_income li on li.ticker = s.ticker
left join income_12m i on i.ticker = s.ticker
left join watchlist w on w.ticker = s.ticker
left join previous pv on pv.ticker = s.ticker
cross join lateral (select w.history_since <= today.d - interval '12 months' as complete) history
where not exists (select 1 from ticker_lineage tl where tl.member_ticker = s.ticker and tl.depth > 0);

comment on view fund_metrics is 'Per-ticker figures for consumers (price, P/VP, last income, DY 12m, liquidity); one row per current ticker, also counting the tickers it replaced (ticker_lineage). DY 12m only for watchlist funds with >= 12 months of history.';
comment on column fund_metrics.price is 'Last close (not adjusted), from quote_daily.';
comment on column fund_metrics.report_month is 'Date of nav_per_share: first day of the month of the CVM monthly report (FII) or the day of the CVM daily report (FI-Infra).';
comment on column fund_metrics.price_to_nav is 'P/VP: price / nav_per_share of the latest CVM report (monthly for FII, daily for FI-Infra).';
comment on column fund_metrics.last_income is 'Latest income (rendimento) per share, also under earlier tickers; amortizations excluded.';
comment on column fund_metrics.income_12m is 'Sum of income per share with base date (data-com) in the last 12 months, also under earlier tickers. Null unless the history is complete.';
comment on column fund_metrics.dividend_yield_12m is 'income_12m / price, as a fraction. Null unless the history is complete.';
comment on column fund_metrics.avg_volume_21d is 'Average daily traded value (BRL) over the last 21 sessions within 60 days, also under earlier tickers.';
comment on column fund_metrics.active is 'Traded in the last 30 days.';
comment on column fund_metrics.watched is 'In the watchlist (distributions are ingested).';
comment on column fund_metrics.previous_tickers is 'Earlier tickers of the same shares, most recent first (see ticker_lineage); null when none.';

-- migrate:down

drop view fund_metrics;
drop view distribution_adjusted;
drop materialized view ticker_lineage;
drop view ticker_change;
drop table ticker_change_manual;

-- Views as in 20261009120000_fiinfra.sql.
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
