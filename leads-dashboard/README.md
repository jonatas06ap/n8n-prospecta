# Painel de leads (Streamlit)

Duas áreas:

1. **Disparar n8n** — escolhe o fluxo (Serper+CNPJ ou Places), preenche o formulário e envia `POST` JSON ao webhook.
2. **Leads** — lê a tabela no Postgres, ordena por `score` (maior primeiro), filtros por setor, região, workflow, período e texto; paginação 50; export CSV; checkbox **Abordado** grava na base.

## Pré-requisitos

- Python 3.11+ (ou Docker)
- Postgres com a tabela de leads (colunas que já indicou + `abordado`)

### Coluna `abordado`

Execute a migração (ajuste o nome da tabela no ficheiro se não for `leads`):

```bash
PGSSLMODE=require psql -h aws-0-us-west-1.pooler.supabase.com -p 5432 -d postgres -U postgres.SEU_REF -f migrations/001_add_abordado.sql
```

Ou manualmente:

```sql
ALTER TABLE leads ADD COLUMN IF NOT EXISTS abordado boolean NOT NULL DEFAULT false;
```

## Configuração

```bash
cp .env.example .env
# edite .env — ver comentários no .env.example
```

Variáveis importantes:

| Variável | Descrição |
|----------|-----------|
| `APP_USER` / `APP_PASSWORD` | Login do painel (**obrigatório** definir senha) |
| `DATABASE_URL` ou `POSTGRES_URL` | URI Postgres (tem prioridade sobre `POSTGRES_HOST`, etc.) |
| `POSTGRES_*` | Host, porta, utilizador, senha, base — só se não houver URL |
| `POSTGRES_SSLMODE` | Ex.: `require` se a URI não tiver `?sslmode=` |
| `POSTGRES_LEADS_TABLE` | Nome da tabela (padrão `leads`) |
| `WEBHOOK_SERPER_CNPJ` | Webhook Serper+CNPJ → `forms_maps` |
| `WEBHOOK_PLACES` | Webhook Places → `forms_cnpj` |

## Executar localmente

```bash
cd leads-dashboard
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Abra `http://localhost:8501`.

## Executar com Docker

```bash
cd leads-dashboard
cp .env.example .env   # e edite
docker compose up -d --build
```

- **Postgres na cloud (Supabase):** normalmente só precisas de `DATABASE_URL` no `.env`; o contentor acede ao host `*.pooler.supabase.com` pela Internet.
- **n8n no host** e Streamlit em Docker: nas `WEBHOOK_*` usa o IP da máquina na LAN ou `http://host.docker.internal:5678/...` (o `docker-compose` já define `extra_hosts` no Linux).

## Supabase (Session pooler + Docker)

No painel: **Connect → PSQL → Session pooler**. Esse modo costuma ser **IPv4 compatível** e funciona bem **a partir de contentores Docker**.

Exemplo de `psql` (ajuste região, ref e credenciais):

```bash
PGSSLMODE=require psql -h aws-0-us-west-1.pooler.supabase.com -p 5432 -d postgres -U postgres.wuntfeozkbqieivojqrt
```

Equivalente no `.env` do painel (mesmos host, porta, utilizador e base):

```env
DATABASE_URL=postgresql://postgres.wuntfeozkbqieivojqrt:SUA_SENHA@aws-0-us-west-1.pooler.supabase.com:5432/postgres?sslmode=require
```

- Utilizador do pooler: **`postgres.<PROJECT_REF>`**, não só `postgres`.
- Host: **`aws-0-<região>.pooler.supabase.com`** (vem no modal).
- Porta: a que o Supabase mostrar (muitas vezes **5432**).
- **Palavra-passe com caracteres especiais** (`@`, `#`, espaços): use [percent-encoding](https://en.wikipedia.org/wiki/Percent-encoding) na URI **ou** deixe `DATABASE_URL` vazio e use `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, etc.

Ligação direta `db.<ref>.supabase.co` é alternativa ao pooler.

## Webhooks e payloads

- **Serper + CNPJ.ws** → `WEBHOOK_SERPER_CNPJ` (ex. `/webhook/forms_maps`):

```json
{"regiao":"São Paulo","setor":"Advocacia","num_procuras":2,"extra":"Cível"}
```

- **Places** → `WEBHOOK_PLACES` (ex. `/webhook/forms_cnpj`):

```json
{"regiao":"São Paulo","setor":"Advocacia"}
```

**Nota:** no n8n o workflow tem de estar **ativo** (produção). Em modo teste o webhook só aceita uma chamada após clicar em *Execute workflow*.

## Estrutura do repositório

O stack do n8n está em `../n8n-stack/` (volume `../n8n_data`).
