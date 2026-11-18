"""Links B3 tickers (security) to CVM funds.

The ISIN a fund reports to CVM is frequently missing, mistyped or points to a subscription
receipt, so matching falls back from the strongest evidence to the weakest. Earlier,
stronger links are never overwritten by weaker ones. ISIN matching applies to FIIs (BDI 12) and
to Fiagro funds (BDI 14, shared with ETFs, FIPs and FIDCs, so the fund type is checked):
FI-Infra funds have no ISIN at CVM and are linked by the CNPJ given in the watchlist.
"""

import psycopg

# Feeder funds often report the master fund's ISIN, so one ISIN may belong to several CVM funds.
# It only identifies a fund when a single fund reports it. (Preferring the one that says it is
# exchange-listed was tried and linked KISU11 and HUSC11 to the wrong funds: CVM's flag is unreliable.)
UNAMBIGUOUS_ISIN = """
    select isin, min(id) as fund_id, min(type) as fund_type
    from fund
    where isin is not null
    group by isin
    having count(*) = 1
"""

STEPS = {
    # FundosNET notices state CNPJ, ISIN and ticker together: the strongest evidence.
    "fnet": """
        update security s set fund_id = m.fund_id, link_method = 'fnet'
        from (
            select distinct on (d.isin) d.isin, f.id fund_id
            from distribution d join fund f on f.cnpj = d.fund_cnpj
            where d.source = 'fnet'
            order by d.isin, d.delivered_at desc nulls last
        ) m
        where s.isin = m.isin and (s.fund_id is distinct from m.fund_id or s.link_method is distinct from 'fnet')
    """,
    # CNPJ given with `fiidb watch add --cnpj` (checked against B3 shares x NAV when added).
    "watchlist": """
        update security s set fund_id = f.id, link_method = 'watchlist'
        from watchlist w join fund f on f.cnpj = w.cnpj
        where s.ticker = w.ticker and s.link_method is distinct from 'fnet'
            and (s.fund_id is distinct from f.id or s.link_method is distinct from 'watchlist')
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
        where u.isin = s.isin and (s.bdi_code = '12' or (s.bdi_code = '14' and u.fund_type = 'FIAGRO'))
            and coalesce(s.link_method, '') not in ('fnet', 'watchlist')
            and (s.fund_id is distinct from u.fund_id or s.link_method is distinct from 'isin')
    """,
    # Same issuer code (ISIN chars 3-6, e.g. BR[HGLG]CTF004), when exactly one fund has it.
    "isin_issuer": """
        update security s set fund_id = m.fund_id, link_method = 'isin_issuer'
        from (
            select substr(isin, 3, 4) issuer, min(id) fund_id
            from fund where length(isin) = 12 and type = 'FII'
            group by 1 having count(*) = 1
        ) m
        where s.fund_id is null and s.bdi_code = '12' and substr(s.isin, 3, 4) = m.issuer
    """,
    # The same for Fiagro (BDI 14): KNCA11 trades as BRKNCACTF006 and reports BRKNCACTF014 to CVM.
    "isin_issuer_fiagro": """
        update security s set fund_id = m.fund_id, link_method = 'isin_issuer'
        from (
            select substr(isin, 3, 4) issuer, min(id) fund_id
            from fund where length(isin) = 12 and type = 'FIAGRO'
            group by 1 having count(*) = 1
        ) m
        where s.fund_id is null and s.bdi_code = '14' and substr(s.isin, 3, 4) = m.issuer
    """,
}


def link_securities(conn: psycopg.Connection) -> dict[str, int]:
    return {name: conn.execute(sql).rowcount for name, sql in STEPS.items()}
