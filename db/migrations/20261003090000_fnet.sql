-- migrate:up

-- Funds whose distributions are fetched from FundosNET. Managed with `fiidb watch add|remove|list`;
-- kept out of the repository because it may reveal a personal portfolio.
create table watchlist (
    ticker          text primary key,
    cnpj            text,                   -- digits; overrides the ticker → fund link when set
    added_at        timestamptz not null default now(),
    history_since   date                    -- distributions are complete from this date on
);

comment on table watchlist is 'Funds whose FundosNET distribution notices are ingested. Managed via `fiidb watch`.';
comment on column watchlist.cnpj is 'Fund CNPJ (digits) used to query FundosNET when the ticker is not linked to a fund.';
comment on column watchlist.history_since is 'Earliest date covered by a full history load; DY 12m is only computed when it is at least 12 months back.';

-- FundosNET documents already processed. A re-presentation keeps the document id and bumps the version.
create table fnet_document (
    id              bigint primary key,
    version         integer not null,
    fund_cnpj       text not null,          -- digits, as queried
    status          text not null,          -- situacaoDocumento: A active, C cancelled, I inactive
    delivered_at    timestamptz,
    fetched_at      timestamptz not null default now(),
    error           text                    -- set when the XML could not be downloaded or parsed
);
create index fnet_document_cnpj_idx on fnet_document (fund_cnpj);

comment on table fnet_document is 'FundosNET "Aviso aos Cotistas - Estruturado" documents seen; drives re-download on new versions.';
comment on column fnet_document.status is 'FundosNET situacaoDocumento: A = active, C = cancelled, I = inactive. Only A counts.';

alter table distribution add column fnet_version integer;
comment on column distribution.fnet_version is 'Version of the FundosNET document the row was parsed from.';

-- migrate:down

alter table distribution drop column fnet_version;
drop table fnet_document;
drop table watchlist;
