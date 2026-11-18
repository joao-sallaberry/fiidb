-- migrate:up

-- Fiagro: CVM monthly report of its own (FIAGRO/DOC/INF_MENSAL) loaded into fund/monthly_report;
-- distributions come from FundosNET like FIIs. No new tables: only the classification and comments.
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
    coalesce(
        o.category,
        case f.type when 'FIINFRA' then 'FI-Infra' when 'FIAGRO' then 'Fiagro' end,
        c.category
    ) as category,
    coalesce(
        o.segment,
        case
            -- FII only: a Fiagro may still carry the composition of its old FII reports.
            when f.type = 'FII' and coalesce(o.category, c.category) in ('Tijolo', 'Desenvolvimento')
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

comment on view fund_profile is 'Category (Tijolo/Papel/FoF/Híbrido/Desenvolvimento from the latest asset breakdown of a FII; FI-Infra or Fiagro by fund.type), plus segment; fund_override wins.';

comment on table fund is 'Fund registry. Sources: CVM FII monthly report (geral CSV), CVM Fiagro monthly report, CVM fund registry (FI-Infra); latest report wins.';
comment on column fund.type is 'FII (CVM FII monthly report) | FIAGRO (CVM Fiagro monthly report; most were FII until ~2025-08 and keep that history) | FIINFRA (CVM fund registry, created by `fiidb watch add --cnpj`).';
comment on column fund.isin is 'ISIN as self-reported to CVM. Often missing or wrong; see security.link_method. A blank ISIN in the Fiagro report keeps the previous one.';
comment on column fund.listed is 'FII: CVM Mercado_Negociacao_Bolsa (S/N). Fiagro: Mercado_Negociacao = BOLSA. Self-reported, unreliable.';
comment on table monthly_report is 'One row per fund per month. Sources: CVM FII monthly report (complemento + ativo_passivo CSVs) and CVM Fiagro monthly report (raw ->> ''informe'' = ''fiagro''; wins when a fund filed both for a month). Highest Versao wins.';
comment on column monthly_report.nav_per_share is 'Valor patrimonial da cota (VP/cota) in BRL. Fiagro: net assets / shares when it matches the two-decimal reported value; null for single-share funds.';
comment on column monthly_report.raw is 'All other non-empty source columns (asset breakdown, shareholder types...) as text, plus any rejected values. Fiagro rows carry informe = fiagro and percentages in percent, as reported.';
comment on column security.link_method is 'How fund_id was resolved: fnet (FundosNET notice, strongest) > watchlist (CNPJ given in `fiidb watch add --cnpj`, checked against B3 shares x NAV for FI-Infra) > isin (exact; BDI 12, or BDI 14 for a Fiagro) > isin_issuer (ISIN chars 3-6, within FIIs or within Fiagros).';

-- migrate:down

-- Drops what the Fiagro report loaded: funds that also filed the FII report go back to FII.
delete from monthly_report where raw ->> 'informe' = 'fiagro';
update fund f set type = 'FII', last_report_month = m.last_month
from (select fund_id, max(ref_month) as last_month from monthly_report group by fund_id) m
where f.type = 'FIAGRO' and m.fund_id = f.id;
update security s set fund_id = null, link_method = null from fund f where f.id = s.fund_id and f.type = 'FIAGRO';
delete from fund where type = 'FIAGRO';
update security set fund_id = null, link_method = null where bdi_code = '14' and link_method in ('isin', 'isin_issuer');
delete from ingestion_run where source = 'cvm_fiagro_inf_mensal';

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

comment on table fund is 'FII registry. Source: CVM informe mensal (geral CSV); latest report wins.';
comment on column fund.type is 'FII (CVM monthly FII report) | FIINFRA (CVM fund registry, created by `fiidb watch add --cnpj`) | FIAGRO (not ingested yet).';
comment on column fund.isin is 'ISIN as self-reported to CVM. Often missing or wrong; see security.link_method.';
comment on column fund.listed is 'CVM Mercado_Negociacao_Bolsa (S/N).';
comment on table monthly_report is 'One row per fund per month. Source: CVM informe mensal (complemento + ativo_passivo CSVs). Highest Versao wins.';
comment on column monthly_report.nav_per_share is 'Valor patrimonial da cota (VP/cota) in BRL.';
comment on column monthly_report.raw is 'All other non-empty source columns (asset breakdown, shareholder types...) as text, plus any rejected ratio values.';
comment on column security.link_method is 'How fund_id was resolved: fnet (FundosNET notice, strongest) > watchlist (CNPJ given in `fiidb watch add --cnpj`, checked against B3 shares x NAV) > isin (exact) > isin_issuer (ISIN chars 3-6).';
