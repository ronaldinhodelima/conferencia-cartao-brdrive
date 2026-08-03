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
}

# categorias que não representam gasto real (usadas para excluir do resumo)
CATEGORIAS_NAO_GASTO = ("Credit card payment", "Interests charged", "Credit card fees", "Transfer - Internal")

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
    categorias = sorted((r["categoria"] for r in cur.fetchall()), key=lambda c: cat_pt(c).lower())

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
        "conferida, observacao, conferida_por, conferida_em "
        "FROM cartao.transacao WHERE " + " AND ".join(where) + " ORDER BY data_transacao DESC;",
        params,
    )
    rows = cur.fetchall()

    # resumo do mes (nao filtrado por status, sempre do mes inteiro)
    cur.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN conferida THEN 1 ELSE 0 END) conferidas, "
        "SUM(CASE WHEN categoria NOT IN %s THEN COALESCE(valor_brl, valor_original) ELSE 0 END) AS gasto_real, "
        "SUM(COALESCE(valor_brl, valor_original)) AS total_bruto "
        "FROM cartao.transacao WHERE to_char(data_transacao,'YYYY-MM') = %s;",
        (CATEGORIAS_NAO_GASTO, mes),
    )
    resumo = cur.fetchone()

    cur.execute(
        "SELECT categoria, SUM(COALESCE(valor_brl, valor_original)) AS total "
        "FROM cartao.transacao WHERE to_char(data_transacao,'YYYY-MM') = %s "
        "AND categoria NOT IN %s AND categoria IS NOT NULL "
        "GROUP BY categoria ORDER BY total DESC LIMIT 8;",
        (mes, CATEGORIAS_NAO_GASTO),
    )
    por_categoria = cur.fetchall()

    cur.execute("SELECT vencimento_fatura FROM cartao.conta LIMIT 1;")
    conta_row = cur.fetchone()

    cur.close()
    conn.close()

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
        row_class = "conferida" if r["conferida"] else ""
        data_local = r["data_transacao"] - timedelta(hours=3)
        data_fmt = data_local.strftime("%d/%m/%Y %H:%M")
        obs = (r["observacao"] or "").replace('"', "&quot;")
        rid = r["transacao_id"]
        trs.append(
            f'<tr class="{row_class}" data-id="{rid}" onclick="linhaClick(event, \'{rid}\')">'
            f'<td>{data_fmt}</td>'
            f'<td>{r["descricao"]}</td>'
            f'<td><select class="cat-select" onchange="salvar(\'{rid}\', this)">{cat_options(r["categoria"])}</select></td>'
            f'<td class="valor">R$ {r["valor"]:,.2f}</td>'
            f'<td><input class="obs-input" type="text" value="{obs}" placeholder="observacao..." onblur="salvar(\'{rid}\', this)"></td>'
            f'<td style="text-align:center"><input type="checkbox" {checked} onchange="salvar(\'{rid}\', this)"></td>'
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
            "cartao": ("final " + r["numero_cartao_final"]) if r["numero_cartao_final"] else "-",
            "parcela": f'{r["parcela_atual"]}/{r["parcela_total"]}' if r["parcela_total"] and r["parcela_total"] > 1 else "À vista",
            "conferida": "Sim" if r["conferida"] else "Não",
            "conferida_por": r["conferida_por"] or "-",
            "observacao": r["observacao"] or "-",
        }

    total = resumo["total"] or 0
    conf = resumo["conferidas"] or 0
    gasto_real = resumo["gasto_real"] or 0
    body_rows = "".join(trs) if trs else '<tr><td colspan="7" style="padding:20px;text-align:center;color:#888">Nenhuma transacao neste filtro.</td></tr>'

    cat_rows_html = "".join(
        f'<div class="cat-row"><span>{cat_pt(c["categoria"])}</span><span>R$ {c["total"]:,.2f}</span></div>'
        for c in por_categoria
    ) or '<div class="cat-row"><span>Sem dados</span></div>'

    return f"""
    <html><head><title>Conferencia de Cartao</title>{BASE_CSS}</head>
    <body>
      <div class="topbar">
        <div>Conferencia de Cartao - {session.get('user')}</div>
        <a href="/logout">Sair</a>
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
            <th>Data</th><th>Descricao</th><th>Categoria</th><th>Valor</th><th>Observacao</th><th>Conferida</th><th></th>
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
        const filaSalvar = {{}};
        function salvar(id, el) {{
          const tr = el.closest('tr');
          const payload = {{
            conferida: tr.querySelector('input[type=checkbox]').checked,
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
              const s = document.getElementById('status-' + id);
              s.classList.add('show');
              setTimeout(() => s.classList.remove('show'), 1500);
            }}
          }});
          filaSalvar[id] = atual;
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
        "UPDATE cartao.transacao SET conferida = %s, observacao = %s, categoria = %s, "
        "conferida_por = CASE WHEN %s THEN %s ELSE conferida_por END, "
        "conferida_em = CASE WHEN %s THEN now() ELSE conferida_em END "
        "WHERE transacao_id = %s;",
        (
            data.get("conferida", False),
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


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
