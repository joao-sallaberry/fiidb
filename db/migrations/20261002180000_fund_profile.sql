-- migrate:up

-- Manual corrections, loaded from seeds/fund_overrides.csv (the CSV is the source of truth).
create table fund_override (
    ticker      text primary key,
    category    text check (category in ('Tijolo', 'Papel', 'FoF', 'Híbrido', 'Desenvolvimento')),
    segment     text,
    note        text
);

-- Fund category derived from the asset breakdown of the latest monthly report that has one.
-- CVM's self-reported segment is too coarse (most funds say "Multicategoria") and has no
-- "Papel" notion, so it is only used as the segment of brick-and-mortar funds, when specific.
create view fund_profile as
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

-- migrate:down

drop view fund_profile;
drop table fund_override;
