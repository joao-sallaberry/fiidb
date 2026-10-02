# fiidb

Dados de fundos imobiliários (FII) listados na B3, apenas de fontes públicas gratuitas (CVM Dados Abertos, B3
COTAHIST, B3 FundosNET). Um pipeline de ingestão em Python grava no Postgres. Uma API em TypeScript (Hono + Kysely)
virá depois. O consumidor inicial é uma planilha Google Sheets com tabelas grandes (muitos fundos × algumas
colunas), alimentada por snapshots estáticos no Cloudflare R2: a máquina que roda o pipeline nem sempre está ligada.

Usuários: o autor e alguns conhecidos, sem cobrança. O autor conversa em português.

Arquitetura da ingestão, fontes → tabelas e armadilhas dos dados: **docs/ingestion.md**. Leia antes de mexer no
pipeline.

## Documentação sempre atualizada

Toda mudança que altere comportamento atualiza a documentação **na mesma alteração**, sem esperar ser pedido:

| Mudou… | Atualize |
|---|---|
| fonte, tabela, regra de carga, vínculo, classificação, armadilha nova de dados | `docs/ingestion.md` |
| tabela ou coluna | `comment on` na migração que a cria ou altera (`\d+` no psql mostra) |
| comando, variável de ambiente, convenção, estrutura de pastas, fase do plano | este arquivo |
| forma de rodar o projeto | `README.md` |

Antes de encerrar uma tarefa, confira se os números e exemplos citados nos docs ainda batem (contagens,
tamanhos, nomes de arquivos).

## Estrutura

```
db/migrations/          SQL puro (dbmate): schema, views e comentários. O banco é o contrato Python ↔ TS.
docs/ingestion.md       arquitetura da ingestão
seeds/fund_overrides.csv correções manuais por ticker (categoria/segmento); recarregado a cada catch-up
pipeline/               projeto Python (uv), pacote `fiidb`
  src/fiidb/
    cli.py              comandos Typer (`fiidb ...`)
    config.py           variáveis de ambiente → Settings
    db.py               connect (autocommit), upsert via COPY + staging, ingestion_run
    http.py             httpx com retry e cache em disco para arquivos imutáveis
    dates.py            today()/now() no fuso de São Paulo, parse_date
    linking.py          vínculo ticker (security) → fundo (fund)
    seeds.py            carga de seeds/
    sources/            um módulo por fonte: cvm_fii, b3_cotahist, fnet (só parser por enquanto)
  tests/                test_parsers.py (sem banco), test_load.py (com banco), fixtures/ (amostras reais)
docker-compose.yml      postgres + migrate (dbmate, profile "tools")
```

## Comandos

```sh
docker compose up -d postgres                 # Postgres 16 em 127.0.0.1:5432 (fiidb/fiidb)
docker compose run --rm migrate               # aplica db/migrations
docker compose run --rm migrate --wait rollback   # desfaz a última migração
docker compose exec postgres psql -U fiidb fiidb

cd pipeline
uv run fiidb catch-up                         # idempotente; baixa só o que falta (roda no boot/diariamente)
uv run fiidb status                           # contagens + última execução por fonte
uv run fiidb cvm-fii 2025 --force             # recarrega um ano da CVM mesmo sem mudança
uv run fiidb cotahist --year 2024             # ou --day 2026-10-01
uv run fiidb seed                             # recarrega seeds/fund_overrides.csv

uv run pytest                                 # parsers
FIIDB_TEST_DATABASE_URL=postgresql://fiidb:fiidb@localhost:5432/fiidb_test uv run pytest   # + carga
uv run ruff check src tests && uv run ruff format src tests
```

`fiidb_test` precisa existir (`create database fiidb_test`). **Os testes de carga apagam o schema `public` do banco
indicado**; nunca aponte para `fiidb`.

## Variáveis de ambiente

| Variável | Padrão | Uso |
|---|---|---|
| `DATABASE_URL` | `postgresql://fiidb:fiidb@localhost:5432/fiidb` | banco do pipeline |
| `FIIDB_START_YEAR` | `2016` | primeiro ano carregado (CVM e COTAHIST) |
| `FIIDB_CACHE_DIR` | `~/.cache/fiidb` | COTAHIST anual de anos fechados (~90 MB cada) |
| `FIIDB_SEEDS_DIR` | `<repo>/seeds` | arquivos de seed |
| `FIIDB_TEST_DATABASE_URL` | — | habilita `tests/test_load.py` |
| `POSTGRES_PASSWORD`, `POSTGRES_PORT` | `fiidb`, `5432` | docker compose (`.env`) |

## Convenções

- **Banco:** migrações em SQL puro, sem ORM. Lógica derivada (classificação, métricas) fica em views SQL, não no
  Python, para a futura API usar a mesma regra. Migração já aplicada não se edita: crie uma nova com timestamp
  `AAAAMMDDHHMMSS_descricao.sql` e seções `-- migrate:up` / `-- migrate:down` (e teste o rollback). Ao recriar uma
  view com `drop` + `create`, recrie também o `comment on view`.
- **Carga:** cada arquivo de origem é uma transação (`with conn.transaction()` numa conexão em autocommit) e gera um
  registro em `ingestion_run`. Use `db.upsert`, que exige linhas sem chave repetida no lote. A carga tem que ser
  idempotente: rodar duas vezes não muda nada.
- **Dados sujos:** a CVM não valida o que os administradores enviam. Nada pode derrubar a carga de um ano inteiro.
  Valores implausíveis viram NULL e o original fica em `monthly_report.raw`. Colunas da CVM são `numeric` sem
  precisão fixa.
- **Datas:** mercado no fuso de São Paulo (`fiidb.dates.today()`); timestamps em UTC (`dates.now()`). Nunca
  `date.today()` / `datetime.now()` sem fuso (o ruff acusa).
- **Fonte nova:** módulo em `sources/` com `parse_*` puro (testado com fixture real em `tests/fixtures/`, cortada
  para poucos KB) e `load`/`ingest_*` que usa `db.upsert` + `db.record_run`; ligue no `catch-up` do `cli.py`.
  Arquivos CVM/B3 são latin-1: ao recortar fixtures com grep use `LC_ALL=C grep -a`.
- **Escopo:** somente FII (BDI 12) por enquanto; Fiagro/FI-Infra depois (`fund.type` já existe). Sem fontes pagas,
  sem dados intraday.
- **Idioma:** código, comentários e `comment on` em inglês; documentação (`docs/`, README, este arquivo) em
  português.
- **Git:** branch `master`, sem remoto. Commits só quando pedido.

## Ambiente local

- Docker exige que o usuário esteja no grupo `docker` **no processo atual**. Se aparecer "permission denied" no
  socket, a sessão foi aberta antes de entrar no grupo: reinicie a sessão.
- O dbmate roda como root no container, por isso o dump `db/schema.sql` está desligado
  (`DBMATE_NO_DUMP_SCHEMA`).
- Primeira carga completa: ~1 GB de download (COTAHIST) e alguns minutos. Com o cache, ~1 minuto. Banco ~200 MB.
- O COTAHIST diário sai por volta das 20h30; antes disso, o 404 do dia é esperado.

## Plano

1. ✅ Fundação: docker compose, migrações, projeto Python
2. ✅ CVM informe mensal + COTAHIST (histórico desde 2016 + diário); classificação `fund_profile`
3. Proventos via FundosNET (~35 mil avisos de FII; também melhora o vínculo ticker → fundo) e métricas em SQL
   (P/VP, DY 12m, liquidez média)
4. Publicação de snapshots JSON/CSV no R2 + Apps Script que grava valores na planilha
5. Fiagro e FI-Infra
6. API Hono/TS com tokens; deploy (Oracle Always Free ou VPS); frontend

Pendências conhecidas: ~45 tickers negociados sem fundo vinculado (a fase 3 deve resolver); segmento vazio para
~80 fundos de tijolo negociados que se declaram "Multicategoria" (preencher via `seeds/fund_overrides.csv`).
