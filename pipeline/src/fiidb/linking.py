"""Links B3 tickers (security) to CVM funds.

The ISIN a fund reports to CVM is frequently missing, mistyped or points to a subscription
receipt, so matching falls back from the strongest evidence to the weakest. Earlier,
stronger links are never overwritten by weaker ones.
"""

import psycopg

STEPS = {
    # Exact ISIN match against the fund's reported ISIN.
    "isin": """
        update security s set fund_id = f.id, link_method = 'isin'
        from fund f
        where f.isin = s.isin and s.link_method is distinct from 'fnet'
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
