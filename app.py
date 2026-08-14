import os
import functools
import json
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Flask, request, redirect, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-isto-em-producao")

USERS = {
    os.environ.get("APP_USER_1", "ronaldo"): os.environ.get("APP_PASS_1", "changeme1"),
    os.environ.get("APP_USER_2", "andrea"): os.environ.get("APP_PASS_2", "changeme2"),
}

CATEGORIA_PT = {
    "Accomodation": "Hospedagem",
    "Airport and airlines": "Aeroporto e Companhias Aéreas",
    "Bookstore": "Livraria",
    "Cinema, theater and concerts": "Cinema, Teatro e Shows",
    "Clothing": "Vestuário",
    "Credit card fees": "Tarifas do Cartão",
    "Credit card payment": "Pagamento de Fatura",
    "Dentist": "Dentista",
    "Digital services": "Serviços Digitais",
    "Donations": "Doações",
    "Eating out": "Restaurantes",
    "Electronics": "Eletrônicos",
    "Gas stations": "Postos de Combustível",
    "Groceries": "Mercado",
    "Healthcare": "Saúde",
    "Hospital clinics and labs": "Hospitais e Laboratórios",
    "Houseware": "Utilidades Domésticas",
    "Insurance": "Seguros",
    "Interests charged": "Juros Cobrados",
    "Kids and toys": "Infantil e Brinquedos",
    "Leisure": "Lazer",
    "Office supplies": "Material de Escritório",
    "Online shopping": "Compras Online",
    "Parking": "Estacionamento",
    "Pharmacy": "Farmácia",
    "School": "Educação",
    "Services": "Serviços",
    "Shopping": "Compras",
    "Taxi and ride-hailing": "Táxi e Transporte por App",
    "Telecommunications": "Telecomunicações",
    "Tickets": "Ingressos",
    "Vehicle maintenance": "Manutenção Veicular",
    "Transfer - Internal": "Transferência Interna",
    "Tax on financial operations": "IOF",
    "Tolls and in vehicle payment": "Pedágio",
    "Agua / Gas": "Água / Gás",
    "Natacao": "Natação",
    "Academia": "Academia",
    "Viagem": "Viagem",
}

# categorias que não representam gasto real (usadas para excluir do resumo)
CATEGORIAS_NAO_GASTO = ("Credit card payment", "Interests charged", "Credit card fees", "Transfer - Internal")

# categorias extras disponiveis no dropdown mesmo que ainda nao tenham sido usadas em nenhuma transacao
CATEGORIAS_EXTRA = ("BRDrive", "Agua / Gas", "Natacao", "Academia", "Viagem")

# dia de fechamento da fatura (fixo, informado pelo usuario - Pluggy nao sincroniza esse dado)
FATURA_DIA_FECHAMENTO = 12


def proxima_ocorrencia_dia(dia):
    """Retorna a proxima data (a partir de hoje, inclusive) em que o mes tem esse dia."""
    hoje = datetime.now()
    import calendar
    ano, mes = hoje.year, hoje.month
    ultimo_dia_mes = calendar.monthrange(ano, mes)[1]
    dia_ajustado = min(dia, ultimo_dia_mes)
    candidata = hoje.replace(day=dia_ajustado, hour=0, minute=0, second=0, microsecond=0)
    if candidata.date() < hoje.date():
        mes2 = mes + 1
        ano2 = ano
        if mes2 > 12:
            mes2 = 1
            ano2 += 1
        ultimo_dia_mes2 = calendar.monthrange(ano2, mes2)[1]
        dia_ajustado2 = min(dia, ultimo_dia_mes2)
        candidata = candidata.replace(year=ano2, month=mes2, day=dia_ajustado2)
    return candidata


def cat_pt(categoria):
    if not categoria:
        return "-"
    return CATEGORIA_PT.get(categoria, categoria)


def get_conn():
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ["PGPASSWORD"],
    )


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect("/login")
        return view(*args, **kwargs)
    return wrapped


def migrate():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("ALTER TABLE cartao.transacao ADD COLUMN IF NOT EXISTS duplicada boolean DEFAULT false;")
        cur.execute("ALTER TABLE cartao.transacao ADD COLUMN IF NOT EXISTS regra_aplicada_id integer;")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.regra_classificacao ("
            "id serial PRIMARY KEY, padrao text NOT NULL, categoria text NOT NULL, ordem integer DEFAULT 0);"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.regra_dimensao_valor ("
            "regra_id integer NOT NULL REFERENCES cartao.regra_classificacao(id) ON DELETE CASCADE, "
            "dimensao_id integer NOT NULL, valor_id integer NOT NULL, "
            "PRIMARY KEY (regra_id, dimensao_id));"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.cartao_nome ("
            "final4 varchar(4) PRIMARY KEY, prefixo varchar(100) NOT NULL);"
        )
        cur.execute(
            "INSERT INTO cartao.cartao_nome (final4, prefixo) VALUES "
            "('9938', 'Andrea - digital'), "
            "('3200', 'Andrea - físico'), "
            "('6493', 'Ronaldo - físico'), "
            "('7638', 'Ronaldo - digital') "
            "ON CONFLICT (final4) DO NOTHING;"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.grupo_custo ("
            "id serial PRIMARY KEY, nome text UNIQUE NOT NULL, "
            "teto_mensal numeric, teto_anual numeric);"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.subgrupo_custo ("
            "id serial PRIMARY KEY, "
            "grupo_id integer NOT NULL REFERENCES cartao.grupo_custo(id) ON DELETE CASCADE, "
            "nome text NOT NULL, teto_mensal numeric, teto_anual numeric, "
            "UNIQUE(grupo_id, nome));"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.categoria_subgrupo ("
            "categoria text PRIMARY KEY, "
            "subgrupo_id integer REFERENCES cartao.subgrupo_custo(id) ON DELETE SET NULL);"
        )
        conn.commit()

        # seed inicial de grupos/subgrupos (so roda se a tabela grupo_custo estiver vazia)
        cur.execute("SELECT COUNT(*) FROM cartao.grupo_custo;")
        if cur.fetchone()[0] == 0:
            for grupo_nome, g_teto_mensal, g_teto_anual, subgrupos in SEED_GRUPOS:
                cur.execute(
                    "INSERT INTO cartao.grupo_custo (nome, teto_mensal, teto_anual) VALUES (%s,%s,%s) RETURNING id;",
                    (grupo_nome, g_teto_mensal, g_teto_anual),
                )
                grupo_id = cur.fetchone()[0]
                for sub_nome, s_teto_mensal, s_teto_anual, categorias in subgrupos:
                    cur.execute(
                        "INSERT INTO cartao.subgrupo_custo (grupo_id, nome, teto_mensal, teto_anual) "
                        "VALUES (%s,%s,%s,%s) RETURNING id;",
                        (grupo_id, sub_nome, s_teto_mensal, s_teto_anual),
                    )
                    subgrupo_id = cur.fetchone()[0]
                    for categoria in categorias:
                        cur.execute(
                            "INSERT INTO cartao.categoria_subgrupo (categoria, subgrupo_id) VALUES (%s,%s) "
                            "ON CONFLICT (categoria) DO UPDATE SET subgrupo_id = EXCLUDED.subgrupo_id;",
                            (categoria, subgrupo_id),
                        )
            conn.commit()

        # dimensoes adicionais (ex: Responsavel, Projeto/Evento) - independentes do Centro de Custo
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.dimensao ("
            "id serial PRIMARY KEY, nome text UNIQUE NOT NULL, "
            "obrigatoria boolean DEFAULT true, ordem integer DEFAULT 0);"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.dimensao_valor ("
            "id serial PRIMARY KEY, "
            "dimensao_id integer NOT NULL REFERENCES cartao.dimensao(id) ON DELETE CASCADE, "
            "nome text NOT NULL, UNIQUE(dimensao_id, nome));"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.transacao_dimensao ("
            "transacao_id text NOT NULL, "
            "dimensao_id integer NOT NULL REFERENCES cartao.dimensao(id) ON DELETE CASCADE, "
            "valor_id integer REFERENCES cartao.dimensao_valor(id) ON DELETE SET NULL, "
            "PRIMARY KEY (transacao_id, dimensao_id));"
        )
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM cartao.dimensao;")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO cartao.dimensao (nome, obrigatoria, ordem) VALUES ('Responsável', true, 1) RETURNING id;")
            resp_id = cur.fetchone()[0]
            for nome in ("Ronaldo", "Andrea", "Amanda", "Compartilhado"):
                cur.execute("INSERT INTO cartao.dimensao_valor (dimensao_id, nome) VALUES (%s,%s);", (resp_id, nome))

            cur.execute("INSERT INTO cartao.dimensao (nome, obrigatoria, ordem) VALUES ('Projeto / Evento', false, 2) RETURNING id;")
            proj_id = cur.fetchone()[0]
            for nome in ("Geral", "Viagem Chile 2027"):
                cur.execute("INSERT INTO cartao.dimensao_valor (dimensao_id, nome) VALUES (%s,%s);", (proj_id, nome))
            conn.commit()

        cur.close()
        conn.close()
    except Exception as e:
        print("Aviso: falha ao rodar migracao:", e)


def aplicar_regras(cur):
    """Aplica regras de classificacao automatica a lancamentos pendentes ainda nao tocados por nenhuma regra.
    So mexe em transacoes com conferida=false, nunca sobrescreve algo que o usuario ja confirmou."""
    try:
        cur.execute(
            "WITH match AS ("
            "  SELECT DISTINCT ON (t.transacao_id) t.transacao_id, r.id AS regra_id, r.categoria "
            "  FROM cartao.transacao t "
            "  JOIN cartao.regra_classificacao r ON t.descricao ILIKE '%%' || r.padrao || '%%' "
            "  WHERE t.regra_aplicada_id IS NULL AND t.conferida = false "
            "  ORDER BY t.transacao_id, r.ordem, r.id"
            ") "
            "UPDATE cartao.transacao t SET categoria = m.categoria, regra_aplicada_id = m.regra_id "
            "FROM match m WHERE t.transacao_id = m.transacao_id::uuid;"
        )
        cur.execute(
            "INSERT INTO cartao.transacao_dimensao (transacao_id, dimensao_id, valor_id) "
            "SELECT t.transacao_id::text, rdv.dimensao_id, rdv.valor_id "
            "FROM cartao.transacao t "
            "JOIN cartao.regra_dimensao_valor rdv ON rdv.regra_id = t.regra_aplicada_id "
            "WHERE t.regra_aplicada_id IS NOT NULL "
            "ON CONFLICT (transacao_id, dimensao_id) DO NOTHING;"
        )
    except Exception as e:
        print("Aviso: falha ao aplicar regras:", e)


DUPLICADA_OBS_PADRAO = "Duplicada - mesma compra ja lancada em outra linha (registro repetido pelo Pluggy)"

# estrutura inicial: (grupo, teto_mensal, teto_anual, [(subgrupo, teto_mensal, teto_anual, [categorias]), ...])
SEED_GRUPOS = [
    ("Moradia & Utilidades", None, None, [
        ("Casa", None, None, ["Houseware", "Agua / Gas", "Telecommunications"]),
    ]),
    ("Alimentação", None, None, [
        ("Mercado", None, None, ["Groceries"]),
        ("Restaurantes", None, None, ["Eating out"]),
    ]),
    ("Transporte", None, None, [
        ("Veículo & Deslocamento", None, None, [
            "Gas stations", "Vehicle maintenance", "Parking",
            "Tolls and in vehicle payment", "Taxi and ride-hailing",
        ]),
    ]),
    ("Saúde & Bem-estar", None, None, [
        ("Saúde", None, None, ["Healthcare", "Hospital clinics and labs", "Dentist", "Pharmacy", "Insurance"]),
        ("Atividades Físicas", None, None, ["Natacao", "Academia"]),
    ]),
    ("Lazer & Viagem", None, None, [
        ("Lazer", None, None, ["Leisure", "Cinema, theater and concerts"]),
        ("Viagem", None, 50000, ["Airport and airlines", "Accomodation", "Tickets", "Viagem"]),
    ]),
    ("Educação & Filhos", None, None, [
        ("Educação", None, None, ["School"]),
        ("Infantil", None, None, ["Kids and toys"]),
    ]),
    ("Compras & Pessoal", None, None, [
        ("Vestuário", None, None, ["Clothing"]),
        ("Compras Gerais", None, None, ["Shopping", "Online shopping", "Electronics", "Bookstore", "Office supplies"]),
    ]),
    ("Serviços & Diversos", None, None, [
        ("Serviços", None, None, ["Services", "Digital services"]),
        ("Doações", None, None, ["Donations"]),
        ("Taxas Financeiras", None, None, ["Tax on financial operations"]),
    ]),
    ("Negócios", None, None, [
        ("BRDrive", None, None, ["BRDrive"]),
    ]),
]

migrate()


BASE_CSS = """
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f5f6f8; margin:0; color:#1d1d1f; }
  .topbar { background:#1d2b3a; color:#fff; padding:14px 20px; display:flex; justify-content:space-between; align-items:center; }
  .topbar a { color:#fff; text-decoration:none; font-size:14px; }
  .wrap { max-width:1200px; margin:20px auto; padding:0 16px; }
  .filters { background:#fff; border-radius:8px; padding:14px 16px; margin-bottom:16px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; box-shadow:0 1px 2px rgba(0,0,0,0.06); }
  .filters label { font-size:13px; color:#555; margin-right:6px; }
  select, input[type=month] { padding:6px 8px; border:1px solid #ccc; border-radius:6px; font-size:14px; }
  table { width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 2px rgba(0,0,0,0.06); }
  th { text-align:left; background:#eef1f5; padding:10px; font-size:12px; text-transform:uppercase; color:#666; }
  td { padding:8px 10px; border-top:1px solid #eee; font-size:14px; vertical-align:middle; }
  tr.conferida { background:#f2fbf3; }
  tbody tr { cursor:pointer; }
  tbody tr:hover { background:#f5f8ff; }
  tr.conferida:hover { background:#e9f7eb; }
  tr.duplicada td { text-decoration:line-through; color:#aaa; }
  tr.duplicada { background:#fbf7f2; }
  tr.duplicada:hover { background:#f6ece0; }
  .valor { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .obs-input { width:100%; padding:5px 7px; border:1px solid #ddd; border-radius:5px; font-size:13px; }
  .cat-select { padding:5px; border-radius:5px; border:1px solid #ddd; font-size:13px; max-width:180px; }
  .status { font-size:11px; color:#2e8b3d; margin-left:6px; opacity:0; transition: opacity .3s; }
  .status.show { opacity:1; }
  .login-box { max-width:340px; margin:80px auto; background:#fff; padding:28px; border-radius:10px; box-shadow:0 2px 8px rgba(0,0,0,0.1); }
  .login-box input { width:100%; padding:9px; margin:6px 0; border:1px solid #ccc; border-radius:6px; }
  .login-box button, .filters button { background:#1d2b3a; color:#fff; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:14px; }
  .err { color:#b00020; font-size:13px; }
  .summary { font-size:13px; color:#555; margin-bottom:10px; }
  .cards { display:flex; gap:14px; margin-bottom:16px; flex-wrap:wrap; }
  .card { background:#fff; border-radius:8px; padding:14px 18px; box-shadow:0 1px 2px rgba(0,0,0,0.06); flex:1; min-width:140px; }
  .card .label { font-size:12px; color:#888; text-transform:uppercase; }
  .card .val { font-size:22px; font-weight:600; margin-top:4px; }
  .cat-breakdown { background:#fff; border-radius:8px; padding:14px 18px; margin-bottom:16px; box-shadow:0 1px 2px rgba(0,0,0,0.06); }
  .cat-breakdown h3 { margin:0 0 10px 0; font-size:14px; color:#444; }
  .cat-row { display:flex; justify-content:space-between; font-size:13px; padding:4px 0; border-bottom:1px solid #f2f2f2; }
  .ver-btn { background:none; border:1px solid #ccc; border-radius:5px; padding:4px 9px; font-size:12px; cursor:pointer; color:#333; }
  .ver-btn:hover { background:#f0f0f0; }
  .modal-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.45); align-items:center; justify-content:center; z-index:50; }
  .modal-bg.show { display:flex; }
  .modal { background:#fff; border-radius:10px; padding:24px; width:420px; max-width:92vw; box-shadow:0 8px 30px rgba(0,0,0,0.2); }
  .modal h3 { margin-top:0; }
  .modal .row { display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid #f2f2f2; font-size:14px; }
  .modal .row span:first-child { color:#888; }
  .modal .close { float:right; cursor:pointer; color:#888; font-size:20px; line-height:1; }
  .cartao-cell { max-width:150px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .nav-menu { display:flex; gap:18px; align-items:center; }
  .nav-menu > a { color:#fff; text-decoration:none; font-size:14px; }
  .nav-menu > a.ativo { text-decoration:underline; }
  .dropdown { position:relative; display:inline-block; }
  .dropbtn { color:#fff; font-size:14px; cursor:pointer; background:none; border:none; font-family:inherit; padding:0; display:flex; align-items:center; gap:4px; }
  .dropdown-content { display:none; position:absolute; right:0; top:22px; background:#26374a; min-width:210px; border-radius:8px; overflow:hidden; box-shadow:0 6px 18px rgba(0,0,0,0.3); z-index:100; }
  .dropdown-content a { display:block; padding:10px 14px; color:#fff; text-decoration:none; font-size:13px; }
  .dropdown-content a:hover { background:#33475b; }
  .dropdown-content a.ativo { background:#33475b; font-weight:600; }
  .dropdown:hover .dropdown-content, .dropdown:focus-within .dropdown-content { display:block; }
  .multisel { padding:6px; border:1px solid #ccc; border-radius:6px; min-width:170px; font-size:13px; }
  .rel-filtros { background:#fff; border-radius:12px; padding:16px 18px; margin-bottom:16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; box-shadow:0 1px 2px rgba(0,0,0,0.06); }
  .rel-filtros label { font-size:12px; color:#888; display:block; margin-bottom:3px; }
  .rel-grupo-row { display:flex; justify-content:space-between; align-items:center; padding:7px 0; border-bottom:1px solid #f2f2f2; font-size:13px; }
  .rel-grupo-row .barra { background:#eef1f5; border-radius:4px; height:6px; margin-top:3px; overflow:hidden; }
  .rel-grupo-row .barra div { background:#2e6fd6; height:100%; }
  .chipfilter { position:relative; display:inline-block; }
  .chip-btn { display:flex; align-items:center; gap:6px; background:#fff; border:1.5px solid #dde2e8; border-radius:20px; padding:8px 14px; font-size:13px; color:#444; cursor:pointer; font-family:inherit; white-space:nowrap; }
  .chip-btn:hover { border-color:#b7c0cc; }
  .chip-btn.ativo { border-color:#2e6fd6; color:#2e6fd6; background:#eef4ff; font-weight:600; }
  .chip-btn .chip-plus { font-size:15px; line-height:1; }
  .chip-btn .chip-clear { margin-left:2px; color:#999; font-weight:700; }
  .chip-btn .chip-clear:hover { color:#c0392b; }
  .chip-panel { display:none; position:absolute; top:calc(100% + 8px); left:0; background:#fff; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,0.18); width:260px; z-index:200; overflow:hidden; border:1px solid #eee; }
  .chip-panel.show { display:block; }
  .chip-search-wrap { padding:10px; border-bottom:1px solid #f0f0f0; }
  .chip-search { width:100%; padding:8px 10px; border:none; background:#f2f3f5; border-radius:8px; font-size:13px; outline:none; }
  .chip-list { max-height:260px; overflow-y:auto; padding:6px; }
  .chip-opt { display:flex; align-items:center; gap:9px; padding:8px 9px; border-radius:8px; font-size:13px; cursor:pointer; color:#333; }
  .chip-opt:hover, .chip-opt.chip-hover { background:#f2f5fa; }
  .chip-opt input { accent-color:#2e6fd6; width:15px; height:15px; }
  .chip-opt.chip-checked { background:#eef4ff; font-weight:600; }
  .rel-datewrap { display:flex; gap:8px; align-items:center; background:#fff; border:1.5px solid #dde2e8; border-radius:20px; padding:6px 12px; }
  .rel-datewrap input[type=date] { border:none; font-size:13px; padding:2px; outline:none; }
  .rel-actions { margin-left:auto; display:flex; gap:10px; align-items:center; }
  .chart-card { background:#fff; border-radius:12px; padding:18px; margin-bottom:16px; box-shadow:0 1px 2px rgba(0,0,0,0.06); }
  .chart-card h3 { margin:0 0 14px 0; font-size:14px; color:#444; }
</style>
"""


def topbar_html(titulo, ativo=None):
    def cls(nome):
        return "ativo" if ativo == nome else ""
    return f"""
      <div class="topbar">
        <div>{titulo} - {session.get('user')}</div>
        <div class="nav-menu">
          <a href="/" class="{cls('inicio')}">Lançamentos</a>
          <a href="/relatorios" class="{cls('relatorios')}">Relatórios</a>
          <div class="dropdown" tabindex="0">
            <button class="dropbtn">Configurações ▾</button>
            <div class="dropdown-content">
              <a href="/dre" class="{cls('dre')}">DRE / Centro de Custos</a>
              <a href="/grupos" class="{cls('grupos')}">Gerenciar grupos</a>
              <a href="/dimensoes" class="{cls('dimensoes')}">Gerenciar dimensões</a>
              <a href="/regras" class="{cls('regras')}">Regras automáticas</a>
              <a href="/cartoes" class="{cls('cartoes')}">Gerenciar cartões</a>
            </div>
          </div>
          <a href="/logout">Sair</a>
        </div>
      </div>
    """


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("usuario", "")
        p = request.form.get("senha", "")
        if u in USERS and USERS[u] == p:
            session["user"] = u
            return redirect("/")
        error = "Usuário ou senha inválidos."
    err_html = '<p class="err">' + error + '</p>' if error else ''
    return f"""
    <html><head><title>Login - Conferencia de Cartao</title>{BASE_CSS}</head>
    <body>
      <div class="login-box">
        <h2>Conferencia de Cartao</h2>
        <form method="post">
          <input name="usuario" placeholder="Usuario" autofocus>
          <input name="senha" type="password" placeholder="Senha">
          <button type="submit">Entrar</button>
        </form>
        {err_html}
      </div>
    </body></html>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def index():
    mes = request.args.get("mes") or datetime.now().strftime("%Y-%m")
    status = request.args.get("status", "todas")

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    aplicar_regras(cur)
    conn.commit()

    cur.execute("SELECT DISTINCT categoria FROM cartao.transacao WHERE categoria IS NOT NULL;")
    categorias_db = {r["categoria"] for r in cur.fetchall()}
    categorias = sorted(categorias_db | set(CATEGORIAS_EXTRA), key=lambda c: cat_pt(c).lower())

    where = ["to_char(data_transacao, 'YYYY-MM') = %s"]
    params = [mes]
    if status == "conferida":
        where.append("conferida = true")
    elif status == "pendente":
        where.append("conferida = false")

    cur.execute(
        "SELECT transacao_id, data_transacao, descricao, categoria, "
        "COALESCE(valor_brl, valor_original) AS valor, valor_original, moeda_original, "
        "status, tipo, numero_cartao_final, parcela_atual, parcela_total, "
        "conferida, observacao, conferida_por, conferida_em, COALESCE(duplicada, false) AS duplicada "
        "FROM cartao.transacao WHERE " + " AND ".join(where) + " ORDER BY data_transacao DESC;",
        params,
    )
    rows = cur.fetchall()

    # resumo do mes (nao filtrado por status, sempre do mes inteiro; duplicadas nao contam)
    cur.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN conferida THEN 1 ELSE 0 END) conferidas, "
        "SUM(CASE WHEN categoria NOT IN %s THEN COALESCE(valor_brl, valor_original) ELSE 0 END) AS gasto_real, "
        "SUM(COALESCE(valor_brl, valor_original)) AS total_bruto "
        "FROM cartao.transacao WHERE to_char(data_transacao,'YYYY-MM') = %s AND COALESCE(duplicada, false) = false;",
        (CATEGORIAS_NAO_GASTO, mes),
    )
    resumo = cur.fetchone()

    cur.execute(
        "SELECT categoria, SUM(COALESCE(valor_brl, valor_original)) AS total "
        "FROM cartao.transacao WHERE to_char(data_transacao,'YYYY-MM') = %s "
        "AND categoria NOT IN %s AND categoria IS NOT NULL AND COALESCE(duplicada, false) = false "
        "GROUP BY categoria ORDER BY total DESC LIMIT 8;",
        (mes, CATEGORIAS_NAO_GASTO),
    )
    por_categoria = cur.fetchall()

    cur.execute("SELECT vencimento_fatura FROM cartao.conta LIMIT 1;")
    conta_row = cur.fetchone()

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome;")
    nomes_cartao = {r["final4"]: r["prefixo"] for r in cur.fetchall()}

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()

    cur.execute("SELECT id, dimensao_id, nome FROM cartao.dimensao_valor ORDER BY nome;")
    valores_por_dim = {}
    for v in cur.fetchall():
        valores_por_dim.setdefault(v["dimensao_id"], []).append(v)

    mapa_dim_transacao = {}
    ids_visiveis = [r["transacao_id"] for r in rows]
    if ids_visiveis:
        cur.execute(
            "SELECT transacao_id, dimensao_id, valor_id FROM cartao.transacao_dimensao WHERE transacao_id IN %s;",
            (tuple(ids_visiveis),),
        )
        for m in cur.fetchall():
            mapa_dim_transacao[(str(m["transacao_id"]), m["dimensao_id"])] = m["valor_id"]

    cur.close()
    conn.close()

    def nome_cartao(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return f"{prefixo} - final {final4}" if prefixo else f"final {final4}"

    def nome_cartao_curto(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return prefixo if prefixo else f"final {final4}"

    dia_vencimento = conta_row["vencimento_fatura"].day if conta_row and conta_row["vencimento_fatura"] else None
    proximo_fechamento = proxima_ocorrencia_dia(FATURA_DIA_FECHAMENTO)
    proximo_vencimento = proxima_ocorrencia_dia(dia_vencimento) if dia_vencimento else None

    def cat_options(selected):
        return "".join(
            f'<option value="{c}" {"selected" if c == selected else ""}>{cat_pt(c)}</option>'
            for c in categorias
        )

    def dim_options(dimensao_id, selecionado):
        opts = ['<option value="">(nao definido)</option>']
        for v in valores_por_dim.get(dimensao_id, []):
            sel = "selected" if selecionado == v["id"] else ""
            opts.append(f'<option value="{v["id"]}" {sel}>{v["nome"]}</option>')
        return "".join(opts)

    trs = []
    detalhes_js = {}
    for r in rows:
        checked = "checked" if r["conferida"] else ""
        dup_checked = "checked" if r["duplicada"] else ""
        classes = " ".join(c for c in ["conferida" if r["conferida"] else "", "duplicada" if r["duplicada"] else ""] if c)
        data_local = r["data_transacao"] - timedelta(hours=3)
        data_fmt = data_local.strftime("%d/%m/%Y %H:%M")
        obs = (r["observacao"] or "").replace('"', "&quot;")
        rid = r["transacao_id"]

        dim_tds = []
        dim_detalhes = {}
        for d in dimensoes:
            valor_sel = mapa_dim_transacao.get((str(rid), d["id"]))
            faltando = d["obrigatoria"] and not valor_sel
            estilo = ' style="border-color:#c0392b;background:#fff5f5"' if faltando else ""
            dim_tds.append(
                f'<td><select class="dim-select" data-dim="{d["id"]}"{estilo} '
                f'onchange="salvar(\'{rid}\', this)">{dim_options(d["id"], valor_sel)}</select></td>'
            )
            nomes_valor = {v["id"]: v["nome"] for v in valores_por_dim.get(d["id"], [])}
            dim_detalhes[d["nome"]] = nomes_valor.get(valor_sel, "(nao definido)")

        trs.append(
            f'<tr class="{classes}" data-id="{rid}" onclick="linhaClick(event, \'{rid}\')">'
            f'<td>{data_fmt}</td>'
            f'<td>{r["descricao"]}</td>'
            f'<td class="cartao-cell" title="{nome_cartao(r["numero_cartao_final"])}">{nome_cartao_curto(r["numero_cartao_final"])}</td>'
            f'<td><select class="cat-select" onchange="salvar(\'{rid}\', this)">{cat_options(r["categoria"])}</select></td>'
            + "".join(dim_tds) +
            f'<td class="valor">R$ {r["valor"]:,.2f}</td>'
            f'<td><input class="obs-input" type="text" value="{obs}" placeholder="observacao..." onblur="salvar(\'{rid}\', this)"></td>'
            f'<td style="text-align:center"><input class="conf-check" type="checkbox" {checked} onchange="salvar(\'{rid}\', this)"></td>'
            f'<td style="text-align:center"><input class="dup-check" type="checkbox" {dup_checked} onchange="toggleDuplicada(\'{rid}\', this)"></td>'
            f'<td><span class="status" id="status-{rid}">salvo</span></td>'
            f'</tr>'
        )
        detalhes = {
            "data": data_fmt,
            "descricao": r["descricao"],
            "categoria": cat_pt(r["categoria"]),
            "valor": f'R$ {r["valor"]:,.2f}',
            "valor_original": f'{r["valor_original"]:,.2f} {r["moeda_original"] or ""}' if r["valor_original"] is not None else "-",
            "status": r["status"] or "-",
            "tipo": r["tipo"] or "-",
            "cartao": nome_cartao(r["numero_cartao_final"]),
            "parcela": f'{r["parcela_atual"]}/{r["parcela_total"]}' if r["parcela_total"] and r["parcela_total"] > 1 else "À vista",
            "conferida": "Sim" if r["conferida"] else "Não",
            "conferida_por": r["conferida_por"] or "-",
            "observacao": r["observacao"] or "-",
        }
        detalhes.update(dim_detalhes)
        detalhes_js[str(rid)] = detalhes

    total = resumo["total"] or 0
    conf = resumo["conferidas"] or 0
    gasto_real = resumo["gasto_real"] or 0
    colspan_total = 9 + len(dimensoes)
    body_rows = "".join(trs) if trs else f'<tr><td colspan="{colspan_total}" style="padding:20px;text-align:center;color:#888">Nenhuma transacao neste filtro.</td></tr>'
    dim_headers = "".join(f'<th>{d["nome"]}{" *" if d["obrigatoria"] else ""}</th>' for d in dimensoes)

    cat_rows_html = "".join(
        f'<div class="cat-row"><span>{cat_pt(c["categoria"])}</span><span>R$ {c["total"]:,.2f}</span></div>'
        for c in por_categoria
    ) or '<div class="cat-row"><span>Sem dados</span></div>'

    return f"""
    <html><head><title>Conferencia de Cartao</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Conferência de Cartão', 'inicio')}
      <div class="wrap">
        <div class="filters">
          <div>
            <label>Mes</label>
            <input type="month" id="mesInput" value="{mes}" onchange="irPara()">
          </div>
          <div>
            <label>Status</label>
            <select id="statusInput" onchange="irPara()">
              <option value="todas" {"selected" if status=="todas" else ""}>Todas</option>
              <option value="pendente" {"selected" if status=="pendente" else ""}>Pendentes</option>
              <option value="conferida" {"selected" if status=="conferida" else ""}>Conferidas</option>
            </select>
          </div>
        </div>

        <div class="cards">
          <div class="card"><div class="label">Gasto real do mes</div><div class="val">R$ {gasto_real:,.2f}</div></div>
          <div class="card"><div class="label">Transacoes</div><div class="val">{total}</div></div>
          <div class="card"><div class="label">Conferidas</div><div class="val">{conf} / {total}</div></div>
          <div class="card"><div class="label">Fechamento da fatura</div><div class="val">Dia {FATURA_DIA_FECHAMENTO}</div><div style="font-size:12px;color:#888;margin-top:4px">Proximo: {proximo_fechamento.strftime('%d/%m/%Y')}</div></div>
          <div class="card"><div class="label">Vencimento da fatura</div><div class="val">{'Dia ' + str(dia_vencimento) if dia_vencimento else '-'}</div><div style="font-size:12px;color:#888;margin-top:4px">{'Proximo: ' + proximo_vencimento.strftime('%d/%m/%Y') if proximo_vencimento else ''}</div></div>
        </div>

        <div class="cat-breakdown">
          <h3>Gasto por categoria (mes)</h3>
          {cat_rows_html}
        </div>

        <table>
          <thead><tr>
            <th>Data</th><th>Descricao</th><th>Cartao</th><th>Categoria</th>{dim_headers}<th>Valor</th><th>Observacao</th><th>Conferida</th><th>Duplicada</th><th></th>
          </tr></thead>
          <tbody>{body_rows}</tbody>
        </table>
      </div>

      <div class="modal-bg" id="modalBg" onclick="if(event.target===this) fecharModal()">
        <div class="modal">
          <span class="close" onclick="fecharModal()">&times;</span>
          <h3>Detalhes da transação</h3>
          <div id="modalBody"></div>
        </div>
      </div>

      <script>
        const detalhes = {json.dumps(detalhes_js)};
        function verDetalhes(id) {{
          const d = detalhes[id];
          if (!d) return;
          const labels = {{
            data: 'Data', descricao: 'Descrição', categoria: 'Categoria', valor: 'Valor (R$)',
            valor_original: 'Valor original', status: 'Status', tipo: 'Tipo', cartao: 'Cartão',
            parcela: 'Parcela', conferida: 'Conferida', conferida_por: 'Conferida por', observacao: 'Observação'
          }};
          let html = '';
          for (const k in labels) {{
            html += '<div class="row"><span>' + labels[k] + '</span><span>' + d[k] + '</span></div>';
          }}
          for (const k in d) {{
            if (!(k in labels)) {{
              html += '<div class="row"><span>' + k + '</span><span>' + d[k] + '</span></div>';
            }}
          }}
          document.getElementById('modalBody').innerHTML = html;
          document.getElementById('modalBg').classList.add('show');
        }}
        function fecharModal() {{
          document.getElementById('modalBg').classList.remove('show');
        }}
        function linhaClick(e, id) {{
          const tag = e.target.tagName;
          if (['SELECT','INPUT','OPTION','BUTTON'].includes(tag)) return;
          verDetalhes(id);
        }}
        function irPara() {{
          const mes = document.getElementById('mesInput').value;
          const status = document.getElementById('statusInput').value;
          window.location = '/?mes=' + mes + '&status=' + status;
        }}
        const DUPLICADA_OBS_PADRAO = {json.dumps(DUPLICADA_OBS_PADRAO)};
        const filaSalvar = {{}};
        function salvar(id, el) {{
          const tr = el.closest('tr');
          const dimensoes = {{}};
          tr.querySelectorAll('.dim-select').forEach(sel => {{
            dimensoes[sel.dataset.dim] = sel.value || null;
          }});
          const payload = {{
            conferida: tr.querySelector('.conf-check').checked,
            duplicada: tr.querySelector('.dup-check').checked,
            observacao: tr.querySelector('.obs-input').value,
            categoria: tr.querySelector('.cat-select').value,
            dimensoes: dimensoes
          }};
          const anterior = filaSalvar[id] || Promise.resolve();
          const atual = anterior.then(() => fetch('/api/transacao/' + id, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
          }})).then(r => r.json()).then(d => {{
            if (d.ok) {{
              const confFinal = payload.conferida && !d.bloqueada;
              tr.querySelector('.conf-check').checked = confFinal;
              tr.classList.toggle('conferida', confFinal);
              tr.classList.toggle('duplicada', payload.duplicada);
              tr.querySelectorAll('.dim-select').forEach(sel => {{
                sel.style.borderColor = '';
                sel.style.background = '';
              }});
              if (d.bloqueada) {{
                (d.faltando || []).forEach(dimId => {{
                  const sel = tr.querySelector('.dim-select[data-dim="' + dimId + '"]');
                  if (sel) {{ sel.style.borderColor = '#c0392b'; sel.style.background = '#fff5f5'; }}
                }});
                alert('Nao foi possivel confirmar: preencha os campos obrigatorios destacados em vermelho.');
              }}
              const s = document.getElementById('status-' + id);
              s.classList.add('show');
              setTimeout(() => s.classList.remove('show'), 1500);
            }}
          }});
          filaSalvar[id] = atual;
        }}
        function toggleDuplicada(id, checkbox) {{
          const tr = checkbox.closest('tr');
          const obsInput = tr.querySelector('.obs-input');
          if (checkbox.checked && !obsInput.value.trim()) {{
            obsInput.value = DUPLICADA_OBS_PADRAO;
          }}
          salvar(id, checkbox);
        }}
      </script>
    </body></html>
    """


@app.route("/api/transacao/<transacao_id>", methods=["POST"])
@login_required
def update_transacao(transacao_id):
    data = request.get_json(force=True)
    conn = get_conn()
    cur = conn.cursor()

    dimensoes_enviadas = data.get("dimensoes") or {}
    for dim_id_str, valor_id in dimensoes_enviadas.items():
        try:
            dim_id = int(dim_id_str)
        except (TypeError, ValueError):
            continue
        valor_id_int = int(valor_id) if valor_id not in (None, "") else None
        cur.execute(
            "INSERT INTO cartao.transacao_dimensao (transacao_id, dimensao_id, valor_id) VALUES (%s,%s,%s) "
            "ON CONFLICT (transacao_id, dimensao_id) DO UPDATE SET valor_id = EXCLUDED.valor_id;",
            (transacao_id, dim_id, valor_id_int),
        )

    # trava: nao permite confirmar (conferida=true) sem preencher as dimensoes obrigatorias
    cur.execute(
        "SELECT d.id FROM cartao.dimensao d "
        "LEFT JOIN cartao.transacao_dimensao td ON td.dimensao_id = d.id AND td.transacao_id = %s "
        "WHERE d.obrigatoria = true AND (td.valor_id IS NULL);",
        (transacao_id,),
    )
    faltando = [r[0] for r in cur.fetchall()]
    bloqueada = bool(faltando) and data.get("conferida", False)
    conferida_final = data.get("conferida", False) and not bloqueada

    cur.execute(
        "UPDATE cartao.transacao SET conferida = %s, duplicada = %s, observacao = %s, categoria = %s, "
        "conferida_por = CASE WHEN %s THEN %s ELSE conferida_por END, "
        "conferida_em = CASE WHEN %s THEN now() ELSE conferida_em END "
        "WHERE transacao_id = %s;",
        (
            conferida_final,
            data.get("duplicada", False),
            data.get("observacao"),
            data.get("categoria"),
            conferida_final,
            session.get("user"),
            conferida_final,
            transacao_id,
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "bloqueada": bloqueada, "faltando": faltando})


@app.route("/cartoes", methods=["GET", "POST"])
@login_required
def cartoes():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    erro = None
    if request.method == "POST":
        acao = request.form.get("acao")
        final4 = (request.form.get("final4") or "").strip()
        prefixo = (request.form.get("prefixo") or "").strip()
        final4_original = (request.form.get("final4_original") or "").strip()

        if acao == "excluir" and final4:
            cur.execute("DELETE FROM cartao.cartao_nome WHERE final4 = %s;", (final4,))
            conn.commit()

        elif acao == "salvar":
            if not final4.isdigit() or len(final4) != 4:
                erro = "Os 4 ultimos digitos devem ser exatamente 4 numeros."
            elif not prefixo:
                erro = "Informe o nome/prefixo do cartao."
            elif final4_original and final4_original != final4:
                # edicao trocando tambem o numero final do cartao
                cur.execute("SELECT 1 FROM cartao.cartao_nome WHERE final4 = %s;", (final4,))
                if cur.fetchone() and final4 != final4_original:
                    erro = f"Ja existe um cartao cadastrado com final {final4}."
                else:
                    cur.execute(
                        "UPDATE cartao.cartao_nome SET final4 = %s, prefixo = %s WHERE final4 = %s;",
                        (final4, prefixo, final4_original),
                    )
                    conn.commit()
            else:
                cur.execute(
                    "INSERT INTO cartao.cartao_nome (final4, prefixo) VALUES (%s, %s) "
                    "ON CONFLICT (final4) DO UPDATE SET prefixo = EXCLUDED.prefixo;",
                    (final4, prefixo),
                )
                conn.commit()

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome ORDER BY prefixo;")
    cartoes_cadastrados = cur.fetchall()
    cur.close()
    conn.close()

    editar_final4 = request.args.get("editar", "")
    editando = next((c for c in cartoes_cadastrados if c["final4"] == editar_final4), None)
    form_final4 = editando["final4"] if editando else ""
    form_prefixo = editando["prefixo"] if editando else ""
    titulo_form = "Editar cartao" if editando else "Novo cartao"

    linhas = "".join(
        f'<tr><td>{c["prefixo"]}</td><td>final {c["final4"]}</td>'
        f'<td style="white-space:nowrap">'
        f'<a href="/cartoes?editar={c["final4"]}" class="ver-btn" style="text-decoration:none;margin-right:6px">Editar</a>'
        f'<form method="post" style="display:inline" onsubmit="return confirm(\'Excluir este cartao?\')">'
        f'<input type="hidden" name="acao" value="excluir"><input type="hidden" name="final4" value="{c["final4"]}">'
        f'<button type="submit" class="ver-btn">Excluir</button></form></td></tr>'
        for c in cartoes_cadastrados
    ) or '<tr><td colspan="3" style="text-align:center;color:#888;padding:16px">Nenhum cartao cadastrado.</td></tr>'

    erro_html = f'<p class="err">{erro}</p>' if erro else ''
    cancelar_html = '<a href="/cartoes" style="margin-left:6px;font-size:13px">cancelar edicao</a>' if editando else ''

    return f"""
    <html><head><title>Gerenciar Cartoes</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Gerenciar Cartões', 'cartoes')}
      <div class="wrap">
        <div class="cat-breakdown">
          <h3>{titulo_form}{cancelar_html}</h3>
          <form method="post" style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
            <input type="hidden" name="acao" value="salvar">
            <input type="hidden" name="final4_original" value="{form_final4}">
            <div>
              <label style="font-size:13px;color:#555;display:block">Ultimos 4 digitos</label>
              <input name="final4" maxlength="4" placeholder="Ex: 9938" value="{form_final4}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px">
            </div>
            <div>
              <label style="font-size:13px;color:#555;display:block">Nome / prefixo (ex: Andrea - digital)</label>
              <input name="prefixo" placeholder="Ex: Andrea - digital" value="{form_prefixo}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:260px">
            </div>
            <button type="submit" style="background:#1d2b3a;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer">Salvar</button>
          </form>
          {erro_html}
        </div>

        <table>
          <thead><tr><th>Nome / prefixo</th><th>Final do cartao</th><th></th></tr></thead>
          <tbody>{linhas}</tbody>
        </table>
      </div>
    </body></html>
    """


@app.route("/dimensoes", methods=["GET", "POST"])
@login_required
def dimensoes_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    erro = None
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "criar_dimensao":
            nome = (request.form.get("nome") or "").strip()
            if not nome:
                erro = "Informe o nome da dimensao."
            else:
                try:
                    cur.execute(
                        "INSERT INTO cartao.dimensao (nome, obrigatoria, ordem) VALUES (%s,%s,%s);",
                        (nome, request.form.get("obrigatoria") == "on", 99),
                    )
                    conn.commit()
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()
                    erro = f"Ja existe uma dimensao chamada '{nome}'."
        elif acao == "editar_dimensao":
            cur.execute(
                "UPDATE cartao.dimensao SET nome=%s, obrigatoria=%s WHERE id=%s;",
                ((request.form.get("nome") or "").strip(), request.form.get("obrigatoria") == "on", request.form.get("dimensao_id")),
            )
            conn.commit()
        elif acao == "excluir_dimensao":
            cur.execute("DELETE FROM cartao.dimensao WHERE id=%s;", (request.form.get("dimensao_id"),))
            conn.commit()
        elif acao == "criar_valor":
            nome = (request.form.get("nome") or "").strip()
            if nome:
                try:
                    cur.execute(
                        "INSERT INTO cartao.dimensao_valor (dimensao_id, nome) VALUES (%s,%s);",
                        (request.form.get("dimensao_id"), nome),
                    )
                    conn.commit()
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()
                    erro = f"Ja existe o valor '{nome}' nessa dimensao."
        elif acao == "editar_valor":
            cur.execute(
                "UPDATE cartao.dimensao_valor SET nome=%s WHERE id=%s;",
                ((request.form.get("nome") or "").strip(), request.form.get("valor_id")),
            )
            conn.commit()
        elif acao == "excluir_valor":
            cur.execute("DELETE FROM cartao.dimensao_valor WHERE id=%s;", (request.form.get("valor_id"),))
            conn.commit()

    cur.execute("SELECT id, nome, obrigatoria, ordem FROM cartao.dimensao ORDER BY ordem, nome;")
    dims = cur.fetchall()
    cur.execute("SELECT id, dimensao_id, nome FROM cartao.dimensao_valor ORDER BY nome;")
    valores_db = cur.fetchall()
    cur.close()
    conn.close()

    valores_por_dim = {}
    for v in valores_db:
        valores_por_dim.setdefault(v["dimensao_id"], []).append(v)

    blocos = []
    for d in dims:
        valores = valores_por_dim.get(d["id"], [])
        valores_rows = "".join(
            f'<tr><td style="padding-left:24px">'
            f'<form method="post" style="display:flex;gap:8px;align-items:center">'
            f'<input type="hidden" name="acao" value="editar_valor"><input type="hidden" name="valor_id" value="{v["id"]}">'
            f'<input name="nome" value="{v["nome"]}" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:220px">'
            f'<button type="submit" class="ver-btn">Salvar</button>'
            f'</form></td>'
            f'<td><form method="post" onsubmit="return confirm(\'Excluir este valor?\')">'
            f'<input type="hidden" name="acao" value="excluir_valor"><input type="hidden" name="valor_id" value="{v["id"]}">'
            f'<button type="submit" class="ver-btn">Excluir</button></form></td></tr>'
            for v in valores
        ) or '<tr><td colspan="2" style="padding-left:24px;color:#888;font-size:13px">Nenhum valor cadastrado ainda.</td></tr>'

        obrig_checked = "checked" if d["obrigatoria"] else ""
        blocos.append(f"""
        <details class="cat-breakdown" open style="padding:0">
          <summary style="cursor:pointer;padding:14px 18px;font-weight:600;font-size:14px">
            {d["nome"]} {"<span style='color:#c0392b;font-size:12px'>(obrigatorio)</span>" if d["obrigatoria"] else "<span style='color:#888;font-size:12px'>(opcional)</span>"}
          </summary>
          <div style="padding:0 18px 18px 18px">
            <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
              <input type="hidden" name="acao" value="editar_dimensao"><input type="hidden" name="dimensao_id" value="{d["id"]}">
              <input name="nome" value="{d["nome"]}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:220px">
              <label style="font-size:13px;color:#555"><input type="checkbox" name="obrigatoria" {obrig_checked}> obrigatorio para confirmar</label>
              <button type="submit" class="ver-btn">Salvar</button>
            </form>
            <form method="post" style="display:inline" onsubmit="return confirm('Excluir esta dimensao e todos os seus valores?')">
              <input type="hidden" name="acao" value="excluir_dimensao"><input type="hidden" name="dimensao_id" value="{d["id"]}">
              <button type="submit" class="ver-btn">Excluir dimensao</button>
            </form>
            <table style="margin-top:12px"><tbody>{valores_rows}</tbody></table>
            <form method="post" style="display:flex;gap:8px;align-items:center;margin-top:8px;padding-left:24px">
              <input type="hidden" name="acao" value="criar_valor"><input type="hidden" name="dimensao_id" value="{d["id"]}">
              <input name="nome" placeholder="Novo valor (ex: Amanda, Viagem Chile 2027)" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:260px">
              <button type="submit" class="ver-btn">+ Adicionar valor</button>
            </form>
          </div>
        </details>
        """)

    erro_html = f'<p class="err">{erro}</p>' if erro else ''

    return f"""
    <html><head><title>Gerenciar Dimensoes</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Gerenciar Dimensões', 'dimensoes')}
      <div class="wrap">
        <div style="font-size:13px;color:#666;margin-bottom:16px">
          Dimensoes sao classificacoes independentes do Centro de Custo, aplicadas a cada lancamento
          (ex: <strong>Responsavel</strong> - quem gastou, <strong>Projeto/Evento</strong> - a qual viagem ou evento pertence).
          Dimensoes marcadas como obrigatorias impedem confirmar (marcar como conferida) um lancamento sem esse vinculo preenchido.
        </div>
        <div class="cat-breakdown">
          <h3>Nova dimensao</h3>
          <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="hidden" name="acao" value="criar_dimensao">
            <input name="nome" placeholder="Ex: Cliente, Metodo de pagamento..." style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:260px">
            <label style="font-size:13px;color:#555"><input type="checkbox" name="obrigatoria" checked> obrigatorio para confirmar</label>
            <button type="submit" style="background:#1d2b3a;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer">Criar dimensao</button>
          </form>
          {erro_html}
        </div>
        {"".join(blocos)}
      </div>
    </body></html>
    """


@app.route("/regras", methods=["GET", "POST"])
@login_required
def regras_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    erro = None
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "criar_regra":
            padrao = (request.form.get("padrao") or "").strip()
            categoria = request.form.get("categoria") or ""
            if not padrao:
                erro = "Informe o texto/padrao a procurar na descricao."
            else:
                cur.execute(
                    "INSERT INTO cartao.regra_classificacao (padrao, categoria) VALUES (%s,%s) RETURNING id;",
                    (padrao, categoria),
                )
                regra_id = cur.fetchone()["id"]
                for chave, valor in request.form.items():
                    if chave.startswith("dim_") and valor:
                        dim_id = chave.replace("dim_", "")
                        cur.execute(
                            "INSERT INTO cartao.regra_dimensao_valor (regra_id, dimensao_id, valor_id) VALUES (%s,%s,%s);",
                            (regra_id, dim_id, valor),
                        )
                conn.commit()
        elif acao == "excluir_regra":
            cur.execute("DELETE FROM cartao.regra_classificacao WHERE id=%s;", (request.form.get("regra_id"),))
            conn.commit()
        elif acao == "reaplicar_regra":
            # libera as transacoes pendentes que essa regra ja tinha marcado, para reclassificar no proximo acesso
            cur.execute(
                "UPDATE cartao.transacao SET regra_aplicada_id = NULL "
                "WHERE regra_aplicada_id = %s AND conferida = false;",
                (request.form.get("regra_id"),),
            )
            conn.commit()
        elif acao == "editar_regra":
            regra_id = request.form.get("regra_id")
            padrao = (request.form.get("padrao") or "").strip()
            categoria = request.form.get("categoria")
            if not padrao:
                erro = "Informe o texto a ser procurado na descricao."
            else:
                cur.execute(
                    "UPDATE cartao.regra_classificacao SET padrao = %s, categoria = %s WHERE id = %s;",
                    (padrao, categoria, regra_id),
                )
                cur.execute("DELETE FROM cartao.regra_dimensao_valor WHERE regra_id = %s;", (regra_id,))
                for chave, valor in request.form.items():
                    if chave.startswith("dim_") and valor:
                        dim_id = chave.split("_", 1)[1]
                        cur.execute(
                            "INSERT INTO cartao.regra_dimensao_valor (regra_id, dimensao_id, valor_id) VALUES (%s, %s, %s);",
                            (regra_id, dim_id, valor),
                        )
                # libera pendentes ja tocados por essa regra para reclassificar com os novos valores
                cur.execute(
                    "UPDATE cartao.transacao SET regra_aplicada_id = NULL "
                    "WHERE regra_aplicada_id = %s AND conferida = false;",
                    (regra_id,),
                )
                conn.commit()

    aplicar_regras(cur)
    conn.commit()

    cur.execute("SELECT id, padrao, categoria, ordem FROM cartao.regra_classificacao ORDER BY ordem, id;")
    regras_db = cur.fetchall()
    cur.execute("SELECT regra_id, dimensao_id, valor_id FROM cartao.regra_dimensao_valor;")
    dim_por_regra = {}
    for r in cur.fetchall():
        dim_por_regra.setdefault(r["regra_id"], {})[r["dimensao_id"]] = r["valor_id"]

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cur.execute("SELECT id, dimensao_id, nome FROM cartao.dimensao_valor ORDER BY nome;")
    valores_por_dim = {}
    for v in cur.fetchall():
        valores_por_dim.setdefault(v["dimensao_id"], []).append(v)

    cur.execute("SELECT COUNT(*) AS n FROM cartao.transacao WHERE regra_aplicada_id IS NOT NULL;")
    total_aplicadas = cur.fetchone()["n"]

    todas_categorias = sorted(
        (set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA)) - set(CATEGORIAS_NAO_GASTO),
        key=lambda c: cat_pt(c).lower(),
    )

    cur.close()
    conn.close()

    def cat_options_regra(selecionado=None):
        return "".join(
            f'<option value="{c}" {"selected" if c == selecionado else ""}>{cat_pt(c)}</option>'
            for c in todas_categorias
        )

    def dim_options_regra(dimensao_id, selecionado=None):
        opts = ['<option value="">(nao definir)</option>']
        for v in valores_por_dim.get(dimensao_id, []):
            sel = "selected" if selecionado == v["id"] else ""
            opts.append(f'<option value="{v["id"]}" {sel}>{v["nome"]}</option>')
        return "".join(opts)

    dim_cols_novo = "".join(
        f'<div><label style="font-size:12px;color:#888;display:block">{d["nome"]}</label>'
        f'<select name="dim_{d["id"]}" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px">{dim_options_regra(d["id"])}</select></div>'
        for d in dimensoes
    )

    editar_id = request.args.get("editar")
    try:
        editar_id = int(editar_id) if editar_id else None
    except ValueError:
        editar_id = None

    linhas = []
    for r in regras_db:
        dims_txt = []
        for d in dimensoes:
            vid = dim_por_regra.get(r["id"], {}).get(d["id"])
            if vid:
                nome_valor = next((v["nome"] for v in valores_por_dim.get(d["id"], []) if v["id"] == vid), "?")
                dims_txt.append(f'{d["nome"]}: {nome_valor}')
        dims_html = ", ".join(dims_txt) or "-"

        if editar_id == r["id"]:
            dim_cols_edit = "".join(
                f'<div><label style="font-size:12px;color:#888;display:block">{d["nome"]}</label>'
                f'<select name="dim_{d["id"]}" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px">'
                f'{dim_options_regra(d["id"], dim_por_regra.get(r["id"], {}).get(d["id"]))}</select></div>'
                for d in dimensoes
            )
            linhas.append(
                f'<tr><td colspan="4">'
                f'<form method="post" style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;padding:8px 0">'
                f'<input type="hidden" name="acao" value="editar_regra"><input type="hidden" name="regra_id" value="{r["id"]}">'
                f'<div><label style="font-size:12px;color:#888;display:block">Texto na descricao</label>'
                f'<input name="padrao" value="{r["padrao"]}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:220px"></div>'
                f'<div><label style="font-size:12px;color:#888;display:block">Categoria</label>'
                f'<select name="categoria" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px">{cat_options_regra(r["categoria"])}</select></div>'
                f'{dim_cols_edit}'
                f'<button type="submit" style="background:#1d2b3a;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer">Salvar</button>'
                f'<a href="/regras" class="ver-btn" style="text-decoration:none">Cancelar</a>'
                f'</form></td></tr>'
            )
        else:
            linhas.append(
                f'<tr><td><strong>"{r["padrao"]}"</strong></td><td>{cat_pt(r["categoria"])}</td><td>{dims_html}</td>'
                f'<td style="white-space:nowrap">'
                f'<a href="/regras?editar={r["id"]}" class="ver-btn" style="text-decoration:none">Editar</a> '
                f'<form method="post" style="display:inline" onsubmit="return confirm(\'Reaplicar essa regra aos lancamentos pendentes?\')">'
                f'<input type="hidden" name="acao" value="reaplicar_regra"><input type="hidden" name="regra_id" value="{r["id"]}">'
                f'<button type="submit" class="ver-btn">Reaplicar</button></form> '
                f'<form method="post" style="display:inline" onsubmit="return confirm(\'Excluir esta regra?\')">'
                f'<input type="hidden" name="acao" value="excluir_regra"><input type="hidden" name="regra_id" value="{r["id"]}">'
                f'<button type="submit" class="ver-btn">Excluir</button></form>'
                f'</td></tr>'
            )
    linhas_html = "".join(linhas) or '<tr><td colspan="4" style="text-align:center;color:#888;padding:16px">Nenhuma regra cadastrada ainda.</td></tr>'

    erro_html = f'<p class="err">{erro}</p>' if erro else ''

    return f"""
    <html><head><title>Regras Automaticas</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Regras Automáticas', 'regras')}
      <div class="wrap">
        <div style="font-size:13px;color:#666;margin-bottom:16px">
          Quando a descricao de um lancamento <strong>pendente</strong> (nao conferido) contiver o texto cadastrado aqui,
          o app preenche sozinho a categoria e as dimensoes escolhidas, na proxima vez que voce abrir a tela principal.
          Nunca mexe em lancamentos que voce ja marcou como conferidos.
          Total de lancamentos ja classificados por regras: <strong>{total_aplicadas}</strong>.
        </div>
        <div class="cat-breakdown">
          <h3>Nova regra</h3>
          <form method="post" style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
            <input type="hidden" name="acao" value="criar_regra">
            <div>
              <label style="font-size:12px;color:#888;display:block">Texto na descricao</label>
              <input name="padrao" placeholder='Ex: BOCO GAS' style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:220px">
            </div>
            <div>
              <label style="font-size:12px;color:#888;display:block">Categoria</label>
              <select name="categoria" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px">{cat_options_regra()}</select>
            </div>
            {dim_cols_novo}
            <button type="submit" style="background:#1d2b3a;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer">Criar regra</button>
          </form>
          {erro_html}
        </div>

        <table>
          <thead><tr><th>Texto procurado</th><th>Categoria</th><th>Dimensoes</th><th></th></tr></thead>
          <tbody>{linhas_html}</tbody>
        </table>
      </div>
    </body></html>
    """


def _fmt_moeda(v):
    return f"R$ {v:,.2f}"


def _barra_html(realizado, teto):
    if not teto or teto <= 0:
        return ""
    pct = min(realizado / teto * 100, 999)
    cor = "#2e8b3d" if pct < 70 else ("#d68a00" if pct < 100 else "#c0392b")
    largura = min(pct, 100)
    return (
        f'<div style="background:#eee;border-radius:4px;height:8px;margin-top:4px;overflow:hidden">'
        f'<div style="background:{cor};width:{largura:.0f}%;height:100%"></div></div>'
        f'<div style="font-size:11px;color:{cor};margin-top:2px">{pct:.0f}% do teto</div>'
    )


@app.route("/dre")
@login_required
def dre():
    ano = request.args.get("ano") or str(datetime.now().year)
    hoje = datetime.now()
    ano_atual = str(hoje.year)
    eh_ano_atual = ano == ano_atual
    dia_do_ano = hoje.timetuple().tm_yday if eh_ano_atual else 365
    mes_atual_str = hoje.strftime("%Y-%m")

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT categoria, SUM(COALESCE(valor_brl, valor_original)) AS total "
        "FROM cartao.transacao WHERE to_char(data_transacao,'YYYY') = %s "
        "AND COALESCE(duplicada, false) = false AND categoria NOT IN %s AND categoria IS NOT NULL "
        "GROUP BY categoria;",
        (ano, CATEGORIAS_NAO_GASTO),
    )
    anual_por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}

    mensal_por_cat = {}
    if eh_ano_atual:
        cur.execute(
            "SELECT categoria, SUM(COALESCE(valor_brl, valor_original)) AS total "
            "FROM cartao.transacao WHERE to_char(data_transacao,'YYYY-MM') = %s "
            "AND COALESCE(duplicada, false) = false AND categoria NOT IN %s AND categoria IS NOT NULL "
            "GROUP BY categoria;",
            (mes_atual_str, CATEGORIAS_NAO_GASTO),
        )
        mensal_por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}

    cur.execute(
        "SELECT g.id AS grupo_id, g.nome AS grupo_nome, g.teto_mensal AS g_teto_mensal, g.teto_anual AS g_teto_anual, "
        "s.id AS subgrupo_id, s.nome AS subgrupo_nome, s.teto_mensal AS s_teto_mensal, s.teto_anual AS s_teto_anual, "
        "cs.categoria "
        "FROM cartao.grupo_custo g "
        "JOIN cartao.subgrupo_custo s ON s.grupo_id = g.id "
        "LEFT JOIN cartao.categoria_subgrupo cs ON cs.subgrupo_id = s.id "
        "ORDER BY g.nome, s.nome;"
    )
    linhas_map = cur.fetchall()

    cur.execute("SELECT id, nome FROM cartao.dimensao ORDER BY ordem, nome;")
    dims = cur.fetchall()
    por_dimensao = []
    for d in dims:
        cur.execute(
            "SELECT COALESCE(dv.nome, '(nao definido)') AS nome, "
            "SUM(COALESCE(t.valor_brl, t.valor_original)) AS total "
            "FROM cartao.transacao t "
            "LEFT JOIN cartao.transacao_dimensao td ON td.transacao_id = t.transacao_id::text AND td.dimensao_id = %s "
            "LEFT JOIN cartao.dimensao_valor dv ON dv.id = td.valor_id "
            "WHERE to_char(t.data_transacao,'YYYY') = %s AND COALESCE(t.duplicada, false) = false "
            "AND t.categoria NOT IN %s AND t.categoria IS NOT NULL "
            "GROUP BY dv.nome ORDER BY total DESC;",
            (d["id"], ano, CATEGORIAS_NAO_GASTO),
        )
        por_dimensao.append({"nome": d["nome"], "linhas": cur.fetchall()})

    cur.close()
    conn.close()

    grupos = {}
    categorias_mapeadas = set()
    for r in linhas_map:
        g = grupos.setdefault(r["grupo_id"], {
            "nome": r["grupo_nome"], "teto_mensal": r["g_teto_mensal"], "teto_anual": r["g_teto_anual"],
            "subgrupos": {},
        })
        s = g["subgrupos"].setdefault(r["subgrupo_id"], {
            "nome": r["subgrupo_nome"], "teto_mensal": r["s_teto_mensal"], "teto_anual": r["s_teto_anual"],
            "categorias": [],
        })
        if r["categoria"]:
            s["categorias"].append(r["categoria"])
            categorias_mapeadas.add(r["categoria"])

    nao_classificadas = sorted(set(anual_por_cat) - categorias_mapeadas)

    blocos = []
    total_geral_anual = 0.0
    for g in sorted(grupos.values(), key=lambda x: x["nome"]):
        g_anual = 0.0
        g_mensal = 0.0
        subs_html = []
        for s in sorted(g["subgrupos"].values(), key=lambda x: x["nome"]):
            s_anual = sum(anual_por_cat.get(c, 0.0) for c in s["categorias"])
            s_mensal = sum(mensal_por_cat.get(c, 0.0) for c in s["categorias"])
            g_anual += s_anual
            g_mensal += s_mensal
            projecao = (s_anual / dia_do_ano * 365) if eh_ano_atual and dia_do_ano else s_anual
            alerta = ""
            if s["teto_anual"] and projecao > float(s["teto_anual"]):
                alerta = f'<div style="font-size:11px;color:#c0392b;margin-top:2px">⚠ projecao ({_fmt_moeda(projecao)}) estoura o teto anual</div>'
            teto_anual_html = ""
            if s["teto_anual"]:
                teto_anual_html = (
                    f'<div style="font-size:12px;color:#888">Teto anual: {_fmt_moeda(float(s["teto_anual"]))}</div>'
                    f'{_barra_html(s_anual, float(s["teto_anual"]))}'
                )
            teto_mensal_html = ""
            if s["teto_mensal"] and eh_ano_atual:
                teto_mensal_html = (
                    f'<div style="font-size:12px;color:#888;margin-top:6px">Teto mensal: {_fmt_moeda(float(s["teto_mensal"]))} '
                    f'(realizado no mes: {_fmt_moeda(s_mensal)})</div>'
                    f'{_barra_html(s_mensal, float(s["teto_mensal"]))}'
                )
            cats_pt = ", ".join(cat_pt(c) for c in s["categorias"]) or "sem categorias vinculadas"
            subs_html.append(
                f'<div style="padding:10px 0;border-top:1px solid #f2f2f2">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
                f'<strong style="font-size:14px">{s["nome"]}</strong>'
                f'<span style="font-size:14px">{_fmt_moeda(s_anual)} no ano</span>'
                f'</div>'
                f'<div style="font-size:11px;color:#aaa;margin-top:2px">{cats_pt}</div>'
                f'{teto_anual_html}{teto_mensal_html}{alerta}'
                f'</div>'
            )
        total_geral_anual += g_anual
        teto_grupo_html = f'{_barra_html(g_anual, float(g["teto_anual"]))}' if g["teto_anual"] else ""
        blocos.append(
            f'<div class="cat-breakdown">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
            f'<h3 style="margin:0">{g["nome"]}</h3>'
            f'<span style="font-size:18px;font-weight:600">{_fmt_moeda(g_anual)}</span>'
            f'</div>{teto_grupo_html}'
            f'{"".join(subs_html)}'
            f'</div>'
        )

    if nao_classificadas:
        linhas_nc = "".join(
            f'<div class="cat-row"><span>{cat_pt(c)}</span><span>{_fmt_moeda(anual_por_cat[c])}</span></div>'
            for c in nao_classificadas
        )
        blocos.append(
            f'<div class="cat-breakdown">'
            f'<h3>Nao classificadas</h3>'
            f'<div style="font-size:12px;color:#888;margin-bottom:8px">Categorias sem grupo/subgrupo definido em <a href="/grupos">Gerenciar grupos</a>.</div>'
            f'{linhas_nc}</div>'
        )

    blocos_dimensao = []
    for pd in por_dimensao:
        linhas_html = "".join(
            f'<div class="cat-row"><span>{l["nome"]}</span><span>{_fmt_moeda(float(l["total"] or 0))}</span></div>'
            for l in pd["linhas"]
        ) or '<div class="cat-row"><span>Sem dados</span></div>'
        blocos_dimensao.append(
            f'<div class="cat-breakdown"><h3>Por {pd["nome"]}</h3>{linhas_html}</div>'
        )

    anos_opcoes = "".join(
        f'<option value="{a}" {"selected" if str(a)==ano else ""}>{a}</option>'
        for a in range(hoje.year - 3, hoje.year + 1)
    )

    return f"""
    <html><head><title>DRE / Centro de Custos</title>{BASE_CSS}</head>
    <body>
      {topbar_html('DRE / Centro de Custos', 'dre')}
      <div class="wrap">
        <div class="filters">
          <div>
            <label>Ano</label>
            <select onchange="window.location='/dre?ano='+this.value">{anos_opcoes}</select>
          </div>
          <div style="margin-left:auto;font-size:14px"><strong>Total do ano: {_fmt_moeda(total_geral_anual)}</strong></div>
        </div>
        {"".join(blocos_dimensao)}
        {"".join(blocos)}
      </div>
    </body></html>
    """


@app.route("/grupos", methods=["GET", "POST"])
@login_required
def grupos_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def to_num(v):
        v = (v or "").strip().replace(",", ".")
        return float(v) if v else None

    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "criar_grupo":
            cur.execute(
                "INSERT INTO cartao.grupo_custo (nome, teto_mensal, teto_anual) VALUES (%s,%s,%s);",
                (request.form.get("nome", "").strip(), to_num(request.form.get("teto_mensal")), to_num(request.form.get("teto_anual"))),
            )
        elif acao == "editar_grupo":
            cur.execute(
                "UPDATE cartao.grupo_custo SET nome=%s, teto_mensal=%s, teto_anual=%s WHERE id=%s;",
                (request.form.get("nome", "").strip(), to_num(request.form.get("teto_mensal")), to_num(request.form.get("teto_anual")), request.form.get("grupo_id")),
            )
        elif acao == "excluir_grupo":
            cur.execute("DELETE FROM cartao.grupo_custo WHERE id=%s;", (request.form.get("grupo_id"),))
        elif acao == "criar_subgrupo":
            cur.execute(
                "INSERT INTO cartao.subgrupo_custo (grupo_id, nome, teto_mensal, teto_anual) VALUES (%s,%s,%s,%s);",
                (request.form.get("grupo_id"), request.form.get("nome", "").strip(), to_num(request.form.get("teto_mensal")), to_num(request.form.get("teto_anual"))),
            )
        elif acao == "editar_subgrupo":
            cur.execute(
                "UPDATE cartao.subgrupo_custo SET nome=%s, teto_mensal=%s, teto_anual=%s WHERE id=%s;",
                (request.form.get("nome", "").strip(), to_num(request.form.get("teto_mensal")), to_num(request.form.get("teto_anual")), request.form.get("subgrupo_id")),
            )
        elif acao == "excluir_subgrupo":
            cur.execute("DELETE FROM cartao.subgrupo_custo WHERE id=%s;", (request.form.get("subgrupo_id"),))
        elif acao == "mapear_categoria":
            subgrupo_id = request.form.get("subgrupo_id") or None
            cur.execute(
                "INSERT INTO cartao.categoria_subgrupo (categoria, subgrupo_id) VALUES (%s,%s) "
                "ON CONFLICT (categoria) DO UPDATE SET subgrupo_id = EXCLUDED.subgrupo_id;",
                (request.form.get("categoria"), subgrupo_id),
            )
        conn.commit()

    cur.execute("SELECT id, nome, teto_mensal, teto_anual FROM cartao.grupo_custo ORDER BY nome;")
    grupos_db = cur.fetchall()
    cur.execute("SELECT id, grupo_id, nome, teto_mensal, teto_anual FROM cartao.subgrupo_custo ORDER BY nome;")
    subgrupos_db = cur.fetchall()
    cur.execute("SELECT categoria, subgrupo_id FROM cartao.categoria_subgrupo;")
    mapa_categoria = {r["categoria"]: r["subgrupo_id"] for r in cur.fetchall()}
    cur.close()
    conn.close()

    subgrupos_por_grupo = {}
    for s in subgrupos_db:
        subgrupos_por_grupo.setdefault(s["grupo_id"], []).append(s)

    def input_num(nome, valor):
        v = "" if valor is None else f"{float(valor):g}"
        return f'<input name="{nome}" value="{v}" placeholder="opcional" style="width:110px;padding:6px 8px;border:1px solid #ccc;border-radius:6px">'

    grupos_html = []
    for g in grupos_db:
        subs = subgrupos_por_grupo.get(g["id"], [])
        subs_rows = "".join(
            f'<tr>'
            f'<td style="padding-left:24px">'
            f'<form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
            f'<input type="hidden" name="acao" value="editar_subgrupo"><input type="hidden" name="subgrupo_id" value="{s["id"]}">'
            f'<input name="nome" value="{s["nome"]}" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:200px">'
            f'{input_num("teto_mensal", s["teto_mensal"])}{input_num("teto_anual", s["teto_anual"])}'
            f'<button type="submit" class="ver-btn">Salvar</button>'
            f'</form></td>'
            f'<td><form method="post" onsubmit="return confirm(\'Excluir subgrupo?\')">'
            f'<input type="hidden" name="acao" value="excluir_subgrupo"><input type="hidden" name="subgrupo_id" value="{s["id"]}">'
            f'<button type="submit" class="ver-btn">Excluir</button></form></td>'
            f'</tr>'
            for s in subs
        )
        grupos_html.append(f"""
        <details class="cat-breakdown" style="padding:0">
          <summary style="cursor:pointer;padding:14px 18px;font-weight:600;font-size:14px">
            {g["nome"]} <span style="color:#888;font-weight:400;font-size:12px">({len(subs)} subgrupo{'s' if len(subs)!=1 else ''})</span>
          </summary>
          <div style="padding:0 18px 18px 18px">
            <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
              <input type="hidden" name="acao" value="editar_grupo"><input type="hidden" name="grupo_id" value="{g["id"]}">
              <input name="nome" value="{g["nome"]}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;font-weight:600;width:220px">
              <span style="font-size:12px;color:#888">teto mensal</span>{input_num("teto_mensal", g["teto_mensal"])}
              <span style="font-size:12px;color:#888">teto anual</span>{input_num("teto_anual", g["teto_anual"])}
              <button type="submit" class="ver-btn">Salvar grupo</button>
            </form>
            <form method="post" style="display:inline" onsubmit="return confirm('Excluir grupo e seus subgrupos?')">
              <input type="hidden" name="acao" value="excluir_grupo"><input type="hidden" name="grupo_id" value="{g["id"]}">
              <button type="submit" class="ver-btn" style="margin-top:6px">Excluir grupo</button>
            </form>
            <table style="margin-top:10px"><tbody>{subs_rows}</tbody></table>
            <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px;padding-left:24px">
              <input type="hidden" name="acao" value="criar_subgrupo"><input type="hidden" name="grupo_id" value="{g["id"]}">
              <input name="nome" placeholder="Novo subgrupo" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:200px">
              {input_num("teto_mensal", None)}{input_num("teto_anual", None)}
              <button type="submit" class="ver-btn">+ Adicionar subgrupo</button>
            </form>
          </div>
        </details>
        """)

    def opcoes_subgrupo(categoria_selecionada):
        opts = ['<option value="">(sem grupo)</option>']
        for g in grupos_db:
            subs = subgrupos_por_grupo.get(g["id"], [])
            if not subs:
                continue
            opts.append(f'<optgroup label="{g["nome"]}">')
            for s in subs:
                sel = "selected" if mapa_categoria.get(categoria_selecionada) == s["id"] else ""
                opts.append(f'<option value="{s["id"]}" {sel}>{s["nome"]}</option>')
            opts.append('</optgroup>')
        return "".join(opts)

    todas_categorias = sorted(
        (set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA)) - set(CATEGORIAS_NAO_GASTO),
        key=lambda c: cat_pt(c).lower(),
    )
    categorias_rows = "".join(
        f'<tr><td>{cat_pt(c)}</td><td>'
        f'<form method="post" onchange="this.submit()">'
        f'<input type="hidden" name="acao" value="mapear_categoria"><input type="hidden" name="categoria" value="{c}">'
        f'<select name="subgrupo_id" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px">{opcoes_subgrupo(c)}</select>'
        f'</form></td></tr>'
        for c in todas_categorias
    )

    return f"""
    <html><head><title>Gerenciar Grupos de Custo</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Gerenciar Grupos de Custo', 'grupos')}
      <div class="wrap">
        <div style="font-size:13px;color:#666;margin-bottom:16px">
          Isto e o <strong>Centro de Custo</strong> (o que voce gastou). Para classificar por pessoa, projeto/evento
          ou outra dimensao independente, use <a href="/dimensoes">Gerenciar dimensoes</a>.
        </div>
        <div class="cat-breakdown">
          <h3>Novo grupo</h3>
          <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="hidden" name="acao" value="criar_grupo">
            <input name="nome" placeholder="Nome do grupo" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:220px">
            <span style="font-size:12px;color:#888">teto mensal</span>{input_num("teto_mensal", None)}
            <span style="font-size:12px;color:#888">teto anual</span>{input_num("teto_anual", None)}
            <button type="submit" style="background:#1d2b3a;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer">Criar grupo</button>
          </form>
        </div>

        {"".join(grupos_html)}

        <details class="cat-breakdown" style="padding:0">
          <summary style="cursor:pointer;padding:14px 18px;font-weight:600;font-size:14px">Vincular categorias aos subgrupos</summary>
          <div style="padding:0 18px 18px 18px">
            <table>
              <thead><tr><th>Categoria</th><th>Subgrupo</th></tr></thead>
              <tbody>{categorias_rows}</tbody>
            </table>
          </div>
        </details>
      </div>
    </body></html>
    """


@app.route("/relatorios")
@login_required
def relatorios():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    aplicar_regras(cur)
    conn.commit()

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cur.execute("SELECT id, dimensao_id, nome FROM cartao.dimensao_valor ORDER BY nome;")
    valores_por_dim = {}
    for v in cur.fetchall():
        valores_por_dim.setdefault(v["dimensao_id"], []).append(v)

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome ORDER BY prefixo;")
    cartoes_cadastrados = cur.fetchall()
    nomes_cartao = {c["final4"]: c["prefixo"] for c in cartoes_cadastrados}

    cur.execute("SELECT DISTINCT categoria FROM cartao.transacao WHERE categoria IS NOT NULL;")
    categorias_db = {r["categoria"] for r in cur.fetchall()}
    todas_categorias = sorted(categorias_db | set(CATEGORIAS_EXTRA), key=lambda c: cat_pt(c).lower())

    cur.execute("SELECT DISTINCT numero_cartao_final FROM cartao.transacao WHERE numero_cartao_final IS NOT NULL;")
    finais_usados = sorted({r["numero_cartao_final"] for r in cur.fetchall()})

    # ---- filtros vindos da URL (todos multi-selecionaveis) ----
    categorias_sel = request.args.getlist("categoria")
    cartoes_sel = request.args.getlist("cartao")
    data_ini = request.args.get("data_ini") or ""
    data_fim = request.args.get("data_fim") or ""
    agrupar = request.args.get("agrupar") or "categoria"
    dim_sel = {}
    for d in dimensoes:
        vals = request.args.getlist(f"dim_{d['id']}")
        if vals:
            dim_sel[d["id"]] = vals

    where = ["COALESCE(t.duplicada, false) = false"]
    params = []
    if categorias_sel:
        where.append("t.categoria IN %s")
        params.append(tuple(categorias_sel))
    else:
        # sem filtro explicito de categoria: exclui por padrao o que nao e gasto real
        # (pagamento de fatura, juros, tarifas, transferencia interna) para o total fazer sentido
        where.append("(t.categoria NOT IN %s OR t.categoria IS NULL)")
        params.append(CATEGORIAS_NAO_GASTO)
    if cartoes_sel:
        where.append("t.numero_cartao_final IN %s")
        params.append(tuple(cartoes_sel))
    if data_ini:
        where.append("t.data_transacao >= %s")
        params.append(data_ini)
    if data_fim:
        where.append("t.data_transacao <= %s")
        params.append(data_fim + " 23:59:59")
    for dim_id, vals in dim_sel.items():
        where.append(
            "EXISTS (SELECT 1 FROM cartao.transacao_dimensao td WHERE td.transacao_id = t.transacao_id::text "
            "AND td.dimensao_id = %s AND td.valor_id IN %s)"
        )
        params.append(dim_id)
        params.append(tuple(int(v) for v in vals))
    where_sql = " AND ".join(where)

    join_extra = ""
    if agrupar == "categoria":
        group_expr = "t.categoria"
    elif agrupar == "cartao":
        group_expr = "t.numero_cartao_final"
    elif agrupar == "mes":
        group_expr = "to_char(t.data_transacao, 'YYYY-MM')"
    elif agrupar.startswith("dim_"):
        dim_id_grp = agrupar.split("_", 1)[1]
        join_extra = (
            f"LEFT JOIN cartao.transacao_dimensao tdg ON tdg.transacao_id = t.transacao_id::text "
            f"AND tdg.dimensao_id = {int(dim_id_grp)} LEFT JOIN cartao.dimensao_valor dvg ON dvg.id = tdg.valor_id"
        )
        group_expr = "COALESCE(dvg.nome, '(nao definido)')"
    else:
        agrupar = "categoria"
        group_expr = "t.categoria"

    cur.execute(
        f"SELECT {group_expr} AS grupo, COUNT(*) AS qtd, SUM(COALESCE(t.valor_brl, t.valor_original)) AS total "
        f"FROM cartao.transacao t {join_extra} WHERE {where_sql} GROUP BY {group_expr} ORDER BY total DESC;",
        params,
    )
    grupos_raw = cur.fetchall()

    cur.execute(
        f"SELECT COUNT(*) AS qtd, SUM(COALESCE(t.valor_brl, t.valor_original)) AS total "
        f"FROM cartao.transacao t WHERE {where_sql};",
        params,
    )
    totalizador = cur.fetchone()
    total_geral = totalizador["total"] or 0
    qtd_geral = totalizador["qtd"] or 0

    cur.execute(
        f"SELECT t.transacao_id, t.data_transacao, t.descricao, t.categoria, "
        f"COALESCE(t.valor_brl, t.valor_original) AS valor, t.numero_cartao_final "
        f"FROM cartao.transacao t WHERE {where_sql} ORDER BY t.data_transacao DESC LIMIT 500;",
        params,
    )
    detalhe_rows = cur.fetchall()

    cur.close()
    conn.close()

    def nome_grupo(g):
        if agrupar == "categoria":
            return cat_pt(g)
        if agrupar == "cartao":
            if not g:
                return "(sem cartao)"
            prefixo = nomes_cartao.get(g)
            return f"{prefixo} - final {g}" if prefixo else f"final {g}"
        return g if g else "(nao definido)"

    def nome_cartao_curto(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return prefixo if prefixo else f"final {final4}"

    grupos_html = []
    chart_labels = []
    chart_valores = []
    for g in grupos_raw:
        total_g = g["total"] or 0
        pct = (total_g / total_geral * 100) if total_geral else 0
        nome_g = nome_grupo(g["grupo"])
        grupos_html.append(
            f'<div class="rel-grupo-row"><div style="flex:1"><div style="display:flex;justify-content:space-between">'
            f'<span>{nome_g} <span style="color:#aaa">({g["qtd"]})</span></span>'
            f'<span><strong>{_fmt_moeda(total_g)}</strong> <span style="color:#aaa">{pct:.0f}%</span></span></div>'
            f'<div class="barra"><div style="width:{max(pct, 0):.0f}%"></div></div></div></div>'
        )
        chart_labels.append(nome_g)
        chart_valores.append(round(total_g, 2))
    grupos_html_str = "".join(grupos_html) or '<div style="color:#888;padding:10px 0">Nenhum lancamento encontrado com esses filtros.</div>'

    detalhe_html = "".join(
        f'<tr><td>{(r["data_transacao"] - timedelta(hours=3)).strftime("%d/%m/%Y")}</td>'
        f'<td>{r["descricao"]}</td>'
        f'<td class="cartao-cell">{nome_cartao_curto(r["numero_cartao_final"])}</td>'
        f'<td>{cat_pt(r["categoria"])}</td>'
        f'<td class="valor">R$ {r["valor"]:,.2f}</td></tr>'
        for r in detalhe_rows
    ) or '<tr><td colspan="5" style="text-align:center;color:#888;padding:16px">Nenhum lancamento encontrado.</td></tr>'

    def chip_filter(nome, label, opcoes, selecionados):
        """opcoes: lista de (value, texto). selecionados: lista de strings (values marcados)."""
        n_sel = len(selecionados)
        opts_html = "".join(
            f'<label class="chip-opt {"chip-checked" if str(val) in selecionados else ""}">'
            f'<input type="checkbox" name="{nome}" value="{val}" {"checked" if str(val) in selecionados else ""} '
            f'onchange="cfOnChange(this)"> {texto}</label>'
            for val, texto in opcoes
        )
        return f"""
        <div class="chipfilter">
          <button type="button" class="chip-btn {"ativo" if n_sel else ""}" onclick="cfToggle(this)">
            <span class="chip-plus">+</span> {label}{f' ({n_sel})' if n_sel else ''}
            {f'<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' if n_sel else ''}
          </button>
          <div class="chip-panel">
            <div class="chip-search-wrap"><input type="text" class="chip-search" placeholder="Procure {label.lower()}..." oninput="cfFiltrar(this)" onkeydown="cfKeydown(event, this)"></div>
            <div class="chip-list">{opts_html}</div>
          </div>
        </div>
        """

    dims_filtros_html = "".join(
        chip_filter(f"dim_{d['id']}", d["nome"],
                    [(v["id"], v["nome"]) for v in valores_por_dim.get(d["id"], [])],
                    dim_sel.get(d["id"], []))
        for d in dimensoes if valores_por_dim.get(d["id"])
    )

    cartao_opcoes = [(c["final4"], f'{c["prefixo"]} - final {c["final4"]}') for c in cartoes_cadastrados]
    registrados = {c["final4"] for c in cartoes_cadastrados}
    cartao_opcoes += [(f, f"final {f}") for f in finais_usados if f not in registrados]

    agrupar_opcoes = [("categoria", "Categoria"), ("cartao", "Cartão"), ("mes", "Período (mês)")]
    agrupar_opcoes += [(f"dim_{d['id']}", d["nome"]) for d in dimensoes]
    agrupar_opcoes_html = "".join(
        f'<option value="{val}" {"selected" if val == agrupar else ""}>{label}</option>'
        for val, label in agrupar_opcoes
    )

    return f"""
    <html><head><title>Relatórios</title>{BASE_CSS}
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
    </head>
    <body>
      {topbar_html('Relatórios', 'relatorios')}
      <div class="wrap">
        <div style="font-size:12px;color:#888;margin-bottom:10px">
          Por padrão os totais nao incluem pagamento de fatura, juros, tarifas e transferencia interna (nao sao gasto real).
          Selecione uma categoria especifica para incluir esses lancamentos.
        </div>
        <form method="get" id="formFiltros">
          <div class="rel-filtros">
            <select name="agrupar" class="chip-btn" style="border-radius:20px" onchange="this.form.submit()">{agrupar_opcoes_html}</select>
            {chip_filter('categoria', 'Categoria', [(c, cat_pt(c)) for c in todas_categorias], categorias_sel)}
            {chip_filter('cartao', 'Cartão', cartao_opcoes, cartoes_sel)}
            {dims_filtros_html}
            <div class="rel-datewrap">
              <input type="date" name="data_ini" value="{data_ini}" onchange="this.form.submit()">
              <span style="color:#bbb">–</span>
              <input type="date" name="data_fim" value="{data_fim}" onchange="this.form.submit()">
            </div>
            <div class="rel-actions">
              <a href="/relatorios" class="chip-btn" style="text-decoration:none">Limpar tudo</a>
            </div>
          </div>
        </form>

        <div class="cards">
          <div class="card"><div class="label">Total no filtro</div><div class="val">{_fmt_moeda(total_geral)}</div></div>
          <div class="card"><div class="label">Lançamentos</div><div class="val">{qtd_geral}</div></div>
        </div>

        <div class="chart-card">
          <h3>Gráfico ({dict(agrupar_opcoes).get(agrupar, agrupar)})</h3>
          <canvas id="chartGrupos" height="90"></canvas>
        </div>

        <div class="cat-breakdown">
          <h3>Totais agrupados</h3>
          {grupos_html_str}
        </div>

        <details class="cat-breakdown" style="padding:0">
          <summary style="cursor:pointer;padding:14px 18px;font-weight:600;font-size:14px">Ver lançamentos ({len(detalhe_rows)}{'​' if len(detalhe_rows) < 500 else '+'})</summary>
          <div style="padding:0 18px 18px 18px">
            <table>
              <thead><tr><th>Data</th><th>Descrição</th><th>Cartão</th><th>Categoria</th><th>Valor</th></tr></thead>
              <tbody>{detalhe_html}</tbody>
            </table>
          </div>
        </details>
      </div>

      <script>
        // ---- chip filters: dropdown com busca, checkbox toggle e navegacao por teclado ----
        function cfToggle(btn) {{
          const panel = btn.nextElementSibling;
          const abrir = !panel.classList.contains('show');
          document.querySelectorAll('.chip-panel.show').forEach(p => p.classList.remove('show'));
          if (abrir) {{
            panel.classList.add('show');
            const search = panel.querySelector('.chip-search');
            if (search) {{ search.value = ''; cfFiltrar(search); search.focus(); }}
          }}
        }}
        document.addEventListener('click', function(e) {{
          if (!e.target.closest('.chipfilter')) {{
            document.querySelectorAll('.chip-panel.show').forEach(p => p.classList.remove('show'));
          }}
        }});
        function cfOnChange(input) {{
          const opt = input.closest('.chip-opt');
          opt.classList.toggle('chip-checked', input.checked);
          input.form.submit();
        }}
        function cfClear(e, btn) {{
          e.stopPropagation();
          const panel = btn.parentElement.nextElementSibling;
          panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
          btn.form ? btn.form.submit() : document.getElementById('formFiltros').submit();
        }}
        function cfFiltrar(input) {{
          const panel = input.closest('.chip-panel');
          const q = input.value.toLowerCase();
          panel.querySelectorAll('.chip-opt').forEach(opt => {{
            opt.style.display = opt.textContent.toLowerCase().includes(q) ? 'flex' : 'none';
          }});
          panel.querySelectorAll('.chip-hover').forEach(o => o.classList.remove('chip-hover'));
        }}
        function cfKeydown(e, input) {{
          const panel = input.closest('.chip-panel');
          const visiveis = Array.from(panel.querySelectorAll('.chip-opt')).filter(o => o.style.display !== 'none');
          let idx = visiveis.findIndex(o => o.classList.contains('chip-hover'));
          if (e.key === 'ArrowDown') {{
            e.preventDefault();
            if (idx >= 0) visiveis[idx].classList.remove('chip-hover');
            idx = Math.min(idx + 1, visiveis.length - 1);
            if (visiveis[idx]) visiveis[idx].classList.add('chip-hover');
          }} else if (e.key === 'ArrowUp') {{
            e.preventDefault();
            if (idx >= 0) visiveis[idx].classList.remove('chip-hover');
            idx = Math.max(idx - 1, 0);
            if (visiveis[idx]) visiveis[idx].classList.add('chip-hover');
          }} else if (e.key === 'Enter') {{
            e.preventDefault();
            if (idx >= 0) {{
              const cb = visiveis[idx].querySelector('input[type=checkbox]');
              cb.checked = !cb.checked;
              cfOnChange(cb);
            }}
          }} else if (e.key === 'Escape') {{
            panel.classList.remove('show');
          }}
        }}

        // ---- grafico dinamico conforme os filtros aplicados ----
        const chartLabels = {json.dumps(chart_labels)};
        const chartValores = {json.dumps(chart_valores)};
        if (window.Chart) {{
          new Chart(document.getElementById('chartGrupos'), {{
            type: 'bar',
            data: {{
              labels: chartLabels,
              datasets: [{{
                label: 'Total (R$)',
                data: chartValores,
                backgroundColor: '#2e6fd6',
                borderRadius: 4,
                maxBarThickness: 46
              }}]
            }},
            options: {{
              responsive: true,
              plugins: {{ legend: {{ display: false }} }},
              scales: {{ y: {{ beginAtZero: true }} }}
            }}
          }});
        }}
      </script>
    </body></html>
    """


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
