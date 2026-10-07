"""
Painel Streamlit: disparo de webhooks n8n e listagem de leads no Postgres.
Credenciais e URLs via variáveis de ambiente (ficheiro .env carregado com python-dotenv).
"""

from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import psycopg2
import requests
import streamlit as st
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from streamlit_autorefresh import st_autorefresh

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")


def _resolve_pg_settings() -> dict[str, Any]:
    """
    Prioridade: DATABASE_URL ou POSTGRES_URL (string de conexão).
    Se não existir, usa POSTGRES_HOST, POSTGRES_PORT, etc.
    sslmode: query string (?sslmode=require) ou POSTGRES_SSLMODE.
    """
    url = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
    sslmode_env = os.getenv("POSTGRES_SSLMODE", "").strip() or None

    if url:
        if url.startswith("postgres://") and not url.startswith("postgresql://"):
            url = "postgresql://" + url[len("postgres://") :]
        p = urlparse(url)
        host = p.hostname or "localhost"
        port = p.port or 5432
        user = unquote(p.username or "") if p.username else os.getenv("POSTGRES_USER", "postgres")
        password = unquote(p.password or "") if p.password is not None else os.getenv("POSTGRES_PASSWORD", "")
        dbname = (p.path or "").lstrip("/") or "postgres"
        qs = parse_qs(p.query)
        sslmode_q = (qs.get("sslmode") or [None])[0]
        sslmode = sslmode_q or sslmode_env
        return {
            "host": host,
            "port": int(port),
            "dbname": dbname,
            "user": user,
            "password": password,
            "sslmode": sslmode,
        }

    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DB", "leads"),
        "user": os.getenv("POSTGRES_USER", "postgres"),
        "password": os.getenv("POSTGRES_PASSWORD", ""),
        "sslmode": sslmode_env,
    }


_PG = _resolve_pg_settings()
LEADS_TABLE = os.getenv("POSTGRES_LEADS_TABLE", "leads")

WEBHOOK_SERPER_CNPJ = os.getenv(
    "WEBHOOK_SERPER_CNPJ",
    "http://192.168.200.113:5678/webhook/forms_maps",
)
WEBHOOK_PLACES = os.getenv(
    "WEBHOOK_PLACES",
    "http://192.168.200.113:5678/webhook/forms_cnpj",
)

PAGE_SIZE = 50
AUTO_REFRESH_MS = 600_000  # 10 minutos

# Mapeamento CNAE: letra da seção -> descrição (para regra de setor)
DICIONARIO_SETORES: dict[str, str] = {
    "A": "Agricultura, Pecuária, Produção Florestal, Pesca e Aquicultura",
    "B": "Indústrias Extrativas",
    "C": "Indústria de Transformação",
    "D": "Eletricidade e Gás",
    "E": "Água, Esgoto, Gestão de Resíduos",
    "F": "Construção",
    "G": "Comércio, Reparação de Veículos e Motocicletas",
    "H": "Transporte, Armazenagem e Correio",
    "I": "Alojamento e Alimentação",
    "J": "Informação e Comunicação (Tecnologia)",
    "K": "Atividades Financeiras, de Seguros e Serviços Relacionados",
    "L": "Atividades Imobiliárias",
    "M": "Atividades Profissionais, Científicas e Técnicas",
    "N": "Atividades Administrativas e Serviços Complementares",
    "O": "Administração Pública, Defesa e Seguridade Social",
    "P": "Educação",
    "Q": "Saúde Humana e Serviços Sociais",
    "R": "Artes, Cultura, Esporte e Recreação",
    "S": "Outras Atividades de Serviços",
    "T": "Serviços Domésticos",
    "U": "Organismos Internacionais e Outras Instituições",
}


def _setor_para_secao(setor_catalogo: str | None) -> str | None:
    """Retorna a letra da seção CNAE (A-U) a partir de setor_catalogo, ou None."""
    if not setor_catalogo or not setor_catalogo.strip():
        return None
    sc = setor_catalogo.strip().lower()
    for letra, desc in DICIONARIO_SETORES.items():
        d = desc.lower()
        if d in sc or sc in d or sc.startswith(d[:25]) or d.startswith(sc[:25]):
            return letra
    return None


def calc_lead_score(row: dict[str, Any]) -> tuple[int, str, str, list[str]]:
    """
    Calcula score do lead (0–100), status (Quente/Morno/Frio/Descartado) e motivos.
    Retorna (score, status, motivo_descarte, motivos_score).
    motivo_descarte só preenchido quando Descartado.
    """
    motivos: list[str] = []
    email = (row.get("email") or "").strip()
    telefone = (row.get("telefone") or "").strip()
    telefone2 = (row.get("telefone2") or "").strip()
    tem_contato = bool(email or telefone or telefone2)
    situacao = (row.get("situacao_cadastral") or "").strip()
    mei = (row.get("mei") or "").strip()
    # Knockout: empresa inativa (exceto se tiver contato)
    if not tem_contato and situacao and situacao != "Ativa":
        return 0, "Descartado", "Empresa Inativa ou Baixada", []
    # Knockout: MEI (exceto se tiver email ou algum telefone)
    if not tem_contato and mei == "Sim":
        return 0, "Descartado", "É MEI", []
    score = 0
    # Regra 1: Idade (máx 30)
    idade_val = row.get("idade")
    if idade_val is not None:
        try:
            idade = int(idade_val)
            if idade > 10:
                score += 30
                motivos.append("+30: Mais de 10 anos de mercado")
            elif idade > 3:
                score += 20
                motivos.append("+20: Mais de 3 anos de mercado")
            elif idade >= 1:
                score += 10
                motivos.append("+10: Tem entre 1 e 3 anos")
            else:
                motivos.append("0: Empresa com menos de 1 ano (Risco)")
        except (ValueError, TypeError):
            pass
    # Regra 2: Porte (máx 30)
    porte = (row.get("porte") or "").strip()
    if porte:
        if "Demais" in porte or porte == "Demais":
            score += 30
            motivos.append("+30: Porte 'Demais' (Provável Médio/Grande)")
        elif "Pequeno Porte" in porte or porte == "Empresa de Pequeno Porte":
            score += 20
            motivos.append("+20: Empresa de Pequeno Porte (EPP)")
        elif "Micro" in porte or porte == "Micro Empresa":
            score += 10
            motivos.append("+10: Micro Empresa")
    # Regra 2b: Simples (não no Simples = maior faturamento)
    simples_val = (row.get("simples") or "").strip()
    if simples_val == "Não":
        score += 15
        motivos.append("+15: Não é do Simples (Lucro Real/Presumido, maior potencial)")
    # Regra 3: Setor CNAE (máx 30)
    secao = _setor_para_secao(row.get("setor_catalogo"))
    if secao:
        if secao in ("J", "C"):
            score += 30
            motivos.append("+30: Setor Alvo (Tecnologia/Indústria)")
        else:
            score += 10
            motivos.append("+10: Setor Genérico")
    # Regra 4: Qualidade do contato (máx 10)
    if email:
        score += 5
        motivos.append("+5: Possui E-mail cadastrado")
    else:
        motivos.append("0: Sem E-mail")
    telefone = (row.get("telefone") or "").strip()
    if telefone:
        score += 5
        motivos.append("+5: Possui Telefone")
    score = min(score, 100)
    if score >= 80:
        status = "Quente 🔥"
    elif score >= 50:
        status = "Morno 🟡"
    else:
        status = "Frio ❄️"
    return score, status, "", motivos


def get_connection():
    kwargs: dict[str, Any] = {
        "host": _PG["host"],
        "port": _PG["port"],
        "dbname": _PG["dbname"],
        "user": _PG["user"],
        "password": _PG["password"],
        "connect_timeout": 10,
    }
    if _PG.get("sslmode"):
        kwargs["sslmode"] = _PG["sslmode"]
    return psycopg2.connect(**kwargs)


def table_ident():
    """Identificador SQL seguro para o nome da tabela (apenas letras/números/_)."""
    if not LEADS_TABLE or not LEADS_TABLE.replace("_", "").isalnum():
        raise ValueError("POSTGRES_LEADS_TABLE inválido")
    return sql.Identifier(LEADS_TABLE)


def login_form() -> None:
    st.title("Leads — Acesso")
    if not APP_PASSWORD:
        st.error(
            "Defina APP_PASSWORD no ambiente (.env). Sem senha o painel não arranca por segurança."
        )
        st.stop()

    with st.form("login"):
        user = st.text_input("Utilizador")
        password = st.text_input("Palavra-passe", type="password")
        submitted = st.form_submit_button("Entrar")
        if submitted:
            if user == APP_USER and password == APP_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Credenciais inválidas.")


def require_auth() -> None:
    if not st.session_state.get("authenticated"):
        login_form()
        st.stop()


def post_webhook(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        r = requests.post(url, json=payload, timeout=60)
        body = r.text[:2000]
        if r.ok:
            return True, f"HTTP {r.status_code}\n{body}"
        return False, f"HTTP {r.status_code}\n{body}"
    except requests.RequestException as e:
        return False, str(e)


def tab_disparar() -> None:
    st.subheader("Disparar fluxo no n8n")
    st.caption(
        "Serper + CNPJ.ws → webhook `forms_cnpj`. Places → webhook `forms_maps`. "
        "Ative o workflow em produção no n8n (não só modo teste), senão o webhook pode devolver 404."
    )

    fluxo = st.radio(
        "Fluxo",
        options=[
            ("serper_cnpj", "Serper + CNPJ.ws (forms_cnpj)"),
            ("places", "Places API (forms_maps)"),
        ],
        format_func=lambda x: x[1],
        horizontal=True,
    )
    fluxo_id = fluxo[0]

    if fluxo_id == "serper_cnpj":
        st.text_input("URL", value=WEBHOOK_PLACES, disabled=True)
        with st.form("f_serper"):
            regiao = st.text_input("Região", placeholder="São Paulo")
            setor = st.text_input("Setor", placeholder="Advocacia")
            num_procuras = st.number_input(
                "Número de procuras (num_procuras)", min_value=1, value=1, step=1
            )
            extra = st.text_input("Extra", placeholder="Cível")
            go = st.form_submit_button("Enviar para n8n")
        if go:
            payload = {
                "regiao": regiao.strip(),
                "setor": setor.strip(),
                "num_procuras": int(num_procuras),
                "extra": extra.strip(),
            }
            ok, msg = post_webhook(WEBHOOK_PLACES, payload)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
    else:
        st.text_input("URL", value=WEBHOOK_SERPER_CNPJ, disabled=True)
        with st.form("f_places"):
            regiao = st.text_input("Região", placeholder="São Paulo", key="p_regiao")
            setor = st.text_input("Setor", placeholder="Advocacia", key="p_setor")
            go = st.form_submit_button("Enviar para n8n")
        if go:
            payload = {"regiao": regiao.strip(), "setor": setor.strip()}
            ok, msg = post_webhook(WEBHOOK_SERPER_CNPJ, payload)
            if ok:
                st.success(msg)
            else:
                st.error(msg)


def fetch_distinct(column: str) -> list[str]:
    tid = table_ident()
    q = sql.SQL("SELECT DISTINCT {col} AS v FROM {t} WHERE {col} IS NOT NULL AND TRIM({col}) <> '' ORDER BY 1").format(
        col=sql.Identifier(column),
        t=tid,
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(q)
            return [r[0] for r in cur.fetchall()]


def build_filters_sql(
    setores_pesquisa: list[str],
    setores_catalogo: list[str],
    regioes: list[str],
    workflows: list[str],
    portes: list[str],
    meis: list[str],
    situacoes: list[str],
    idade_min: int | None,
    idade_max: int | None,
    texto: str,
    d_ini: date | None,
    d_fim: date | None,
) -> tuple[sql.Composed, list[Any]]:
    tid = table_ident()
    # Exclui leads sem telefone, telefone2 e email
    parts: list[sql.Composable] = [
        sql.SQL("(TRIM(COALESCE(telefone,'')) <> '' OR TRIM(COALESCE(telefone2,'')) <> '' OR TRIM(COALESCE(email,'')) <> '')")
    ]
    params: list[Any] = []

    if setores_pesquisa:
        parts.append(sql.SQL("setor = ANY(%s)"))
        params.append(setores_pesquisa)
    if setores_catalogo:
        parts.append(sql.SQL("setor_catalogo = ANY(%s)"))
        params.append(setores_catalogo)
    if regioes:
        parts.append(sql.SQL("regiao = ANY(%s)"))
        params.append(regioes)
    if workflows:
        parts.append(sql.SQL("workflow = ANY(%s)"))
        params.append(workflows)
    if portes:
        parts.append(sql.SQL("porte = ANY(%s)"))
        params.append(portes)
    if meis:
        parts.append(sql.SQL("mei = ANY(%s)"))
        params.append(meis)
    if situacoes:
        parts.append(sql.SQL("situacao_cadastral = ANY(%s)"))
        params.append(situacoes)
    if idade_min is not None:
        parts.append(sql.SQL("(idade IS NOT NULL AND idade >= %s)"))
        params.append(idade_min)
    if idade_max is not None:
        parts.append(sql.SQL("(idade IS NOT NULL AND idade <= %s)"))
        params.append(idade_max)

    if d_ini is not None:
        start = datetime.combine(d_ini, time.min, tzinfo=timezone.utc)
        parts.append(sql.SQL("created_at >= %s"))
        params.append(start)
    if d_fim is not None:
        end = datetime.combine(d_fim, time.max, tzinfo=timezone.utc)
        parts.append(sql.SQL("created_at <= %s"))
        params.append(end)

    if texto.strip():
        like = f"%{texto.strip()}%"
        parts.append(
            sql.SQL(
                "("
                "nome_empresa ILIKE %s OR telefone ILIKE %s OR telefone2 ILIKE %s OR "
                "email ILIKE %s OR website ILIKE %s OR descricao ILIKE %s OR cnpj ILIKE %s OR "
                "id ILIKE %s OR atividade_principal ILIKE %s OR atividades_secundarias ILIKE %s OR "
                "porte ILIKE %s OR setor_catalogo ILIKE %s OR situacao_cadastral ILIKE %s"
                ")"
            )
        )
        params.extend([like] * 13)

    where = sql.SQL(" AND ").join(parts)
    return where, params


def count_leads(
    where_sql: sql.Composed,
    params: list[Any],
) -> int:
    tid = table_ident()
    q = sql.SQL("SELECT COUNT(*) FROM {t} WHERE ").format(t=tid) + where_sql
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            return int(cur.fetchone()[0])


def fetch_leads_filtered(
    where_sql: sql.Composed,
    params: list[Any],
    max_rows: int = 50_000,
) -> list[dict[str, Any]]:
    """Busca leads com filtros, sem paginação (para ordenar por score calculado)."""
    tid = table_ident()
    q = (
        sql.SQL(
            "SELECT id, nome_empresa, telefone, telefone2, email, website, descricao, workflow, "
            "score, setor, regiao, cnpj, atividade_principal, atividades_secundarias, "
            "porte, setor_catalogo, situacao_cadastral, idade, simples, mei, "
            "created_at, COALESCE(abordado, false) AS abordado "
            "FROM {t} WHERE "
        ).format(t=tid)
        + where_sql
        + sql.SQL(" ORDER BY created_at DESC NULLS LAST LIMIT %s")
    )
    p = list(params) + [max_rows]
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, p)
            return [dict(row) for row in cur.fetchall()]


def fetch_leads_page_sorted_by_score(
    where_sql: sql.Composed,
    params: list[Any],
    offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Busca leads filtrados, calcula score, ordena por score (maior primeiro) e aplica paginação."""
    all_rows = fetch_leads_filtered(where_sql, params)
    scored = [(row, calc_lead_score(row)[0]) for row in all_rows]
    scored.sort(
        key=lambda x: (
            x[1],
            x[0].get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    start = offset
    end = offset + limit
    return [r[0] for r in scored[start:end]]


def fetch_leads_all_for_export(
    where_sql: sql.Composed,
    params: list[Any],
    max_rows: int = 50_000,
) -> list[dict[str, Any]]:
    tid = table_ident()
    q = (
        sql.SQL(
            "SELECT id, nome_empresa, telefone, telefone2, email, website, descricao, workflow, "
            "score, setor, regiao, cnpj, atividade_principal, atividades_secundarias, "
            "porte, setor_catalogo, situacao_cadastral, idade, simples, mei, "
            "created_at, COALESCE(abordado, false) AS abordado "
            "FROM {t} WHERE "
        ).format(t=tid)
        + where_sql
        + sql.SQL(" ORDER BY score DESC NULLS LAST, created_at DESC NULLS LAST LIMIT %s")
    )
    p = list(params) + [max_rows]
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, p)
            return [dict(row) for row in cur.fetchall()]


def update_abordado(lead_id: str, value: bool) -> None:
    tid = table_ident()
    q = sql.SQL("UPDATE {t} SET abordado = %s WHERE id = %s").format(t=tid)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (value, lead_id))
        conn.commit()


def tab_leads() -> None:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="leads_autorefresh")

    st.subheader("Leads")
    c1, c2 = st.columns([1, 4])
    with c1:
        if st.button("Atualizar agora", use_container_width=True):
            st.rerun()
    with c2:
        mins = AUTO_REFRESH_MS // 60_000
        secs = (AUTO_REFRESH_MS % 60_000) // 1000
        if mins >= 1:
            refresh_label = f"{mins} min" + (f" {secs}s" if secs else "")
        else:
            refresh_label = f"{AUTO_REFRESH_MS // 1000}s"
        st.caption(
            f"Atualização automática a cada {refresh_label}. Score e status calculados em tempo real."
        )

    try:
        opts_setor_pesquisa = fetch_distinct("setor")
        opts_setor_catalogo = fetch_distinct("setor_catalogo")
        opts_regiao = fetch_distinct("regiao")
        opts_workflow = fetch_distinct("workflow")
        opts_porte = fetch_distinct("porte")
        opts_mei = ["Sim", "Não"]
        opts_situacao = fetch_distinct("situacao_cadastral")
    except Exception as e:
        st.error(f"Erro ao ler opções de filtro: {e}")
        st.stop()

    with st.expander("Filtros", expanded=True):
        fc1, fc2 = st.columns(2)
        with fc1:
            sel_setor_pesquisa = st.multiselect("Setor (Pesquisa)", options=opts_setor_pesquisa, default=[])
            sel_setor = st.multiselect("Setor", options=opts_setor_catalogo, default=[])
            sel_regiao = st.multiselect("Local (região)", options=opts_regiao, default=[])
            sel_porte = st.multiselect("Porte", options=opts_porte, default=[])
            sel_workflow = st.multiselect("Workflow", options=opts_workflow, default=[])
        with fc2:
            sel_mei = st.multiselect("MEI", options=opts_mei, default=[])
            sel_situacao = st.multiselect("Situação cadastral", options=opts_situacao, default=[])
            idade_min = st.number_input("Idade mín. (anos)", min_value=0, value=0, step=1, key="idade_min", help="0 = sem mínimo")
            idade_max = st.number_input("Idade máx. (anos)", min_value=0, value=0, step=1, key="idade_max", help="0 = sem máximo")
            texto = st.text_input("Busca (nome, telefone, email, site, descrição, CNPJ, atividades, porte, setor)")

        usar_periodo = st.checkbox("Filtrar por período", value=False)
        d_ini: date | None = None
        d_fim: date | None = None
        if usar_periodo:
            dr1, dr2 = st.columns(2)
            with dr1:
                d_ini = st.date_input(
                    "Período — início",
                    value=date.today() - timedelta(days=30),
                )
            with dr2:
                d_fim = st.date_input("Período — fim", value=date.today())

    _idade_min: int | None = int(idade_min) if idade_min and idade_min > 0 else None
    _idade_max: int | None = int(idade_max) if idade_max and idade_max > 0 else None
    where_sql, params = build_filters_sql(
        sel_setor_pesquisa,
        sel_setor,
        sel_regiao,
        sel_workflow,
        sel_porte,
        sel_mei,
        sel_situacao,
        _idade_min,
        _idade_max,
        texto,
        d_ini,
        d_fim,
    )

    try:
        total = count_leads(where_sql, params)
    except Exception as e:
        st.error(
            f"Erro ao contar leads: {e}\n\n"
            "Confirme a tabela POSTGRES_LEADS_TABLE e se a coluna `abordado` existe "
            "(veja migrations/001_add_abordado.sql)."
        )
        st.stop()

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = st.number_input("Página", min_value=1, max_value=pages, value=1, step=1)
    st.write(f"Total: **{total}** leads (mostrando {PAGE_SIZE} por página).")

    if st.button("Gerar CSV (até 50k linhas com os filtros atuais)"):
        try:
            rows_exp = fetch_leads_filtered(where_sql, params)
            rows_exp = [r[0] for r in sorted(
                [(row, calc_lead_score(row)[0]) for row in rows_exp],
                key=lambda x: (x[1], x[0].get("created_at") or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True,
            )]
            buf = io.StringIO()
            if rows_exp:
                w = csv.DictWriter(buf, fieldnames=list(rows_exp[0].keys()))
                w.writeheader()
                w.writerows(rows_exp)
            st.session_state["_csv_export_bytes"] = buf.getvalue().encode("utf-8")
            st.session_state["_csv_export_name"] = (
                f"leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
        except Exception as e:
            st.session_state.pop("_csv_export_bytes", None)
            st.session_state.pop("_csv_export_name", None)
            st.error(f"Exportação falhou: {e}")

    csv_bytes = st.session_state.get("_csv_export_bytes")
    csv_name = st.session_state.get("_csv_export_name")
    if csv_bytes and csv_name:
        st.download_button(
            label="Descarregar CSV",
            data=csv_bytes,
            file_name=csv_name,
            mime="text/csv",
        )
        if st.button("Limpar exportação"):
            st.session_state.pop("_csv_export_bytes", None)
            st.session_state.pop("_csv_export_name", None)
            st.rerun()

    offset = (int(page) - 1) * PAGE_SIZE
    try:
        rows = fetch_leads_page_sorted_by_score(where_sql, params, offset, PAGE_SIZE)
    except Exception as e:
        st.error(f"Erro ao carregar leads: {e}")
        st.stop()

    if not rows:
        st.info("Nenhum lead com estes filtros.")
        return

    def _make_abordado_callback(lead_id: str, key: str):
        def _cb() -> None:
            new_val = bool(st.session_state[key])
            try:
                update_abordado(lead_id, new_val)
            except Exception as ex:
                st.session_state["_last_db_err"] = str(ex)

        return _cb

    for row in rows:
        rid = str(row["id"])
        chk_key = f"abordado_chk_{rid}"
        st.session_state[chk_key] = bool(row.get("abordado"))
        lead_score, status_lead, motivo_descarte, motivos_score = calc_lead_score(row)
        simples_raw = (row.get("simples") or "").strip()
        simples_label = "No Simples" if simples_raw == "Sim" else ("Fora do Simples" if simples_raw == "Não" else (simples_raw or "—"))

        with st.container(border=True):
            h1, h2 = st.columns([1, 6])
            with h1:
                st.checkbox(
                    "Abordado",
                    key=chk_key,
                    on_change=_make_abordado_callback(rid, chk_key),
                )
            with h2:
                nome = row.get("nome_empresa") or "(sem nome)"
                st.markdown(f"**{nome}** — **Score:** {lead_score} — **Status:** {status_lead} — workflow: `{row.get('workflow')}`")
                if motivo_descarte:
                    st.caption(f"Descartado: {motivo_descarte}")
                elif motivos_score:
                    with st.expander("Ver motivos do score", expanded=False):
                        for m in motivos_score:
                            st.caption(m)
            # Grid 5 colunas × 3 linhas — mesmo número de elementos por coluna
            c1, c2, c3, c4, c5 = st.columns(5)
            with c1:
                st.caption("**Telefone**")
                st.text(row.get("telefone") or "—")
                st.caption("**Setor (Pesquisa)**")
                st.text(row.get("setor") or "—")
                st.caption("**Situação cadastral**")
                st.text(row.get("situacao_cadastral") or "—")
            with c2:
                st.caption("**Telefone 2**")
                st.text(row.get("telefone2") or "—")
                st.caption("**CNPJ**")
                st.text(row.get("cnpj") or "—")
                st.caption("**Idade (anos)**")
                idade_val = row.get("idade")
                st.text(str(idade_val) if idade_val is not None else "—")
            with c3:
                st.caption("**Email**")
                st.text(row.get("email") or "—")
                st.caption("**Porte**")
                st.text(row.get("porte") or "—")
                st.caption("**Simples**")
                st.text(simples_label)
            with c4:
                st.caption("**Local**")
                st.text(row.get("regiao") or "—")
                st.caption("**Setor**")
                st.text((row.get("setor_catalogo") or "—")[:60] + ("…" if len((row.get("setor_catalogo") or "")) > 60 else ""))
                st.caption("**MEI**")
                st.text(row.get("mei") or "—")
            with c5:
                st.caption("**Site**")
                st.text(row.get("website") or "—")
                st.caption("**Atividade principal**")
                ativ = (row.get("atividade_principal") or "").strip()
                st.text(ativ[:60] + ("…" if len(ativ) > 60 else "—") if ativ else "—")
            if (row.get("descricao") or "").strip():
                st.caption(row.get("descricao"))
            sec = (row.get("atividades_secundarias") or "").strip()
            if sec:
                with st.expander("Atividades secundárias", expanded=False):
                    itens = [x.strip() for x in sec.split(";") if x.strip()]
                    if itens:
                        for item in itens:
                            st.caption(f"• {item}")
                    else:
                        st.caption(sec)

    err = st.session_state.pop("_last_db_err", None)
    if err:
        st.error(f"Erro ao gravar abordado: {err}")


def main() -> None:
    st.set_page_config(page_title="Leads", layout="wide")
    require_auth()

    st.title("Plataforma de leads")
    tab1, tab2 = st.tabs(["Alimentar Base (n8n)", "Leads"])

    with tab1:
        tab_disparar()
    with tab2:
        tab_leads()


if __name__ == "__main__":
    main()
