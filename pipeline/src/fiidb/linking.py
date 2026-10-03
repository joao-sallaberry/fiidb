"""Links B3 tickers (security) to CVM funds.

The ISIN a fund reports to CVM is frequently missing, mistyped or points to a subscription
receipt, so matching falls back from the strongest evidence to the weakest. Earlier,
stronger links are never overwritten by weaker ones.
"""

import psycopg

# Feeder funds often report the master fund's ISIN, so one ISIN may belong to several CVM funds.
# It only identifies a fund when a single fund reports it, or a single one of them is exchange-listed.
UNAMBIGUOUS_ISIN = """
    select isin, id as fund_id
    from (
        select isin, id,
            row_number() over (partition by isin order by listed desc nulls last, id) as rank,
            count(*) over (partition by isin) as funds,
            count(*) filter (where listed) over (partition by isin) as listed_funds
        from fund
        where isin is not null
    ) candidates
    where rank = 1 and (funds = 1 or listed_funds = 1)
"""

STEPS = {
    # FundosNET notices state CNPJ, ISIN and ticker together: the strongest evidence.
    "fnet": """
        update security s set fund_id = m.fund_id, link_method = 'fnet'
        from (
            select distinct on (d.isin) d.isin, f.id fund_id
            from distribution d join fund f on f.cnpj = d.fund_cnpj
            order by d.isin, d.delivered_at desc nulls last
        ) m
        where s.isin = m.isin and (s.fund_id is distinct from m.fund_id or s.link_method is distinct from 'fnet')
    """,
    # Drop ISIN links that are no longer unambiguous (see UNAMBIGUOUS_ISIN).
    "isin_stale": f"""
        update security s set fund_id = null, link_method = null
        where s.link_method = 'isin' and s.isin not in (select isin from ({UNAMBIGUOUS_ISIN}) u)
    """,
    # Exact ISIN match against the fund's reported ISIN.
    "isin": f"""
        update security s set fund_id = u.fund_id, link_method = 'isin'
        from ({UNAMBIGUOUS_ISIN}) u
        where u.isin = s.isin and s.link_method is distinct from 'fnet'
            and (s.fund_id is distinct from u.fund_id or s.link_method is distinct from 'isin')
    """,
    # Same issuer code (ISIN chars 3-6, e.g. BR[HGLG]CTF004), when exactly one fund has it.
    "isin_issuer": """
        update security s set fund_id = m.fund_id, link_method = 'isin_issuer'
        from (
            select substr(isin, 3, 4) issuer, min(id) fund_id
            from fund where length(isin) = 12
            group by 1 having count(*) = 1
        ) m
        where s.fund_id is null and substr(s.isin, 3, 4) = m.issuer
    """,
}


def link_securities(conn: psycopg.Connection) -> dict[str, int]:
    return {name: conn.execute(sql).rowcount for name, sql in STEPS.items()}
