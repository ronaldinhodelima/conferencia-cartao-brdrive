"""Relatorios, DRE e investimentos."""
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, jsonify, render_template

from core import (
    CATEGORIAS_EXTRA,
    CATEGORIAS_OCULTAS,
    CATEGORIA_PT_DB,
    JOIN_NATUREZA,
    MESES_ABREV,
    NATUREZA_SQL,
    VAL_DESPESA,
    _montar_filtro_relatorio,
    aplicar_regras,
    aviso_pendencias_html,
    carregar_origens,
    cat_pt_puro,
    chave_alfa,
    chip_filter_html,
    get_conn,
    levantar_pendencias,
    pode,
    requer,
    topbar_html,
)

bp = Blueprint("relatorios", __name__)


@bp.route("/dre")
@requer("relatorios")
def dre():
    ano = request.args.get("ano") or str(datetime.now().year)
    hoje = datetime.now()

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    base = f"FROM cartao.transacao t {JOIN_NATUREZA} WHERE COALESCE(t.duplicada, false) = false "

    cur.execute(
        f"SELECT t.categoria, SUM({VAL_DESPESA}) AS total {base} "
        f"AND to_char(t.data_transacao,'YYYY') = %s AND {NATUREZA_SQL} = 'despesa' "
        "AND t.categoria IS NOT NULL GROUP BY t.categoria;",
        (ano,),
    )
    anual_por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}

    # ---- DRE propriamente dito: receitas, despesas e resultado de cada mes do ano ----
    cur.execute(
        f"SELECT to_char(t.data_transacao,'YYYY-MM') AS mes, {NATUREZA_SQL} AS natureza, "
        f"SUM({VAL_DESPESA}) AS total {base} AND to_char(t.data_transacao,'YYYY') = %s "
        f"GROUP BY 1, 2 ORDER BY 1;",
        (ano,),
    )
    meses_dre = {}
    for r in cur.fetchall():
        m = meses_dre.setdefault(r["mes"], {"receita": 0.0, "despesa": 0.0, "investimento": 0.0, "bem": 0.0})
        v = float(r["total"] or 0)
        if r["natureza"] == "receita":
            m["receita"] += -v          # receita entra: VAL_DESPESA e negativo
        elif r["natureza"] == "despesa":
            m["despesa"] += v
        elif r["natureza"] in ("investimento", "bem"):
            m[r["natureza"]] += v       # positivo = dinheiro aplicado/investido no bem

    cur.execute(
        "SELECT g.id AS grupo_id, g.nome AS grupo_nome, "
        "s.id AS subgrupo_id, s.nome AS subgrupo_nome, "
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
            f"SUM({VAL_DESPESA}) AS total "
            f"FROM cartao.transacao t {JOIN_NATUREZA} "
            "LEFT JOIN cartao.transacao_dimensao td ON td.transacao_id = t.transacao_id::text AND td.dimensao_id = %s "
            "LEFT JOIN cartao.dimensao_valor dv ON dv.id = td.valor_id "
            "WHERE to_char(t.data_transacao,'YYYY') = %s AND COALESCE(t.duplicada, false) = false "
            f"AND {NATUREZA_SQL} = 'despesa' AND t.categoria IS NOT NULL "
            "GROUP BY dv.nome ORDER BY total DESC;",
            (d["id"], ano),
        )
        por_dimensao.append({"nome": d["nome"], "linhas": cur.fetchall()})

    # o DRE e onde a ma classificacao vira numero errado - avisa aqui
    aviso_pend = aviso_pendencias_html(levantar_pendencias(cur)) if pode("cadastros") else ""

    cur.close()
    conn.close()

    grupos = {}
    categorias_mapeadas = set()
    for r in linhas_map:
        g = grupos.setdefault(r["grupo_id"], {
            "nome": r["grupo_nome"],
            "subgrupos": {},
        })
        s = g["subgrupos"].setdefault(r["subgrupo_id"], {
            "nome": r["subgrupo_nome"],
            "categorias": [],
        })
        if r["categoria"]:
            s["categorias"].append(r["categoria"])
            categorias_mapeadas.add(r["categoria"])

    nao_classificadas = sorted(set(anual_por_cat) - categorias_mapeadas)

    # ---- centro de custo: total do ano por grupo > subgrupo ----
    blocos_grupo = []
    for g in sorted(grupos.values(), key=lambda x: chave_alfa(x["nome"])):
        subs = []
        for s in sorted(g["subgrupos"].values(), key=lambda x: chave_alfa(x["nome"])):
            s_anual = sum(anual_por_cat.get(c, 0.0) for c in s["categorias"])
            subs.append({
                "nome": s["nome"],
                "total": s_anual,
                "categorias": ", ".join(cat_pt_puro(c) for c in s["categorias"]) or "sem categorias vinculadas",
            })
        blocos_grupo.append({
            "nome": g["nome"],
            "total": sum(s["total"] for s in subs),
            "subgrupos": subs,
        })

    blocos_dimensao = [{
        "nome": pd["nome"],
        "linhas": [{"nome": l["nome"], "total": float(l["total"] or 0)} for l in pd["linhas"]],
    } for pd in por_dimensao]

    # ---- DRE: um mes por linha, do mais recente para o mais antigo ----
    rec_ano = sum(m["receita"] for m in meses_dre.values())
    desp_ano = sum(m["despesa"] for m in meses_dre.values())
    inv_ano = sum(m["investimento"] + m["bem"] for m in meses_dre.values())

    linhas_dre = []
    for mes_key in sorted(meses_dre, reverse=True):
        m = meses_dre[mes_key]
        res = m["receita"] - m["despesa"]
        linhas_dre.append({
            "rotulo": f'{MESES_ABREV[int(mes_key[5:7]) - 1]}/{mes_key[2:4]}',
            "receita": m["receita"],
            "despesa": m["despesa"],
            "resultado": res,
            "margem": (res / m["receita"] * 100) if m["receita"] else 0,
            "investido": m["investimento"] + m["bem"],
        })

    return render_template(
        "dre.html",
        titulo="DRE / Centro de Custos",
        topbar=topbar_html("DRE / Centro de Custos", "dre"),
        aviso_pend=aviso_pend,
        ano=ano,
        anos=list(range(hoje.year - 3, hoje.year + 1)),
        rec_ano=rec_ano,
        desp_ano=desp_ano,
        resultado_ano=rec_ano - desp_ano,
        inv_ano=inv_ano,
        linhas_dre=linhas_dre,
        blocos_dimensao=blocos_dimensao,
        grupos=blocos_grupo,
        nao_classificadas=[
            {"nome": cat_pt_puro(c), "total": anual_por_cat[c]} for c in nao_classificadas
        ],
    )


@bp.route("/relatorios")
@requer("relatorios")
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

    cur.execute("SELECT DISTINCT categoria FROM cartao.transacao WHERE categoria IS NOT NULL;")
    categorias_db = {r["categoria"] for r in cur.fetchall()}
    todas_categorias = sorted((categorias_db | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_OCULTAS, key=lambda c: chave_alfa(cat_pt_puro(c)))

    cur.execute("SELECT DISTINCT numero_cartao_final FROM cartao.transacao WHERE numero_cartao_final IS NOT NULL;")
    finais_usados = sorted({r["numero_cartao_final"] for r in cur.fetchall()})

    contas_by_id, origem_opcoes = carregar_origens(cur)

    cfg = _montar_filtro_relatorio(dimensoes)
    cur.close()
    conn.close()

    cartao_opcoes = [(c["final4"], f'{c["prefixo"]} - final {c["final4"]}') for c in cartoes_cadastrados]
    registrados = {c["final4"] for c in cartoes_cadastrados}
    cartao_opcoes += [(f, f"final {f}") for f in finais_usados if f not in registrados]

    # cada filtro de chip ja vem como HTML pronto de chip_filter_html(); o template
    # so injeta na ordem, marcando |safe
    filtros_chip = [
        chip_filter_html("origem", "Origem", origem_opcoes, cfg["origens_sel"]),
        chip_filter_html("categoria", "Categoria",
                         [(c, cat_pt_puro(c)) for c in todas_categorias], cfg["categorias_sel"]),
        chip_filter_html("cartao", "Cartão", cartao_opcoes, cfg["cartoes_sel"]),
    ]
    filtros_chip += [
        chip_filter_html(f"dim_{d['id']}", d["nome"],
                         [(v["id"], v["nome"]) for v in valores_por_dim.get(d["id"], [])],
                         cfg["dim_sel"].get(d["id"], []))
        for d in dimensoes if valores_por_dim.get(d["id"])
    ]

    agrupar_opcoes = [("categoria", "Categoria"), ("origem", "Origem"),
                      ("cartao", "Cartão"), ("mes", "Período (mês)"), ("ano", "Período (ano)")]
    agrupar_opcoes += [(f"dim_{d['id']}", d["nome"]) for d in dimensoes]

    return render_template(
        "relatorios.html",
        titulo="Relatórios",
        topbar=topbar_html("Relatórios", "relatorios"),
        visao=cfg["visao"],
        visao_opcoes=[
            ("despesa", "Despesas"),
            ("receita", "Receitas"),
            ("investimento", "Investimentos e bens"),
            ("tudo", "Tudo (fluxo de caixa)"),
        ],
        agrupar=cfg["agrupar"],
        agrupar_opcoes=agrupar_opcoes,
        filtros_chip=filtros_chip,
        data_ini=cfg["data_ini"],
        data_fim=cfg["data_fim"],
    )


@bp.route("/relatorios/dados")
@requer("relatorios")
def relatorios_dados():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cfg = _montar_filtro_relatorio(dimensoes)

    # agrupando por periodo o resultado e uma linha do tempo: ordena cronologicamente
    # (do mais antigo para o mais recente). Nos demais agrupamentos, maior valor primeiro.
    # periodo sai em ordem cronologica; o resto, do maior gasto para o menor
    ordem = f"{cfg['group_expr']} ASC" if cfg["agrupar"] in ("mes", "ano") else "total DESC"
    cur.execute(
        f"SELECT {cfg['group_expr']} AS grupo, COUNT(*) AS qtd, SUM({cfg['soma_expr']}) AS total "
        f"FROM cartao.transacao t {cfg['join_natureza']} {cfg['join_extra']} "
        f"WHERE {cfg['where_sql']} GROUP BY {cfg['group_expr']} ORDER BY {ordem};",
        cfg["params"],
    )
    grupos_raw = cur.fetchall()

    cur.execute(
        f"SELECT COUNT(*) AS qtd, SUM({cfg['soma_expr']}) AS total "
        f"FROM cartao.transacao t {cfg['join_natureza']} WHERE {cfg['where_sql']};",
        cfg["params"],
    )
    totalizador = cur.fetchone()
    total_geral = float(totalizador["total"] or 0)
    qtd_geral = totalizador["qtd"] or 0

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome;")
    nomes_cartao = {r["final4"]: r["prefixo"] for r in cur.fetchall()}

    contas_by_id, _ = carregar_origens(cur)

    cur.close()
    conn.close()

    def selo_grupo(g):
        """Selo do banco quando agrupado por origem (o grafico usa so o nome puro)."""
        if cfg["agrupar"] != "origem":
            return ""
        c = contas_by_id.get(str(g))
        return c["selo"] if c else ""

    def nome_grupo(g):
        if cfg["agrupar"] == "categoria":
            return cat_pt_puro(g)
        if cfg["agrupar"] == "cartao":
            if not g:
                return "(sem cartao)"
            prefixo = nomes_cartao.get(g)
            return f"{prefixo} - final {g}" if prefixo else f"final {g}"
        if cfg["agrupar"] == "origem":
            c = contas_by_id.get(str(g))
            return c["label"] if c else "(sem origem)"
        if cfg["agrupar"] == "ano":
            return str(g) if g else "(sem periodo)"
        if cfg["agrupar"] == "mes":
            # '2026-01' -> 'jan/26', mais legivel na linha do tempo
            try:
                ano, mes = str(g).split("-")
                return f"{MESES_ABREV[int(mes) - 1]}/{ano[2:]}"
            except (ValueError, IndexError):
                return g or "(sem periodo)"
        return g if g else "(nao definido)"

    grupos = []
    for g in grupos_raw:
        total_g = float(g["total"] or 0)
        pct = (total_g / total_geral * 100) if total_geral else 0
        grupos.append({
            "valor": g["grupo"],
            "nome": nome_grupo(g["grupo"]),
            "selo": selo_grupo(g["grupo"]),
            "qtd": g["qtd"],
            "total": round(total_g, 2),
            "pct": round(pct, 1),
        })

    agrupar_labels = {"categoria": "Categoria", "origem": "Origem", "cartao": "Cartão", "mes": "Período (mês)"}
    for d in dimensoes:
        agrupar_labels[f"dim_{d['id']}"] = d["nome"]

    return jsonify({
        "total_geral": round(total_geral, 2),
        "qtd_geral": qtd_geral,
        "visao": cfg["visao"],
        "agrupar": cfg["agrupar"],
        "agrupar_label": agrupar_labels.get(cfg["agrupar"], cfg["agrupar"]),
        "grupos": grupos,
    })


@bp.route("/relatorios/lancamentos")
@requer("relatorios")
def relatorios_lancamentos():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cfg = _montar_filtro_relatorio(dimensoes)

    where_sql = cfg["where_sql"]
    params = list(cfg["params"])
    if request.args.get("valor_none") == "1":
        where_sql += f" AND {cfg['group_expr']} IS NULL"
    elif request.args.get("valor") is not None:
        where_sql += f" AND {cfg['group_expr']} = %s"
        params.append(request.args.get("valor"))

    cur.execute(
        f"SELECT t.data_transacao, t.descricao, t.categoria, {cfg['soma_expr']} AS valor, "
        f"t.numero_cartao_final, t.account_id FROM cartao.transacao t {cfg['join_natureza']} {cfg['join_extra']} "
        f"WHERE {where_sql} ORDER BY t.data_transacao DESC LIMIT 300;",
        params,
    )
    rows = cur.fetchall()

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome;")
    nomes_cartao = {r["final4"]: r["prefixo"] for r in cur.fetchall()}

    contas_by_id, _ = carregar_origens(cur)

    cur.close()
    conn.close()

    def nome_cartao_curto(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return prefixo if prefixo else f"final {final4}"

    def origem_de(r):
        """(selo_html, texto_curto, texto_completo).

        O selo e HTML montado pelo app; o resto e texto puro e quem escapa e o
        relatorios.js, na hora de montar o innerHTML. Antes isso vinha tudo
        concatenado e so o apelido do cartao era escapado - a descricao e o
        label da conta iam crus para o innerHTML.
        """
        c = contas_by_id.get(str(r["account_id"]))
        if not c:
            return "", "-", "-"
        apelido = nomes_cartao.get(r["numero_cartao_final"])
        # se for cartao de credito e tiver apelido cadastrado, o apelido e mais informativo
        if c["tipo"] == "CREDIT" and r["numero_cartao_final"] and apelido:
            return c["selo"], apelido, f'{c["label"]} - {nome_cartao_curto(r["numero_cartao_final"])}'
        return c["selo"], c["label_curto"], c["label"]

    lancamentos = []
    for r in rows:
        selo, curto, completo = origem_de(r)
        lancamentos.append({
            "data": (r["data_transacao"] - timedelta(hours=3)).strftime("%d/%m/%Y"),
            "descricao": r["descricao"],
            "origem_selo": selo,
            "origem": curto,
            "origem_completa": completo,
            "cartao": nome_cartao_curto(r["numero_cartao_final"]),
            "categoria": cat_pt_puro(r["categoria"]),
            "valor": float(r["valor"] or 0),
        })
    return jsonify({"lancamentos": lancamentos, "total": len(lancamentos)})


@bp.route("/investimentos")
@requer("relatorios")
def investimentos_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            "SELECT investimento_id, nome, tipo, subtipo, instituicao, saldo, valor_bruto, "
            "valor_aplicado, impostos, taxa, tipo_taxa, data_posicao, data_vencimento, "
            "data_aplicacao, status "
            "FROM cartao.investimento ORDER BY saldo DESC NULLS LAST;"
        )
        posicoes = cur.fetchall()
    except Exception:
        conn.rollback()
        posicoes = None

    historico = []
    if posicoes is not None:
        # rendimento de cada mes = variacao do saldo total, descontando aportes e resgates
        cur.execute(
            "SELECT to_char(data, 'YYYY-MM') AS mes, MAX(data) AS ultima, "
            "SUM(saldo) AS saldo, SUM(valor_aplicado) AS aplicado "
            "FROM cartao.investimento_saldo "
            "WHERE data = (SELECT MAX(d2.data) FROM cartao.investimento_saldo d2 "
            "              WHERE to_char(d2.data,'YYYY-MM') = to_char(cartao.investimento_saldo.data,'YYYY-MM')) "
            "GROUP BY 1 ORDER BY 1;"
        )
        historico = cur.fetchall()

    cur.close()
    conn.close()

    contexto = {
        "titulo": "Investimentos",
        "topbar": topbar_html("Investimentos", "investimentos"),
    }
    if posicoes is None:
        return render_template("investimentos.html", sincronizado=False, **contexto)

    def _dt(v):
        return v.strftime("%d/%m/%Y") if v else "-"

    brutos = [p for p in posicoes if float(p["saldo"] or 0) > 0]
    ativos = []
    for p in brutos:
        aplicado = float(p["valor_aplicado"] or 0)
        bruto = float(p["valor_bruto"] or 0)
        rend = bruto - aplicado
        taxa = ""
        if p["taxa"] and float(p["taxa"]) > 0:
            taxa = f'{float(p["taxa"]):g}% {p["tipo_taxa"] or ""}'.strip()
        detalhe = p["subtipo"] or p["tipo"] or ""
        if taxa:
            detalhe = f"{detalhe} · {taxa}" if detalhe else taxa
        ativos.append({
            "nome": (p["nome"] or "-")[:46],
            "detalhe": detalhe,
            "aplicado": aplicado,
            "bruto": bruto,
            "rend": rend,
            "pct": (rend / aplicado * 100) if aplicado else 0,
            "impostos": float(p["impostos"] or 0),
            "saldo": float(p["saldo"] or 0),
            "vencimento": _dt(p["data_vencimento"]),
        })

    aplicado_total = sum(a["aplicado"] for a in ativos)
    bruto_total = sum(a["bruto"] for a in ativos)
    rendimento_bruto = bruto_total - aplicado_total

    # historico do mais recente para o mais antigo; a variacao de cada mes e a
    # diferenca para o mes seguinte (o de cima na tabela)
    hist = []
    anterior = None
    for h in reversed(historico):
        saldo = float(h["saldo"] or 0)
        mes = h["mes"]
        hist.append({
            "rotulo": f"{MESES_ABREV[int(mes[5:7]) - 1]}/{mes[2:4]}",
            "aplicado": float(h["aplicado"] or 0),
            "saldo": saldo,
            "variacao": None if anterior is None else anterior - saldo,
        })
        anterior = saldo

    return render_template(
        "investimentos.html",
        sincronizado=True,
        ativos=ativos,
        encerrados=len(posicoes) - len(ativos),
        saldo_total=sum(a["saldo"] for a in ativos),
        aplicado_total=aplicado_total,
        rendimento_bruto=rendimento_bruto,
        rend_pct=(rendimento_bruto / aplicado_total * 100) if aplicado_total else 0,
        ir_total=sum(a["impostos"] for a in ativos),
        historico=hist,
        **contexto,
    )


@bp.route("/api/categoria-lancamentos")
@requer("cadastros")
def api_categoria_lancamentos():
    """Lista os lancamentos de uma categoria - usado pelo botao 'protegida' em /categorias,
    pra mostrar o que esta impedindo a remocao sem precisar ir pra tela de Lancamentos."""
    categoria = request.args.get("categoria") or ""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT t.transacao_id, t.data_transacao, t.descricao, "
        "COALESCE(t.valor_brl, t.valor_original) AS valor "
        "FROM cartao.transacao t WHERE t.categoria = %s ORDER BY t.data_transacao DESC LIMIT 300;",
        (categoria,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([
        {
            # o id vai junto para o modal poder abrir os detalhes do lancamento
            "transacao_id": str(r["transacao_id"]),
            "data": (r["data_transacao"] - timedelta(hours=3)).strftime("%d/%m/%Y") if r["data_transacao"] else "-",
            "descricao": r["descricao"] or "-",
            "valor": float(r["valor"]) if r["valor"] is not None else 0,
        }
        for r in rows
    ])
