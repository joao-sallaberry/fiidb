# fiidb

Armazena e serve dados de fundos imobiliários (FII) listados na B3, usando apenas fontes públicas gratuitas:

| Dado | Fonte |
|---|---|
| Cadastro e informe mensal (PL, VP/cota, cotistas, DY) | [CVM Dados Abertos](https://dados.cvm.gov.br/dataset/fii-doc-inf_mensal) |
| Cotações diárias | B3 COTAHIST |
| Proventos | B3 FundosNET — "Aviso aos Cotistas – Estruturado" (em breve) |

## Rodando localmente

Requisitos: Docker e [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env
docker compose up -d postgres
docker compose run --rm migrate          # aplica db/migrations

cd pipeline
uv run fiidb catch-up                    # baixa tudo que falta (1ª vez: ~1 GB de COTAHIST)
uv run fiidb status
```

`catch-up` é idempotente: pode rodar no boot e diariamente. Outros comandos: `fiidb cvm-fii <ano>`,
`fiidb cotahist --year <ano> | --day <AAAA-MM-DD>`.

## Estrutura

Arquitetura da ingestão (fontes, tabelas, armadilhas dos dados): [docs/ingestion.md](docs/ingestion.md).


- `db/migrations/` — schema em SQL puro (dbmate); o banco é o contrato entre o pipeline e a futura API.
- `pipeline/` — ingestão em Python (`uv run pytest`; com `FIIDB_TEST_DATABASE_URL` apontando para um banco
  descartável, roda também os testes de carga).
- `seeds/fund_overrides.csv` — correções manuais de categoria/segmento por ticker (recarregado no `catch-up` ou com
  `fiidb seed`).

## Classificação dos fundos

A view `fund_profile` deriva a categoria da composição do ativo no informe mensal mais recente
(≥ 60% em imóveis → Tijolo, ou Desenvolvimento se a maior parte for para venda; ≥ 60% em CRI/LCI/etc. → Papel;
≥ 60% em cotas de FII → FoF; senão Híbrido). O segmento declarado à CVM só é usado para fundos de tijolo e quando é
específico (não "Multicategoria"/"Outros"). O que estiver em `seeds/fund_overrides.csv` prevalece.

## Licença

O código é licenciado sob a [GNU AGPL v3.0 ou posterior](LICENSE). Se você modificar o projeto e oferecê-lo como
serviço pela rede (API, site), precisa disponibilizar o código-fonte das suas modificações aos usuários.

A licença cobre o código, não os dados. Os dados pertencem às fontes e seguem os termos de cada uma:

- **CVM Dados Abertos**: [Open Data Commons ODbL](https://opendatacommons.org/licenses/odbl/). Bancos derivados
  distribuídos publicamente exigem atribuição à CVM e a mesma licença.
- **B3** (COTAHIST, FundosNET): termos de uso da B3; a redistribuição pública de dados de mercado pode ter
  restrições.

Os arquivos em `pipeline/tests/fixtures/` são pequenos recortes dessas fontes, usados apenas para teste.
