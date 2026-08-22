"""Categorias, centro de custos, dimensoes, regras, contas e pendencias."""
from datetime import datetime

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, render_template

from core import (
    CATEGORIAS_EXTRA,
    CATEGORIAS_NEUTRAS_PADRAO,
    CATEGORIAS_OCULTAS,
    CATEGORIA_PT,
    CATEGORIA_PT_DB,
    JOIN_NATUREZA,
    NATUREZAS,
    NATUREZAS_NEUTRAS,
    NATUREZA_PADRAO,
    NATUREZA_SQL,
    VAL_DESPESA,
    aplicar_regras,
    cat_pt,
    cat_pt_puro,
    categoria_com_nome,
    chave_alfa,
    detectar_banco,
    esc,
    get_conn,
    levantar_pendencias,
    recarregar_categorias_db,
    pode,
    requer,
    selo_banco_html,
    topbar_html,
)

bp = Blueprint("cadastros", __name__)


@bp.route("/dimensoes", methods=["GET", "POST"])
@requer("cadastros")
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
                    erro = f"Já existe uma dimensão chamada '{nome}'."
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
                    erro = f"Já existe o valor '{nome}' nessa dimensão."
        elif acao == "editar_valor":
            def to_num(v):
                v = (v or "").strip().replace(",", ".")
                return float(v) if v else None
            cur.execute(
                "UPDATE cartao.dimensao_valor SET nome=%s, teto_mensal=%s, teto_anual=%s WHERE id=%s;",
                (
                    (request.form.get("nome") or "").strip(),
                    to_num(request.form.get("teto_mensal")),
                    to_num(request.form.get("teto_anual")),
                    request.form.get("valor_id"),
                ),
            )
            conn.commit()
        elif acao == "excluir_valor":
            cur.execute("DELETE FROM cartao.dimensao_valor WHERE id=%s;", (request.form.get("valor_id"),))
            conn.commit()

    cur.execute("SELECT id, nome, obrigatoria, ordem FROM cartao.dimensao ORDER BY ordem, nome;")
    dims = cur.fetchall()
    cur.execute("SELECT id, dimensao_id, nome, teto_mensal, teto_anual FROM cartao.dimensao_valor ORDER BY nome;")
    valores_db = cur.fetchall()

    # gasto do mes e do ano corrente por valor de dimensao, pra comparar com o teto
    mes_atual = datetime.now().strftime("%Y-%m")
    ano_atual = datetime.now().strftime("%Y")
    cur.execute(
        "SELECT td.valor_id, "
        f"SUM(CASE WHEN to_char(t.data_transacao,'YYYY-MM') = %s THEN {VAL_DESPESA} ELSE 0 END) AS gasto_mes, "
        f"SUM(CASE WHEN to_char(t.data_transacao,'YYYY') = %s THEN {VAL_DESPESA} ELSE 0 END) AS gasto_ano "
        f"FROM cartao.transacao_dimensao td "
        f"JOIN cartao.transacao t ON t.transacao_id::text = td.transacao_id {JOIN_NATUREZA} "
        f"WHERE {NATUREZA_SQL} = 'despesa' AND COALESCE(t.duplicada, false) = false "
        "GROUP BY td.valor_id;",
        (mes_atual, ano_atual),
    )
    gasto_por_valor = {r["valor_id"]: r for r in cur.fetchall()}
    cur.close()
    conn.close()

    valores_por_dim = {}
    for v in valores_db:
        valores_por_dim.setdefault(v["dimensao_id"], []).append(v)

    # gasto ja somado por valor de dimensao, pro template so exibir
    gastos = {
        vid: {"mes": float(g["gasto_mes"] or 0), "ano": float(g["gasto_ano"] or 0)}
        for vid, g in gasto_por_valor.items()
    }

    return render_template(
        "dimensoes.html",
        titulo="Gerenciar Dimensões",
        topbar=topbar_html("Gerenciar Dimensões", "dimensoes"),
        erro=erro,
        dimensoes=dims,
        valores_por_dim=valores_por_dim,
        gastos=gastos,
    )


@bp.route("/regras", methods=["GET", "POST"])
@requer("cadastros")
def regras_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    erro = None
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "criar_regra":
            padrao = (request.form.get("padrao") or "").strip()
            categoria = request.form.get("categoria") or ""
            cur.execute("SELECT id FROM cartao.regra_classificacao WHERE lower(padrao) = lower(%s);", (padrao,))
            repetida = cur.fetchone() if padrao else None
            if not padrao:
                erro = "Informe o texto/padrao a procurar na descricao."
            elif repetida:
                # duas regras com o mesmo texto: a segunda nunca decide nada,
                # so confunde quem tenta entender por que um lancamento foi parar
                # numa categoria
                erro = f'Já existe uma regra para o texto "{padrao}".'
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
        (set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_NEUTRAS_PADRAO - CATEGORIAS_OCULTAS,
        key=lambda c: chave_alfa(cat_pt(c)),
    )

    cur.close()
    conn.close()

    categorias = [{"chave": c, "nome": cat_pt_puro(c)} for c in todas_categorias]

    editar_id = request.args.get("editar")
    try:
        editar_id = int(editar_id) if editar_id else None
    except ValueError:
        editar_id = None

    regras = []
    for r in regras_db:
        selecionadas = dim_por_regra.get(r["id"], {})
        dims_txt = []
        for d in dimensoes:
            vid = selecionadas.get(d["id"])
            if vid:
                nome_valor = next(
                    (v["nome"] for v in valores_por_dim.get(d["id"], []) if v["id"] == vid), "?"
                )
                dims_txt.append(f'{d["nome"]}: {nome_valor}')
        regras.append({
            "id": r["id"],
            "padrao": r["padrao"],
            "categoria": r["categoria"],
            "categoria_nome": cat_pt_puro(r["categoria"]),
            "dims_txt": ", ".join(dims_txt) or "-",
            "dims_selecionadas": selecionadas,
        })

    return render_template(
        "regras.html",
        titulo="Regras Automáticas",
        topbar=topbar_html("Regras Automáticas", "regras"),
        erro=erro,
        regras=regras,
        categorias=categorias,
        dimensoes=dimensoes,
        valores_por_dim=valores_por_dim,
        total_aplicadas=total_aplicadas,
        editar_id=editar_id,
    )


@bp.route("/grupos", methods=["GET", "POST"])
@requer("cadastros")
def grupos_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    erro = None
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "criar_grupo":
            nome = request.form.get("nome", "").strip()
            try:
                cur.execute("INSERT INTO cartao.grupo_custo (nome) VALUES (%s);", (nome,))
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                erro = f"Já existe um centro de custo chamado '{nome}'."
        elif acao == "editar_grupo":
            try:
                cur.execute(
                    "UPDATE cartao.grupo_custo SET nome=%s WHERE id=%s;",
                    (request.form.get("nome", "").strip(), request.form.get("grupo_id")),
                )
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                erro = "Já existe um centro de custo com esse nome."
        elif acao == "excluir_grupo":
            cur.execute("DELETE FROM cartao.grupo_custo WHERE id=%s;", (request.form.get("grupo_id"),))
            conn.commit()
        elif acao == "criar_subgrupo":
            nome = request.form.get("nome", "").strip()
            try:
                cur.execute(
                    "INSERT INTO cartao.subgrupo_custo (grupo_id, nome) VALUES (%s,%s);",
                    (request.form.get("grupo_id"), nome),
                )
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                erro = f"Já existe um subgrupo chamado '{nome}' nesse centro de custo."
        elif acao == "editar_subgrupo":
            try:
                cur.execute(
                    "UPDATE cartao.subgrupo_custo SET nome=%s WHERE id=%s;",
                    (request.form.get("nome", "").strip(), request.form.get("subgrupo_id")),
                )
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                erro = "Já existe um subgrupo com esse nome nesse centro de custo."
        elif acao == "excluir_subgrupo":
            cur.execute("DELETE FROM cartao.subgrupo_custo WHERE id=%s;", (request.form.get("subgrupo_id"),))
            conn.commit()
        elif acao == "mapear_categoria":
            subgrupo_id = request.form.get("subgrupo_id") or None
            categoria = request.form.get("categoria")
            # a categoria so cabe em um subgrupo (ela e a chave primaria da tabela),
            # entao vincular uma ja vinculada MOVE. Antes isso acontecia sem dizer
            # nada e a categoria sumia do subgrupo antigo.
            cur.execute(
                "SELECT s.nome AS subgrupo, g.nome AS grupo FROM cartao.categoria_subgrupo cs "
                "JOIN cartao.subgrupo_custo s ON s.id = cs.subgrupo_id "
                "JOIN cartao.grupo_custo g ON g.id = s.grupo_id "
                "WHERE cs.categoria = %s;",
                (categoria,),
            )
            antes = cur.fetchone()
            cur.execute(
                "INSERT INTO cartao.categoria_subgrupo (categoria, subgrupo_id) VALUES (%s,%s) "
                "ON CONFLICT (categoria) DO UPDATE SET subgrupo_id = EXCLUDED.subgrupo_id;",
                (categoria, subgrupo_id),
            )
            conn.commit()
            if antes:
                aviso = (
                    f'"{cat_pt_puro(categoria)}" foi movida de '
                    f'{antes["grupo"]} › {antes["subgrupo"]} para cá — '
                    "uma categoria pertence a um centro de custo por vez."
                )
            else:
                aviso = f'"{cat_pt_puro(categoria)}" vinculada.'

    cur.execute("SELECT id, nome FROM cartao.grupo_custo;")
    grupos_db = sorted(cur.fetchall(), key=lambda g: chave_alfa(g["nome"]))
    cur.execute("SELECT id, grupo_id, nome FROM cartao.subgrupo_custo;")
    subgrupos_db = sorted(cur.fetchall(), key=lambda s: chave_alfa(s["nome"]))
    cur.execute("SELECT categoria, subgrupo_id FROM cartao.categoria_subgrupo;")
    mapa_categoria = {r["categoria"]: r["subgrupo_id"] for r in cur.fetchall()}
    nome_subgrupo = {
        s["id"]: f'{next((g["nome"] for g in grupos_db if g["id"] == s["grupo_id"]), "")} › {s["nome"]}'
        for s in subgrupos_db
    }
    cur.close()
    conn.close()

    subgrupos_por_grupo = {}
    for s in subgrupos_db:
        subgrupos_por_grupo.setdefault(s["grupo_id"], []).append(s)

    todas_categorias = sorted(
        (set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_NEUTRAS_PADRAO - CATEGORIAS_OCULTAS,
        key=lambda c: chave_alfa(cat_pt_puro(c)),
    )

    # cada categoria vira {chave, nome, subgrupo_id} - o template filtra por
    # subgrupo_id pra montar os chips e o dropdown de vincular
    categorias = [
        {
            "chave": c,
            "nome": cat_pt_puro(c),
            "subgrupo_id": mapa_categoria.get(c),
            # usado no dropdown para dizer de onde a categoria sairia
            "subgrupo_nome": nome_subgrupo.get(mapa_categoria.get(c), ""),
        }
        for c in todas_categorias
    ]
    categorias_por_subgrupo = {}
    for c in categorias:
        if c["subgrupo_id"]:
            categorias_por_subgrupo.setdefault(c["subgrupo_id"], []).append(c)
    sem_vinculo = [c for c in categorias if not c["subgrupo_id"]]

    return render_template(
        "grupos.html",
        titulo="Centro de Custos",
        topbar=topbar_html("Centro de Custos", "grupos"),
        erro=erro,
        aviso=aviso,
        grupos=grupos_db,
        subgrupos_por_grupo=subgrupos_por_grupo,
        categorias=categorias,
        categorias_por_subgrupo=categorias_por_subgrupo,
        sem_vinculo=sem_vinculo,
    )


@bp.route("/contas", methods=["GET", "POST"])
@requer("cadastros")
def contas_view():
    """Configuracoes de Contas / Cartao - centraliza tudo que descreve a origem do
    dinheiro: de quem e a conexao bancaria, e o apelido de cada cartao (fisico,
    virtual, adicional). As datas de fatura vem do Pluggy e sao so leitura."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aviso = erro = None

    if request.method == "POST":
        acao = request.form.get("acao") or "titular"
        try:
            if acao == "salvar_cartao":
                final4 = (request.form.get("final4") or "").strip()
                prefixo = (request.form.get("prefixo") or "").strip()
                if not (final4.isdigit() and len(final4) == 4):
                    erro = "Os 4 últimos dígitos devem ser exatamente 4 números."
                elif not prefixo:
                    # nome em branco = remover o apelido, volta a aparecer como "final NNNN"
                    cur.execute("DELETE FROM cartao.cartao_nome WHERE final4 = %s;", (final4,))
                    conn.commit()
                    aviso = f"Nome do cartão final {final4} removido."
                else:
                    cur.execute(
                        "SELECT final4 FROM cartao.cartao_nome WHERE lower(prefixo) = lower(%s) "
                        "AND final4 <> %s;",
                        (prefixo, final4),
                    )
                    ja_usado = cur.fetchone()
                    if ja_usado:
                        # dois cartoes com o mesmo apelido ficam indistinguiveis na
                        # coluna Origem e no filtro
                        erro = (
                            f'O nome "{prefixo}" já é usado pelo cartão final '
                            f'{ja_usado["final4"]}.'
                        )
                    else:
                        cur.execute(
                            "INSERT INTO cartao.cartao_nome (final4, prefixo) VALUES (%s,%s) "
                            "ON CONFLICT (final4) DO UPDATE SET prefixo = EXCLUDED.prefixo;",
                            (final4, prefixo),
                        )
                        conn.commit()
                        aviso = f'Cartão final {final4} salvo como "{prefixo}".'
            else:
                item_id = request.form.get("item_id")
                titular = (request.form.get("titular") or "").strip()
                if not titular:
                    cur.execute("DELETE FROM cartao.item_titular WHERE item_id = %s;", (item_id,))
                    aviso = "Titular removido dessa conexão."
                else:
                    cur.execute(
                        "INSERT INTO cartao.item_titular (item_id, titular) VALUES (%s,%s) "
                        "ON CONFLICT (item_id) DO UPDATE SET titular = EXCLUDED.titular;",
                        (item_id, titular),
                    )
                    aviso = f'Titular salvo: "{titular}".'
                conn.commit()
        except Exception as e:
            conn.rollback()
            erro = str(e)

    cur.execute(
        "SELECT c.item_id, c.account_id, c.tipo, c.nome, c.numero_final, "
        "c.fechamento_fatura, c.vencimento_fatura, p.connector_name, it.titular "
        "FROM cartao.conta c JOIN cartao.pluggy_item p ON p.item_id = c.item_id "
        "LEFT JOIN cartao.item_titular it ON it.item_id = c.item_id "
        "ORDER BY p.connector_name, c.tipo;"
    )
    linhas = cur.fetchall()

    # quais cartoes (finais) pertencem a cada conta - vem dos proprios lancamentos,
    # que e o unico lugar onde o cartao adicional aparece. A conta traz so o final
    # do cartao principal; os adicionais so existem nas transacoes.
    cur.execute(
        "SELECT DISTINCT account_id::text AS account_id, numero_cartao_final "
        "FROM cartao.transacao WHERE numero_cartao_final IS NOT NULL;"
    )
    finais_por_conta = {}
    for r in cur.fetchall():
        finais_por_conta.setdefault(r["account_id"], set()).add(r["numero_cartao_final"])

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome ORDER BY prefixo;")
    cartoes_nome = cur.fetchall()
    cur.close()
    conn.close()

    nomes_cartao = {c["final4"]: c["prefixo"] for c in cartoes_nome}

    conexoes = {}
    for r in linhas:
        item_id = str(r["item_id"])
        banco = detectar_banco(r["nome"], r["connector_name"])
        info = conexoes.setdefault(
            item_id, {"banco": banco, "titular": r["titular"], "contas": [], "credito": []}
        )
        tipo_pt = {"CREDIT": "Cartão de crédito", "BANK": "Conta corrente",
                   "MANUAL": "Dinheiro (manual)"}.get(r["tipo"], r["tipo"])
        info["contas"].append(tipo_pt)
        if r["tipo"] == "CREDIT":
            info["credito"].append(r)

    # finais ja usados por alguma conta - o que sobra e cartao cadastrado a mao
    usados = set()
    for r in linhas:
        if r["tipo"] == "CREDIT":
            usados |= finais_por_conta.get(str(r["account_id"]), set())
            if r["numero_final"]:
                usados.add(r["numero_final"])
    avulsos = [c for c in cartoes_nome if c["final4"] not in usados]

    # prepara os dados prontos pro template: dia da fatura ja extraido e a lista de
    # cartoes de cada conta ja resolvida (principal + adicionais vistos nos lancamentos)
    for info in conexoes.values():
        info["selo"] = selo_banco_html(info["banco"])
        info["contas_txt"] = ", ".join(dict.fromkeys(info["contas"]))
        for c in info["credito"]:
            finais = set(finais_por_conta.get(str(c["account_id"]), set()))
            if c["numero_final"]:
                finais.add(c["numero_final"])
            c["finais"] = sorted(finais)
            c["dia_fechamento"] = c["fechamento_fatura"].day if c["fechamento_fatura"] else None
            c["dia_vencimento"] = c["vencimento_fatura"].day if c["vencimento_fatura"] else None

    return render_template(
        "contas.html",
        titulo="Configurações de Contas / Cartão",
        topbar=topbar_html("Configurações de Contas / Cartão", "contas"),
        aviso=aviso,
        erro=erro,
        conexoes=conexoes,
        nomes_cartao=nomes_cartao,
        avulsos=avulsos,
        sugestoes=["Ronaldo", "Andrea", "Ronaldo e Andrea", "Compartilhado"],
    )


@bp.route("/pendencias", methods=["GET", "POST"])
@requer("cadastros")
def pendencias_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aviso = None

    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "definir_natureza":
            categoria = request.form.get("categoria")
            natureza = request.form.get("natureza")
            if categoria and natureza in NATUREZAS:
                cur.execute(
                    "INSERT INTO cartao.categoria_natureza (categoria, natureza) VALUES (%s,%s) "
                    "ON CONFLICT (categoria) DO UPDATE SET natureza = EXCLUDED.natureza;",
                    (categoria, natureza),
                )
                conn.commit()
                aviso = f'Natureza de "{cat_pt_puro(categoria)}" definida como {NATUREZAS[natureza]}.'
        elif acao == "vincular_centro":
            categoria = request.form.get("categoria")
            subgrupo_id = request.form.get("subgrupo_id") or None
            if categoria and subgrupo_id:
                cur.execute(
                    "INSERT INTO cartao.categoria_subgrupo (categoria, subgrupo_id) VALUES (%s,%s) "
                    "ON CONFLICT (categoria) DO UPDATE SET subgrupo_id = EXCLUDED.subgrupo_id;",
                    (categoria, subgrupo_id),
                )
                conn.commit()
                aviso = f'"{cat_pt_puro(categoria)}" vinculada ao centro de custo.'
        elif acao == "limpar_natureza":
            # volta o lancamento a seguir a natureza da categoria dele
            transacao_id = request.form.get("transacao_id")
            if transacao_id:
                cur.execute(
                    "UPDATE cartao.transacao SET natureza = NULL WHERE transacao_id = %s;",
                    (transacao_id,),
                )
                conn.commit()
                aviso = "Lançamento voltou a seguir a natureza da categoria."
        elif acao == "ocultar":
            categoria = request.form.get("categoria")
            if categoria:
                cur.execute(
                    "INSERT INTO cartao.categoria_oculta (categoria) VALUES (%s) ON CONFLICT DO NOTHING;",
                    (categoria,),
                )
                conn.commit()
                recarregar_categorias_db()
                aviso = f'"{cat_pt_puro(categoria)}" ocultada — não aparece mais nas listas.'

    pend = levantar_pendencias(cur)

    cur.execute("SELECT id, nome FROM cartao.grupo_custo;")
    grupos_db = sorted(cur.fetchall(), key=lambda g: chave_alfa(g["nome"]))
    cur.execute("SELECT id, grupo_id, nome FROM cartao.subgrupo_custo;")
    subgrupos_db = sorted(cur.fetchall(), key=lambda s: chave_alfa(s["nome"]))
    cur.close()
    conn.close()

    subgrupos_por_grupo = {}
    for s in subgrupos_db:
        subgrupos_por_grupo.setdefault(s["grupo_id"], []).append(s)

    # Primeira tela migrada para Jinja (fase 3 da refatoracao). Repare que aqui nao
    # ha nenhuma chamada a esc(): o proprio Jinja escapa todo {{ ... }}, entao nome
    # de categoria com HTML dentro sai como texto sem ninguem precisar lembrar.
    return render_template(
        "pendencias.html",
        titulo="Pendências de classificação",
        topbar=topbar_html("Pendências de classificação", "pendencias"),
        aviso=aviso,
        pend=pend,
        grupos=grupos_db,
        subgrupos_por_grupo=subgrupos_por_grupo,
        naturezas=NATUREZAS,
        natureza_padrao=NATUREZA_PADRAO,
        nome_categoria=cat_pt_puro,
    )


@bp.route("/categorias", methods=["GET", "POST"])
@requer("cadastros")
def categorias_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aviso = erro = None

    def contar_uso(categoria):
        cur.execute("SELECT COUNT(*) AS n FROM cartao.transacao WHERE categoria = %s;", (categoria,))
        return cur.fetchone()["n"]

    if request.method == "POST":
        acao = request.form.get("acao")
        try:
            if acao == "natureza":
                categoria = request.form.get("categoria")
                natureza = request.form.get("natureza")
                if categoria and natureza in NATUREZAS:
                    cur.execute(
                        "INSERT INTO cartao.categoria_natureza (categoria, natureza) VALUES (%s,%s) "
                        "ON CONFLICT (categoria) DO UPDATE SET natureza = EXCLUDED.natureza;",
                        (categoria, natureza),
                    )
                    conn.commit()

            elif acao == "criar":
                nome = (request.form.get("nome") or "").strip()
                if not nome:
                    erro = "Informe o nome da categoria."
                elif categoria_com_nome(nome):
                    erro = f'Já existe uma categoria chamada "{nome}".'
                else:
                    cur.execute(
                        "INSERT INTO cartao.categoria (categoria, nome_pt) VALUES (%s,%s) "
                        "ON CONFLICT (categoria) DO UPDATE SET nome_pt = EXCLUDED.nome_pt;",
                        (nome, nome),
                    )
                    cur.execute("DELETE FROM cartao.categoria_oculta WHERE categoria = %s;", (nome,))
                    conn.commit()
                    aviso = f'Categoria "{nome}" criada.'

            elif acao == "renomear":
                categoria = request.form.get("categoria") or ""
                novo_nome = (request.form.get("novo_nome") or "").strip()
                conflito = categoria_com_nome(novo_nome, exceto=categoria)
                if not novo_nome:
                    erro = "Informe o novo nome."
                elif conflito:
                    # sem isto, duas categorias diferentes ficam com o mesmo nome na
                    # tela e o relatorio passa a mostrar linhas repetidas
                    erro = (
                        f'Já existe uma categoria chamada "{novo_nome}" '
                        f'(a categoria "{conflito}"). Para juntar as duas, use '
                        '"Mover lançamentos" e depois remova a que ficar vazia.'
                    )
                else:
                    cur.execute(
                        "INSERT INTO cartao.categoria (categoria, nome_pt) VALUES (%s,%s) "
                        "ON CONFLICT (categoria) DO UPDATE SET nome_pt = EXCLUDED.nome_pt;",
                        (categoria, novo_nome),
                    )
                    conn.commit()
                    aviso = f'Categoria renomeada para "{novo_nome}".'

            elif acao == "mover":
                origem = request.form.get("origem") or ""
                destino = request.form.get("destino") or ""
                if not origem or not destino:
                    erro = "Escolha a categoria de origem e a de destino."
                elif origem == destino:
                    erro = "Escolha categorias diferentes para mover."
                else:
                    cur.execute(
                        "UPDATE cartao.transacao SET categoria = %s WHERE categoria = %s;",
                        (destino, origem),
                    )
                    qtd = cur.rowcount
                    conn.commit()
                    aviso = f'{qtd} lançamento(s) movido(s) de "{cat_pt_puro(origem)}" para "{cat_pt_puro(destino)}".'

            elif acao == "excluir":
                categoria = request.form.get("categoria") or ""
                qtd = contar_uso(categoria)
                if qtd > 0:
                    erro = f'Não é possível remover: existem {qtd} lançamento(s) nessa categoria. Mova-os primeiro.'
                else:
                    cur.execute("DELETE FROM cartao.categoria WHERE categoria = %s;", (categoria,))
                    cur.execute("DELETE FROM cartao.categoria_natureza WHERE categoria = %s;", (categoria,))
                    cur.execute("DELETE FROM cartao.categoria_subgrupo WHERE categoria = %s;", (categoria,))
                    cur.execute("INSERT INTO cartao.categoria_oculta (categoria) VALUES (%s) ON CONFLICT DO NOTHING;", (categoria,))
                    conn.commit()
                    aviso = f'Categoria "{cat_pt_puro(categoria)}" removida.'
        except psycopg2.errors.UniqueViolation:
            # rede de baixo: a validacao da tela ja barra nome repetido, mas se
            # algo passar, o indice do banco impede - e o usuario merece uma
            # mensagem em portugues, nao o erro cru do Postgres
            conn.rollback()
            erro = "Já existe uma categoria com esse nome."
        except Exception as e:
            conn.rollback()
            erro = str(e)
        recarregar_categorias_db()

    cur.execute(
        f"SELECT t.categoria, COUNT(*) AS qtd, SUM({VAL_DESPESA}) AS total "
        f"FROM cartao.transacao t JOIN cartao.conta c ON c.account_id = t.account_id "
        "WHERE t.categoria IS NOT NULL "
        "GROUP BY t.categoria;"
    )
    usadas = {r["categoria"]: r for r in cur.fetchall()}

    cur.execute("SELECT categoria, natureza FROM cartao.categoria_natureza;")
    naturezas_atuais = {r["categoria"]: r["natureza"] for r in cur.fetchall()}
    cur.close()
    conn.close()

    todas = sorted(
        (set(usadas) | set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_OCULTAS,
        key=lambda c: chave_alfa(cat_pt_puro(c)),
    )

    # uma lista de dicts prontos, pro template so exibir. 'chave' e o identificador
    # que vai nos forms; 'nome' e o rotulo traduzido que o usuario le.
    categorias = []
    for c in todas:
        info = usadas.get(c)
        categorias.append({
            "chave": c,
            "nome": cat_pt_puro(c),
            "qtd": info["qtd"] if info else 0,
            "total": float(info["total"] or 0) if info else 0.0,
            "natureza": naturezas_atuais.get(c, NATUREZA_PADRAO),
        })

    # Nomes iguais apontando para categorias diferentes: a renomeacao e so um
    # apelido, a chave do Pluggy continua distinta. Isso vira linha duplicada no
    # relatorio, vinculo separado no centro de custo e - o mais grave - naturezas
    # que podem divergir sem ninguem ver, porque na tela sao "a mesma" categoria.
    por_nome = {}
    for c in categorias:
        por_nome.setdefault(c["nome"], []).append(c)
    duplicadas = [
        {"nome": nome, "itens": itens,
         "naturezas_divergem": len({i["natureza"] for i in itens}) > 1}
        for nome, itens in sorted(por_nome.items()) if len(itens) > 1
    ]

    return render_template(
        "categorias.html",
        duplicadas=duplicadas,
        titulo="Gerenciar categorias",
        topbar=topbar_html("Gerenciar categorias", "categorias"),
        aviso=aviso,
        erro=erro,
        categorias=categorias,
        naturezas=NATUREZAS,
        naturezas_neutras=NATUREZAS_NEUTRAS,
        # o modal de detalhes deixa trocar a categoria do lancamento; quem so tem
        # 'cadastros' e nao 'lancamentos_editar' ve o seletor travado (a API recusaria)
        pode_editar=pode("lancamentos_editar"),
    )
