-- migrate:up

-- Fiagro traded under BDI 13 until 2025-10 (and a few, e.g. BBGO11, still do); COTAHIST now loads
-- it too. A Fiagro whose code changed around the move to BDI 14 is still a ticker change.

create or replace view ticker_change as
with candidate as (
    -- Same fund and market (BDI 13 and 14 count as one: Fiagro moved between them in 2025-10),
    -- a main share code (XXXX11 or the old XXXX11B, not subscription receipts/rights such as 12,
    -- 13), and the new code starts trading within a month of the last trade of the old one.
    select distinct on (o.ticker) o.ticker as old_ticker, n.ticker as new_ticker, o.last_trade_date, n.first_trade_date
    from security o
    join security n on n.fund_id = o.fund_id and n.ticker <> o.ticker
        and (n.bdi_code = o.bdi_code or (n.bdi_code in ('13', '14') and o.bdi_code in ('13', '14')))
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

comment on view ticker_change is 'Ticker changes: detected from COTAHIST (same fund and market, BDI 13 and 14 counting as one; main share codes, new code starting within 31 days of the last trade of the old one) or listed in ticker_change_manual, which wins.';
comment on column ticker_change.change_date is 'First trading day of the new ticker.';
comment on column ticker_change.source is 'auto (detected) or manual (seeds/ticker_changes.csv).';
comment on column security.bdi_code is 'B3 BDI code of the latest trade: 12 = FII; 13 = Fiagro (most until 2025-10); 14 = Fiagro/FI-Infra/ETFs/FIPs/FIDCs.';

refresh materialized view ticker_lineage;

-- migrate:down

create or replace view ticker_change as
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
comment on column security.bdi_code is 'B3 BDI code: 12 = FII; 14 = Fiagro/FI-Infra/ETFs/FIPs/FIDCs.';

refresh materialized view ticker_lineage;
