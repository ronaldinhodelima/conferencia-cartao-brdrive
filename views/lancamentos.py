"""Tela de Lancamentos e a API que ela usa."""
import uuid
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, session, jsonify, render_template

from core import (
    CATEGORIAS_EXTRA,
    CATEGORIAS_OCULTAS,
    CATEGORIA_PT_DB,
    CONTA_MANUAL_ID,
    DUPLICADA_OBS_PADRAO,
    JOIN_NATUREZA,
    NATUREZAS,
    NATUREZA_SQL,
    VAL_DESPESA,
    aplicar_regras,
    carregar_origens,
    rotulo_valor_dimensao,
    cat_pt,
    cat_pt_puro,
    chave_alfa,
    chip_filter_html,
    esc,
    get_conn,
    json_script,
    pode,
    requer,
    topbar_html,
)

bp = Blueprint("lancamentos", __name__)


@bp.route("/")
@requer("lancamentos_ver")
def index():
    mes = request.args.get("mes") or datetime.now().strftime("%Y-%m")
    status = request.args.get("status", "todas")
    origem_sel = request.args.getlist("origem")

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    aplicar_regras(cur)
    conn.commit()

    contas_by_id, origem_opcoes = carregar_origens(cur)

    # quantos lancamentos cada origem tem NO MES aberto. Nao entra o filtro de
    # origem aqui de proposito: se entrasse, marcar uma origem zeraria a contagem
    # das outras e o numero deixaria de servir para comparar.
    cur.execute(
        "SELECT account_id, COUNT(*) AS n FROM cartao.transacao "
        "WHERE to_char(data_transacao, 'YYYY-MM') = %s GROUP BY account_id;",
        (mes,),
    )
    qtd_por_origem = {str(r["account_id"]): r["n"] for r in cur.fetchall()}

    # Possiveis duplicidades do mes: mesma conta, mesmo dia e mesmo valor. O Pluggy
    # ja mandou o mesmo debito duas vezes (Cond Sta Lucia em 21/11/2025), e sem
    # aviso isso vira despesa dobrada sem ninguem notar. Quem ja foi marcado como
    # duplicada fica de fora - a decisao ja foi tomada.
    cur.execute(
        "SELECT array_agg(t.transacao_id::text) AS ids FROM cartao.transacao t "
        "WHERE to_char(t.data_transacao, 'YYYY-MM') = %s "
        "AND COALESCE(t.duplicada, false) = false "
        "GROUP BY t.account_id, t.data_transacao::date, "
        "COALESCE(t.valor_brl, t.valor_original), t.descricao "
        "HAVING COUNT(*) > 1;",
        (mes,),
    )
    ids_suspeitos = set()
    grupos_suspeitos = 0
    for r in cur.fetchall():
        grupos_suspeitos += 1
        ids_suspeitos.update(r["ids"] or [])

    cur.execute("SELECT DISTINCT categoria FROM cartao.transacao WHERE categoria IS NOT NULL;")
    categorias_db = {r["categoria"] for r in cur.fetchall()}
    categorias = sorted((categorias_db | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_OCULTAS, key=lambda c: chave_alfa(cat_pt(c)))

    where = ["to_char(t.data_transacao, 'YYYY-MM') = %s"]
    params = [mes]
    if origem_sel:
        where.append("t.account_id IN %s")
        params.append(tuple(origem_sel))
    if status == "conferida":
        where.append("t.conferida = true")
    elif status == "pendente":
        where.append("t.conferida = false")

    cur.execute(
        "SELECT t.transacao_id, t.account_id, t.data_transacao, t.descricao, t.categoria, "
        "COALESCE(t.valor_brl, t.valor_original) AS valor, t.valor_original, t.moeda_original, "
        "t.status, t.tipo, t.numero_cartao_final, t.parcela_atual, t.parcela_total, "
        "t.conferida, t.observacao, t.conferida_por, t.conferida_em, COALESCE(t.duplicada, false) AS duplicada, "
        "COALESCE(t.importado, false) AS importado, t.natureza, "
        f"{NATUREZA_SQL} AS natureza_efetiva "
        f"FROM cartao.transacao t {JOIN_NATUREZA} WHERE " + " AND ".join(where) + " ORDER BY t.data_transacao DESC;",
        params,
    )
    rows = cur.fetchall()

    # resumo do mes (nao filtrado por status, sempre do mes inteiro; duplicadas nao contam)
    where_resumo = ["to_char(t.data_transacao,'YYYY-MM') = %s", "COALESCE(t.duplicada, false) = false"]
    params_resumo = [mes]
    if origem_sel:
        where_resumo.append("t.account_id IN %s")
        params_resumo.append(tuple(origem_sel))
    # gasto real = so o que tem natureza de despesa (fatura, transferencia,
    # investimento e compra de bem nao sao gasto - ver NATUREZAS)
    cur.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN t.conferida THEN 1 ELSE 0 END) conferidas, "
        f"SUM(CASE WHEN {NATUREZA_SQL} = 'despesa' THEN {VAL_DESPESA} ELSE 0 END) AS gasto_real, "
        f"SUM(CASE WHEN {NATUREZA_SQL} = 'receita' THEN -{VAL_DESPESA} ELSE 0 END) AS receita_mes "
        f"FROM cartao.transacao t {JOIN_NATUREZA} WHERE " + " AND ".join(where_resumo) + ";",
        params_resumo,
    )
    resumo = cur.fetchone()

    where_cat = ["to_char(t.data_transacao,'YYYY-MM') = %s", f"{NATUREZA_SQL} = 'despesa'",
                 "t.categoria IS NOT NULL", "COALESCE(t.duplicada, false) = false"]
    params_cat = [mes]
    if origem_sel:
        where_cat.append("t.account_id IN %s")
        params_cat.append(tuple(origem_sel))
    cur.execute(
        f"SELECT t.categoria, SUM({VAL_DESPESA}) AS total "
        f"FROM cartao.transacao t {JOIN_NATUREZA} WHERE " + " AND ".join(where_cat) +
        " GROUP BY t.categoria ORDER BY total DESC LIMIT 8;",
        params_cat,
    )
    por_categoria = cur.fetchall()

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome;")
    nomes_cartao = {r["final4"]: esc(r["prefixo"]) for r in cur.fetchall()}

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()

    cur.execute("SELECT id, dimensao_id, nome, icone FROM cartao.dimensao_valor ORDER BY nome;")
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

    def nome_cartao_curto(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return prefixo if prefixo else f"final {final4}"

    def origem_curta(account_id, final4=None):
        """Selo do banco + texto curto. No cartao, o apelido cadastrado
        (ex: 'Andrea físico') diz mais que 'Cartão Unicred'."""
        c = contas_by_id.get(str(account_id))
        if not c:
            return "-"
        if c["tipo"] == "CREDIT" and final4 and nomes_cartao.get(final4):
            texto = nomes_cartao[final4]
        else:
            texto = c["label_curto"]
        return f'{c["selo"]}<span>{texto}</span>'

    def origem_completa(account_id, final4=None):
        c = contas_by_id.get(str(account_id))
        if not c:
            return "-"
        if c["tipo"] == "CREDIT" and final4:
            return f'{c["label"]} - {nome_cartao_curto(final4)}'
        return c["label"]

    # a tela nao deve oferecer acao que a API vai recusar
    pode_editar = pode("lancamentos_editar")
    pode_conferir = pode("lancamentos_conferir")
    pode_manual = pode("lancamentos_manual")

    def nome_cartao_curto(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return prefixo if prefixo else f"final {final4}"

    def origem_partes(account_id, final4=None):
        """(selo_html, texto). O selo e HTML montado pelo app; o texto vem do
        apelido do cartao, digitado pelo usuario, e o template escapa."""
        c = contas_by_id.get(str(account_id))
        if not c:
            return "", "-"
        if c["tipo"] == "CREDIT" and final4 and nomes_cartao.get(final4):
            return c["selo"], nomes_cartao[final4]
        return c["selo"], c["label_curto"]

    def origem_completa(account_id, final4=None):
        c = contas_by_id.get(str(account_id))
        if not c:
            return "-"
        if c["tipo"] == "CREDIT" and final4:
            return f'{c["label"]} - {nome_cartao_curto(final4)}'
        return c["label"]

    linhas_tabela = []
    detalhes_js = {}
    for r in rows:
        data_local = r["data_transacao"] - timedelta(hours=3)
        data_fmt_full = data_local.strftime("%d/%m/%Y %H:%M")
        rid = r["transacao_id"]
        desc = r["descricao"] or ""

        conta_info = contas_by_id.get(str(r["account_id"]))
        # manual (dinheiro) ou importado de arquivo: pode ser excluido pelo modal
        eh_manual = bool((conta_info and conta_info["tipo"] == "MANUAL") or r["importado"])
        eh_nao_credito = conta_info and conta_info["tipo"] != "CREDIT"
        # cartao de credito: exibicao tradicional (sem sinal). conta corrente/manual: entrada/saida
        if eh_nao_credito:
            sinal = "-" if r["tipo"] == "DEBIT" else "+"
            cor_valor = "color:#c23c34" if r["tipo"] == "DEBIT" else "color:#1f8a53"
            valor_fmt = f'{sinal} R$ {abs(r["valor"]):,.2f}'
            valor_sort = -abs(r["valor"]) if sinal == "-" else abs(r["valor"])
        else:
            cor_valor = ""
            valor_fmt = f'R$ {r["valor"]:,.2f}'
            valor_sort = r["valor"]

        dims_sel = {d["id"]: mapa_dim_transacao.get((str(rid), d["id"])) for d in dimensoes}
        selo, origem_texto = origem_partes(r["account_id"], r["numero_cartao_final"])
        origem_full = origem_completa(r["account_id"], r["numero_cartao_final"])

        linhas_tabela.append({
            "id": str(rid),
            "classes": " ".join(c for c in [
                "conferida" if r["conferida"] else "",
                "duplicada" if r["duplicada"] else "",
            ] if c),
            "data_dia": data_local.strftime("%d/%m/%y"),
            "data_hora": data_local.strftime("%H:%M"),
            "data_full": data_fmt_full,
            "data_sort": data_local.timestamp(),
            "descricao": desc,
            "origem_selo": selo,
            "origem_texto": origem_texto,
            "origem_completa": origem_full,
            "categoria": r["categoria"],
            "dims": dims_sel,
            "valor_fmt": valor_fmt,
            "valor_sort": valor_sort,
            "cor_valor": cor_valor,
            "observacao": r["observacao"] or "",
            "conferida": r["conferida"],
            "duplicada": r["duplicada"],
            "suspeita_duplicidade": str(rid) in ids_suspeitos,
        })

        nomes_por_dim = {
            d["id"]: {v["id"]: rotulo_valor_dimensao(v) for v in valores_por_dim.get(d["id"], [])}
            for d in dimensoes
        }
        detalhes = {
            "data": data_fmt_full,
            "descricao": desc,
            "categoria": cat_pt_puro(r["categoria"]),
            "valor": valor_fmt,
            "valor_original": f'{r["valor_original"]:,.2f} {r["moeda_original"] or ""}' if r["valor_original"] is not None else "-",
            "status": r["status"] or "-",
            "tipo": r["tipo"] or "-",
            "origem": origem_full,
            "parcela": f'{r["parcela_atual"]}/{r["parcela_total"]}' if r["parcela_total"] and r["parcela_total"] > 1 else "À vista",
            "conferida": "Sim" if r["conferida"] else "Não",
            "conferida_por": r["conferida_por"] or "-",
            "observacao": r["observacao"] or "-",
            "_manual": bool(eh_manual),
            "_natureza": r["natureza"] or "",
            "_natureza_efetiva": NATUREZAS.get(r["natureza_efetiva"], r["natureza_efetiva"]),
        }
        for d in dimensoes:
            detalhes[d["nome"]] = nomes_por_dim[d["id"]].get(dims_sel[d["id"]], "(nao definido)")
        detalhes_js[str(rid)] = detalhes

    gasto_real = resumo["gasto_real"] or 0
    receita_mes = resumo["receita_mes"] or 0

    return render_template(
        "index.html",
        titulo="Lançamentos",
        topbar=topbar_html("Lançamentos", "inicio"),
        mes=mes,
        status=status,
        hoje_iso=datetime.now().strftime("%Y-%m-%d"),
        origem_filtro_html=chip_filter_html(
            "origem", "Origem", origem_opcoes, origem_sel,
            onchange="aplicarFiltros()", contagens=qtd_por_origem,
        ),
        pode_editar=pode_editar,
        pode_conferir=pode_conferir,
        pode_manual=pode_manual,
        categorias=[{"chave": c, "nome": cat_pt_puro(c)} for c in categorias],
        dimensoes=dimensoes,
        valores_por_dim=valores_por_dim,
        naturezas=NATUREZAS,
        linhas=linhas_tabela,
        por_categoria=[
            {"nome": cat_pt_puro(c["categoria"]), "total": float(c["total"])} for c in por_categoria
        ],
        receita_mes=receita_mes,
        gasto_real=gasto_real,
        resultado_mes=receita_mes - gasto_real,
        conf=resumo["conferidas"] or 0,
        total=resumo["total"] or 0,
        grupos_suspeitos=grupos_suspeitos,
        detalhes_json=json_script(detalhes_js),
        config_json=json_script({"duplicada_obs": DUPLICADA_OBS_PADRAO}),
    )


@bp.route("/api/lancamento-manual", methods=["POST"])
@requer("lancamentos_manual")
def lancamento_manual():
    data = request.get_json(force=True)
    try:
        data_str = (data.get("data") or "").strip()
        descricao = (data.get("descricao") or "").strip()
        direcao = data.get("direcao")
        valor = float(str(data.get("valor") or "0").replace(",", "."))
        categoria = data.get("categoria") or None
        if not data_str or not descricao or valor <= 0 or direcao not in ("entrada", "saida"):
            return jsonify({"ok": False, "erro": "Preencha data, descrição e um valor válido."}), 400

        tipo = "CREDIT" if direcao == "entrada" else "DEBIT"
        data_transacao = f"{data_str} 12:00:00-03:00"

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO cartao.transacao ("
            "transacao_id, account_id, descricao, descricao_bruta, valor_original, moeda_original, "
            "valor_brl, data_transacao, categoria, status, tipo, criado_em, atualizado_em, sincronizado_em"
            ") VALUES (%s,%s,%s,%s,%s,'BRL',%s,%s,%s,'POSTED',%s, now(), now(), now());",
            (
                str(uuid.uuid4()), CONTA_MANUAL_ID, descricao, descricao,
                valor, valor, data_transacao, categoria, tipo,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400


@bp.route("/api/lancamento-manual/<transacao_id>", methods=["DELETE"])
@requer("lancamentos_manual")
def excluir_lancamento_manual(transacao_id):
    """Exclui um lancamento criado manualmente ou importado de arquivo. Transacoes vindas do
    Pluggy nunca sao apagadas (elas voltariam na proxima sincronizacao)."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT account_id, COALESCE(importado, false) FROM cartao.transacao WHERE transacao_id = %s;",
            (transacao_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return jsonify({"ok": False, "erro": "Lançamento não encontrado."}), 404
        if str(row[0]) != CONTA_MANUAL_ID and not row[1]:
            cur.close()
            conn.close()
            return jsonify({
                "ok": False,
                "erro": "Só é possível excluir lançamentos manuais ou importados de arquivo. Este veio da sincronização com o banco e voltaria na próxima atualização — marque como duplicada se quiser ignorá-lo.",
            }), 400

        cur.execute("DELETE FROM cartao.transacao_dimensao WHERE transacao_id = %s;", (str(transacao_id),))
        cur.execute("DELETE FROM cartao.transacao WHERE transacao_id = %s;", (transacao_id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400


@bp.route("/api/transacao/<transacao_id>")
@requer("lancamentos_ver")
def detalhes_transacao(transacao_id):
    """Detalhes de um lancamento, para telas que nao carregam a tabela inteira.

    A tela de Lancamentos ja recebe tudo embutido no HTML; quem usa isto e o modal
    de /categorias, que precisa abrir um lancamento avulso.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT t.transacao_id, t.account_id, t.data_transacao, t.descricao, t.categoria, "
        "COALESCE(t.valor_brl, t.valor_original) AS valor, t.valor_original, t.moeda_original, "
        "t.status, t.tipo, t.numero_cartao_final, t.parcela_atual, t.parcela_total, "
        "t.conferida, t.observacao, t.conferida_por, t.natureza, "
        f"{NATUREZA_SQL} AS natureza_efetiva "
        f"FROM cartao.transacao t {JOIN_NATUREZA} WHERE t.transacao_id = %s;",
        (transacao_id,),
    )
    r = cur.fetchone()
    if not r:
        cur.close()
        conn.close()
        return jsonify({"ok": False, "erro": "Lançamento não encontrado."}), 404

    contas_by_id, _ = carregar_origens(cur)
    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome;")
    nomes_cartao = {c["final4"]: c["prefixo"] for c in cur.fetchall()}
    cur.close()
    conn.close()

    conta = contas_by_id.get(str(r["account_id"]))
    if not conta:
        origem = "-"
    elif conta["tipo"] == "CREDIT" and r["numero_cartao_final"]:
        apelido = nomes_cartao.get(r["numero_cartao_final"]) or f'final {r["numero_cartao_final"]}'
        origem = f'{conta["label"]} - {apelido}'
    else:
        origem = conta["label"]

    local = r["data_transacao"] - timedelta(hours=3) if r["data_transacao"] else None
    return jsonify({
        "ok": True,
        "transacao_id": str(r["transacao_id"]),
        "data": local.strftime("%d/%m/%Y %H:%M") if local else "-",
        "descricao": r["descricao"] or "-",
        "categoria": r["categoria"] or "",
        "categoria_nome": cat_pt_puro(r["categoria"]),
        "valor": f'R$ {float(r["valor"] or 0):,.2f}',
        "valor_original": (
            f'{float(r["valor_original"]):,.2f} {r["moeda_original"] or ""}'.strip()
            if r["valor_original"] is not None else "-"
        ),
        "status": r["status"] or "-",
        "tipo": r["tipo"] or "-",
        "origem": origem,
        "parcela": (
            f'{r["parcela_atual"]}/{r["parcela_total"]}'
            if r["parcela_total"] and r["parcela_total"] > 1 else "À vista"
        ),
        "conferida": "Sim" if r["conferida"] else "Não",
        "conferida_por": r["conferida_por"] or "-",
        "observacao": r["observacao"] or "-",
        "natureza_efetiva": NATUREZAS.get(r["natureza_efetiva"], r["natureza_efetiva"]),
    })


@bp.route("/api/transacao/<transacao_id>", methods=["POST"])
@requer("lancamentos_editar")
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
    bloqueada = bool(faltando) and bool(data.get("conferida", False))
    conferida_final = data.get("conferida", False) and not bloqueada

    # natureza especifica deste lancamento ("" = volta a seguir a natureza da categoria)
    natureza = data.get("natureza")
    natureza = natureza if natureza in NATUREZAS else None

    # So altera o que veio no payload. A tela de Lancamentos manda o lancamento
    # inteiro a cada edicao, mas o modal de /categorias manda so a categoria - com
    # UPDATE fixo de todas as colunas, isso apagaria conferida, duplicada e a
    # observacao de quem so queria trocar a categoria.
    sets, valores = [], []
    if "conferida" in data:
        sets += [
            "conferida = %s",
            "conferida_por = CASE WHEN %s THEN %s ELSE conferida_por END",
            "conferida_em = CASE WHEN %s THEN now() ELSE conferida_em END",
        ]
        valores += [conferida_final, conferida_final, session.get("user"), conferida_final]
    if "duplicada" in data:
        sets.append("duplicada = %s")
        valores.append(bool(data.get("duplicada")))
    if "observacao" in data:
        sets.append("observacao = %s")
        valores.append(data.get("observacao"))
    if "categoria" in data:
        sets.append("categoria = %s")
        valores.append(data.get("categoria"))
    if "natureza" in data:
        sets.append("natureza = %s")
        valores.append(natureza)

    if sets:
        # os trechos do SET sao literais fixos daqui; so os valores vao por parametro
        cur.execute(
            f"UPDATE cartao.transacao SET {', '.join(sets)} WHERE transacao_id = %s;",
            valores + [transacao_id],
        )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "bloqueada": bloqueada, "faltando": faltando})
