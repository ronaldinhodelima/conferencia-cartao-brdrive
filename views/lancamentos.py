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
        })

        nomes_por_dim = {
            d["id"]: {v["id"]: v["nome"] for v in valores_por_dim.get(d["id"], [])}
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
        origem_filtro_html=chip_filter_html("origem", "Origem", origem_opcoes, origem_sel, onchange="aplicarFiltros()"),
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
    bloqueada = bool(faltando) and data.get("conferida", False)
    conferida_final = data.get("conferida", False) and not bloqueada

    # natureza especifica deste lancamento ("" = volta a seguir a natureza da categoria)
    natureza = data.get("natureza")
    natureza = natureza if natureza in NATUREZAS else None

    cur.execute(
        "UPDATE cartao.transacao SET conferida = %s, duplicada = %s, observacao = %s, categoria = %s, "
        "natureza = %s, "
        "conferida_por = CASE WHEN %s THEN %s ELSE conferida_por END, "
        "conferida_em = CASE WHEN %s THEN now() ELSE conferida_em END "
        "WHERE transacao_id = %s;",
        (
            conferida_final,
            data.get("duplicada", False),
            data.get("observacao"),
            data.get("categoria"),
            natureza,
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
