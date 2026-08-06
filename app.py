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

        cur.close()
        conn.close()
    except Exception as e:
        print("Aviso: falha ao rodar migracao:", e)


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
</style>
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

    cur.close()
    conn.close()

    def nome_cartao(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return f"{prefixo} - final {final4}" if prefixo else f"final {final4}"

    dia_vencimento = conta_row["vencimento_fatura"].day if conta_row and conta_row["vencimento_fatura"] else None
    proximo_fechamento = proxima_ocorrencia_dia(FATURA_DIA_FECHAMENTO)
    proximo_vencimento = proxima_ocorrencia_dia(dia_vencimento) if dia_vencimento else None

    def cat_options(selected):
        return "".join(
            f'<option value="{c}" {"selected" if c == selected else ""}>{cat_pt(c)}</option>'
            for c in categorias
        )

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
        trs.append(
            f'<tr class="{classes}" data-id="{rid}" onclick="linhaClick(event, \'{rid}\')">'
            f'<td>{data_fmt}</td>'
            f'<td>{r["descricao"]}</td>'
            f'<td>{nome_cartao(r["numero_cartao_final"])}</td>'
            f'<td><select class="cat-select" onchange="salvar(\'{rid}\', this)">{cat_options(r["categoria"])}</select></td>'
            f'<td class="valor">R$ {r["valor"]:,.2f}</td>'
            f'<td><input class="obs-input" type="text" value="{obs}" placeholder="observacao..." onblur="salvar(\'{rid}\', this)"></td>'
            f'<td style="text-align:center"><input class="conf-check" type="checkbox" {checked} onchange="salvar(\'{rid}\', this)"></td>'
            f'<td style="text-align:center"><input class="dup-check" type="checkbox" {dup_checked} onchange="toggleDuplicada(\'{rid}\', this)"></td>'
            f'<td><span class="status" id="status-{rid}">salvo</span></td>'
            f'</tr>'
        )
        detalhes_js[str(rid)] = {
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

    total = resumo["total"] or 0
    conf = resumo["conferidas"] or 0
    gasto_real = resumo["gasto_real"] or 0
    body_rows = "".join(trs) if trs else '<tr><td colspan="9" style="padding:20px;text-align:center;color:#888">Nenhuma transacao neste filtro.</td></tr>'

    cat_rows_html = "".join(
        f'<div class="cat-row"><span>{cat_pt(c["categoria"])}</span><span>R$ {c["total"]:,.2f}</span></div>'
        for c in por_categoria
    ) or '<div class="cat-row"><span>Sem dados</span></div>'

    return f"""
    <html><head><title>Conferencia de Cartao</title>{BASE_CSS}</head>
    <body>
      <div class="topbar">
        <div>Conferencia de Cartao - {session.get('user')}</div>
        <div style="display:flex;gap:18px;align-items:center">
          <a href="/dre">DRE / Centro de Custos</a>
          <a href="/cartoes">Gerenciar cartoes</a>
          <a href="/logout">Sair</a>
        </div>
      </div>
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
            <th>Data</th><th>Descricao</th><th>Cartao</th><th>Categoria</th><th>Valor</th><th>Observacao</th><th>Conferida</th><th>Duplicada</th><th></th>
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
          const payload = {{
            conferida: tr.querySelector('.conf-check').checked,
            duplicada: tr.querySelector('.dup-check').checked,
            observacao: tr.querySelector('.obs-input').value,
            categoria: tr.querySelector('.cat-select').value
          }};
          const anterior = filaSalvar[id] || Promise.resolve();
          const atual = anterior.then(() => fetch('/api/transacao/' + id, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
          }})).then(r => r.json()).then(d => {{
            if (d.ok) {{
              tr.classList.toggle('conferida', payload.conferida);
              tr.classList.toggle('duplicada', payload.duplicada);
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
    cur.execute(
        "UPDATE cartao.transacao SET conferida = %s, duplicada = %s, observacao = %s, categoria = %s, "
        "conferida_por = CASE WHEN %s THEN %s ELSE conferida_por END, "
        "conferida_em = CASE WHEN %s THEN now() ELSE conferida_em END "
        "WHERE transacao_id = %s;",
        (
            data.get("conferida", False),
            data.get("duplicada", False),
            data.get("observacao"),
            data.get("categoria"),
            data.get("conferida", False),
            session.get("user"),
            data.get("conferida", False),
            transacao_id,
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


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
      <div class="topbar">
        <div>Gerenciar Cartoes - {session.get('user')}</div>
        <div style="display:flex;gap:18px;align-items:center">
          <a href="/">Voltar</a>
          <a href="/logout">Sair</a>
        </div>
      </div>
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

    anos_opcoes = "".join(
        f'<option value="{a}" {"selected" if str(a)==ano else ""}>{a}</option>'
        for a in range(hoje.year - 3, hoje.year + 1)
    )

    return f"""
    <html><head><title>DRE / Centro de Custos</title>{BASE_CSS}</head>
    <body>
      <div class="topbar">
        <div>DRE / Centro de Custos - {session.get('user')}</div>
        <div style="display:flex;gap:18px;align-items:center">
          <a href="/grupos">Gerenciar grupos</a>
          <a href="/">Voltar</a>
          <a href="/logout">Sair</a>
        </div>
      </div>
      <div class="wrap">
        <div class="filters">
          <div>
            <label>Ano</label>
            <select onchange="window.location='/dre?ano='+this.value">{anos_opcoes}</select>
          </div>
          <div style="margin-left:auto;font-size:14px"><strong>Total do ano: {_fmt_moeda(total_geral_anual)}</strong></div>
        </div>
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
        <div class="cat-breakdown">
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
      <div class="topbar">
        <div>Gerenciar Grupos de Custo - {session.get('user')}</div>
        <div style="display:flex;gap:18px;align-items:center">
          <a href="/dre">Ver DRE</a>
          <a href="/">Voltar</a>
          <a href="/logout">Sair</a>
        </div>
      </div>
      <div class="wrap">
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

        <div class="cat-breakdown">
          <h3>Vincular categorias aos subgrupos</h3>
          <table>
            <thead><tr><th>Categoria</th><th>Subgrupo</th></tr></thead>
            <tbody>{categorias_rows}</tbody>
          </table>
        </div>
      </div>
    </body></html>
    """


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
