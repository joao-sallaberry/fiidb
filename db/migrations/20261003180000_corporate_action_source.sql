-- migrate:up

alter table corporate_action add column source text not null default 'b3' check (source in ('b3', 'manual'));
comment on column corporate_action.source is 'b3 = B3 listed-fund page; manual = seeds/corporate_actions.csv (events B3 does not list).';

-- migrate:down

alter table corporate_action drop column source;
