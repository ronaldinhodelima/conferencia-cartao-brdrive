import os
import csv
import functools
import io
import json
import re
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Flask, request, redirect, session, jsonify

app = Flask(__name__)

# URL do serviço bussola-financeira-app que faz a sincronizacao com o Pluggy.
# Pode ser sobrescrita via env var caso o dominio mude.
BUSSOLA_SYNC_URL = os.environ.get(
    "BUSSOLA_SYNC_URL", "https://o9eitr54t888rej2cjjpak8y.coolify.brdrive.net/sync"
)
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

# mantido apenas por compatibilidade com bases antigas; a regra de gasto real
# passou a ser a natureza da categoria (ver NATUREZAS mais abaixo)
CATEGORIAS_NAO_GASTO = ("Credit card payment", "Transfer - Internal")

# categorias extras disponiveis no dropdown mesmo que ainda nao tenham sido usadas em nenhuma transacao
CATEGORIAS_EXTRA = (
    "BRDrive", "Agua / Gas", "Natacao", "Academia", "Viagem",
    "Imóveis / Terrenos", "Veículos / Bens",
)

# ---------------------------------------------------------------------------
# Natureza de cada categoria - base do DRE.
#
# O DRE mede o RESULTADO do periodo: Receitas - Despesas. Nem todo dinheiro que
# sai e despesa: comprar um terreno ou aplicar dinheiro nao empobrece ninguem,
# apenas troca a forma do patrimonio, e por isso NAO entra no resultado (vai
# para o balanco patrimonial). O mesmo vale para pagar a fatura do cartao, que
# so move dinheiro da conta para o cartao - a despesa ja foi contada na compra.
#
#   receita       - entra e aumenta o patrimonio (salario, PIX recebido)
#   despesa       - sai e reduz o patrimonio (consumo, juros, tarifas)
#   investimento  - troca dinheiro por aplicacao financeira (neutro no resultado)
#   bem           - troca dinheiro por bem: terreno, veiculo, imovel (neutro)
#   transferencia - so troca de bolso: fatura, conta propria (neutro)
#   fluxo         - a direcao decide: entrada = receita, saida = despesa
# ---------------------------------------------------------------------------
NATUREZAS = {
    "receita": "Receita",
    "despesa": "Despesa",
    "investimento": "Investimento",
    "bem": "Aquisição de bem",
    "transferencia": "Transferência",
    "fluxo": "Depende da direção",
}
NATUREZA_PADRAO = "despesa"

# naturezas que nao afetam o resultado do periodo
NATUREZAS_NEUTRAS = ("investimento", "bem", "transferencia")

SEED_NATUREZAS = {
    # so movem dinheiro de lugar - nunca sao despesa
    "Credit card payment": "transferencia",
    "Transfer - Internal": "transferencia",
    "Same person transfer": "transferencia",
    "Same person transfer - CASH": "transferencia",
    "Same person transfer - PIX": "transferencia",
    "Same person transfer - TED": "transferencia",
    "Same person transfer - DOC": "transferencia",
    "Same person transfer - Bank Slip": "transferencia",
    # na base do Ronaldo isto veio do Pluggy como financiamento, mas e o
    # pagamento da fatura do cartao de marco/2026 (bate com o valor da fatura)
    "Loans and financing": "transferencia",

    # poupanca de longo prazo - sai do resultado, entra no bloco de investimentos
    "Investments": "investimento",
    "Automatic investment": "investimento",
    "Pension": "investimento",
    "Fixed income": "investimento",
    "Variable income": "investimento",
    "Savings": "investimento",

    # aquisicao de bem - nao e despesa, e troca de ativo
    "Imóveis / Terrenos": "bem",
    "Veículos / Bens": "bem",

    # a direcao define: o que entra e receita, o que sai e despesa
    "Transfer - PIX": "fluxo",
    "Transfer - TED": "fluxo",
    "Transfer - DOC": "fluxo",
    "Transfer - Bank Slip": "fluxo",
    "Transfer - Cash": "fluxo",
    "Transfers": "fluxo",
    "Third party transfers": "fluxo",

    # entradas
    "Income": "receita",
    "Salary": "receita",
    "Government aid": "receita",
    "Interest income": "receita",
    "Dividends": "receita",

    # custo financeiro real: dinheiro que saiu de fato
    "Interests charged": "despesa",
    "Credit card fees": "despesa",
    "Tax on financial operations": "despesa",
}

# categorias que, por padrao, nao entram em centro de custo (nao sao despesa)
CATEGORIAS_NEUTRAS_PADRAO = {
    c for c, n in SEED_NATUREZAS.items() if n in NATUREZAS_NEUTRAS
}

# dia de fechamento da fatura (fixo, informado pelo usuario - Pluggy nao sincroniza esse dado)
FATURA_DIA_FECHAMENTO = 12

# conta sintetica usada para lancamentos manuais (dinheiro em especie), fora do Pluggy
CONTA_MANUAL_ID = "00000000-0000-0000-0000-000000000002"

APP_NOME = "Meu Dinheiro"

MESES_ABREV = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")

# ---- expressoes SQL reutilizaveis (exigem JOIN com cartao.conta c e LEFT JOIN categoria_natureza n)
JOIN_NATUREZA = (
    " JOIN cartao.conta c ON c.account_id = t.account_id "
    " LEFT JOIN cartao.categoria_natureza n ON n.categoria = t.categoria "
)

# valor com o sinal do ponto de vista de DESPESA (positivo = dinheiro saiu).
# No cartao a compra vem positiva; na conta corrente a saida vem negativa.
VAL_DESPESA = (
    "(CASE WHEN c.tipo = 'CREDIT' THEN COALESCE(t.valor_brl, t.valor_original) "
    "ELSE -COALESCE(t.valor_brl, t.valor_original) END)"
)

# natureza efetiva de um lancamento, na ordem de prioridade:
#   1. natureza definida no proprio lancamento (ex: um PIX que foi compra de terreno)
#   2. natureza da categoria
#   3. despesa (padrao)
# 'fluxo' e resolvido pela direcao do dinheiro.
_NAT_BASE = "COALESCE(t.natureza, n.natureza, '" + NATUREZA_PADRAO + "')"
NATUREZA_SQL = (
    "(CASE WHEN " + _NAT_BASE + " = 'fluxo' "
    "THEN (CASE WHEN " + VAL_DESPESA + " > 0 THEN 'despesa' ELSE 'receita' END) "
    "ELSE " + _NAT_BASE + " END)"
)


# nomes de banco reconhecidos dentro do nome da conta/instituicao retornado pelo Pluggy
# (o connector_name do Pluggy costuma ser generico, ex: "MeuPluggy", entao preferimos
# procurar o nome real do banco dentro do nome da conta/instituicao)
# o nome que o banco usa na razao social nem sempre e o nome comercial
# (ex: a conta do Nubank vem como "Nu Pagamentos S.A."), entao mapeamos apelidos
BANCOS_CONHECIDOS = (
    ("Nubank", ("nubank", "nu pagamentos", "nu financeira", "nu invest")),
    ("Unicred", ("unicred",)),
    ("Itaú", ("itau", "itaú")),
    ("Bradesco", ("bradesco",)),
    ("Santander", ("santander",)),
    ("Caixa", ("caixa economica", "caixa econômica")),
    ("Banco do Brasil", ("banco do brasil",)),
    ("Inter", ("banco inter", "inter s.a", "intermedium")),
    ("C6 Bank", ("c6 bank", "banco c6")),
    ("PicPay", ("picpay",)),
    ("Mercado Pago", ("mercado pago", "mercadopago")),
    ("BTG", ("btg pactual", "btg")),
    ("XP", ("xp investimentos", "banco xp")),
    ("Sicoob", ("sicoob",)),
    ("Sicredi", ("sicredi",)),
    ("Neon", ("banco neon", "neon pagamentos")),
    ("Will Bank", ("will bank", "willbank")),
    ("Original", ("banco original",)),
    ("Safra", ("safra",)),
    ("Pan", ("banco pan",)),
)


def detectar_banco(nome_conta, connector_name):
    texto = f"{nome_conta or ''} {connector_name or ''}".lower()
    for banco, apelidos in BANCOS_CONHECIDOS:
        if any(a in texto for a in apelidos):
            return banco
    return connector_name or nome_conta or "Banco"


# selo visual de cada banco: cor da marca + sigla. Feito em CSS (sem imagem
# externa) para carregar instantaneo e nao depender de arquivo de terceiros.
BANCOS_ESTILO = {
    "Nubank": ("#820ad1", "Nu"),
    "Unicred": ("#00995d", "UN"),
    "Itaú": ("#ec7000", "It"),
    "Bradesco": ("#cc092f", "Br"),
    "Santander": ("#ec0000", "Sa"),
    "Caixa": ("#0070af", "CX"),
    "Banco do Brasil": ("#f9dd16", "BB", "#1c1c1c"),
    "Inter": ("#ff7a00", "In"),
    "C6 Bank": ("#242424", "C6"),
    "PicPay": ("#11c76f", "PP"),
    "Mercado Pago": ("#00b1ea", "MP"),
    "BTG": ("#0d1b2a", "BT"),
    "XP": ("#0f0f0f", "XP"),
    "Sicoob": ("#00a94f", "Sc"),
    "Sicredi": ("#3fa110", "Si"),
    "Neon": ("#00c8f0", "Ne"),
    "Will Bank": ("#ffe600", "Wl", "#1c1c1c"),
    "Original": ("#00a868", "Or"),
    "Safra": ("#00294b", "Sf"),
    "Pan": ("#00a0df", "Pa"),
}


def selo_banco_html(banco, tipo=None):
    """Selo colorido do banco. Para a conta manual usa um selo neutro."""
    if tipo == "MANUAL":
        return '<span class="selo" style="background:#5c6672">R$</span>'
    estilo = BANCOS_ESTILO.get(banco)
    if estilo:
        cor, sigla = estilo[0], estilo[1]
        cor_texto = estilo[2] if len(estilo) > 2 else "#ffffff"
    else:
        cor, sigla, cor_texto = "#7b828c", (banco or "?")[:2].upper(), "#ffffff"
    return f'<span class="selo" style="background:{cor};color:{cor_texto}">{sigla}</span>'


def origem_label(tipo, connector_name, nome_conta):
    """Rotulo amigavel (completo) de origem a partir do tipo da conta + nome do banco detectado."""
    banco = detectar_banco(nome_conta, connector_name)
    if tipo == "CREDIT":
        return f"Cartão de Crédito {banco}"
    if tipo == "BANK":
        return f"Conta Corrente {banco}"
    if tipo == "MANUAL":
        return "Dinheiro (manual)"
    return nome_conta or "Outra origem"


def origem_label_curto(tipo, connector_name, nome_conta):
    """Rotulo curto de origem, usado na UI ao lado do selo do banco."""
    banco = detectar_banco(nome_conta, connector_name)
    if tipo == "CREDIT":
        return f"Cartão {banco}"
    if tipo == "BANK":
        return f"Conta Corrente {banco}"
    if tipo == "MANUAL":
        return "Dinheiro"
    return nome_conta or "Outra"


def carregar_origens(cur):
    """Le todas as contas (Pluggy + manual) e devolve estruturas prontas de origem.

    O nome do banco costuma so aparecer no nome de UMA das contas da conexao (ex: a conta
    corrente traz a razao social do banco, o cartao traz so 'Cartao de credito'). Por isso
    detectamos o banco olhando todas as contas da conexao (item_id) e aplicamos para todas.
    """
    cur.execute(
        "SELECT c.account_id, c.item_id, c.tipo, c.nome, c.numero_final, p.connector_name "
        "FROM cartao.conta c JOIN cartao.pluggy_item p ON p.item_id = c.item_id "
        "ORDER BY c.tipo, p.connector_name;"
    )
    contas = cur.fetchall()

    banco_por_item = {}
    for c in contas:
        banco = detectar_banco(c["nome"], c["connector_name"])
        if banco != (c["connector_name"] or c["nome"] or "Banco"):
            banco_por_item.setdefault(c["item_id"], banco)

    contas_by_id = {}
    opcoes = []
    for c in contas:
        banco = banco_por_item.get(c["item_id"], c["connector_name"])
        completo = origem_label(c["tipo"], banco, c["nome"])
        curto = origem_label_curto(c["tipo"], banco, c["nome"])
        selo = selo_banco_html(detectar_banco(c["nome"], banco), c["tipo"])
        aid = str(c["account_id"])
        contas_by_id[aid] = {
            **c, "banco": banco, "label": completo, "label_curto": curto, "selo": selo,
        }
        # (valor, html com selo, titulo do tooltip, texto sem html)
        opcoes.append((aid, f"{selo}{curto}", completo, curto))
    return contas_by_id, opcoes


IMPORT_NAMESPACE = uuid.UUID("6f1c2a52-0000-4000-8000-000000000042")


def _decodificar(raw):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _num_br(txt):
    """Converte '1.234,56', '-1234.56', 'R$ 1.234,56' em float."""
    if txt is None:
        return None
    s = str(txt).strip().replace("R$", "").replace(" ", "").replace("\xa0", "")
    if not s:
        return None
    negativo = s.startswith("(") and s.endswith(")")
    if negativo:
        s = s[1:-1]
    s = s.replace("+", "")
    if "," in s and "." in s:
        # 1.234,56 (BR) ou 1,234.56 (US) - o ultimo separador manda
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if negativo else v


def _data_br(txt):
    """Aceita dd/mm/aaaa, aaaa-mm-dd e o formato OFX (aaaammdd...)."""
    s = str(txt or "").strip()
    if not s:
        return None
    # OFX: 20260115 ou 20260115120000[-3:BRT]
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:\d{2})?", s)
    if m and not re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_ofx(texto):
    """Extrai as transacoes de um arquivo OFX (formato SGML dos bancos brasileiros)."""
    linhas = []
    for bloco in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", texto, re.S | re.I):
        def tag(nome):
            m = re.search(rf"<{nome}>([^<\r\n]*)", bloco, re.I)
            return m.group(1).strip() if m else ""
        data = _data_br(tag("DTPOSTED"))
        valor = _num_br(tag("TRNAMT"))
        desc = tag("MEMO") or tag("NAME")
        if data is None or valor is None:
            continue
        linhas.append({"data": data, "descricao": desc.strip(), "valor": valor, "fitid": tag("FITID")})
    return linhas


def parse_csv(texto):
    """Extrai transacoes de um CSV, detectando delimitador e as colunas de data/descricao/valor."""
    # escolhe o delimitador que produz o maior numero de colunas de forma consistente
    # (o Sniffer erra com valores em formato BR, onde a virgula e decimal)
    melhor, melhor_score = ";", -1
    for cand in (";", ",", "\t", "|"):
        try:
            linhas = [l for l in csv.reader(io.StringIO(texto), delimiter=cand) if any((c or "").strip() for c in l)]
        except csv.Error:
            continue
        if not linhas:
            continue
        contagens = [len(l) for l in linhas[:50]]
        mais_comum = max(set(contagens), key=contagens.count)
        if mais_comum < 2:
            continue
        consistencia = contagens.count(mais_comum) / len(contagens)
        score = mais_comum * consistencia
        if score > melhor_score:
            melhor, melhor_score = cand, score
    delim = melhor

    linhas_raw = list(csv.reader(io.StringIO(texto), delimiter=delim))
    linhas_raw = [l for l in linhas_raw if any((c or "").strip() for c in l)]
    if not linhas_raw:
        return [], "Arquivo vazio."

    def norm(s):
        s = (s or "").strip().lower()
        for a, b in (("ç", "c"), ("ã", "a"), ("á", "a"), ("â", "a"), ("é", "e"), ("ê", "e"), ("í", "i"), ("ó", "o"), ("õ", "o"), ("ú", "u")):
            s = s.replace(a, b)
        return s

    # procura a linha de cabecalho nas primeiras linhas
    idx_cab, cols = None, {}
    for i, linha in enumerate(linhas_raw[:15]):
        n = [norm(c) for c in linha]
        c = {}
        for j, cel in enumerate(n):
            if "data" in cel or cel in ("date", "dt"):
                c.setdefault("data", j)
            elif any(k in cel for k in ("descri", "historico", "lancamento", "memo", "estabelecimento", "titulo")):
                c.setdefault("descricao", j)
            elif any(k in cel for k in ("valor", "amount", "montante")) and "moeda" not in cel:
                c.setdefault("valor", j)
        if "data" in c and "valor" in c:
            idx_cab, cols = i, c
            break

    resultado = []
    if idx_cab is None:
        # sem cabecalho reconhecido: tenta posicional (data ; descricao ; valor)
        for linha in linhas_raw:
            if len(linha) < 3:
                continue
            data = _data_br(linha[0])
            valor = _num_br(linha[-1])
            if data and valor is not None:
                resultado.append({"data": data, "descricao": " ".join(linha[1:-1]).strip(), "valor": valor, "fitid": ""})
        if not resultado:
            return [], "Não consegui identificar as colunas de data, descrição e valor. Verifique o arquivo."
        return resultado, None

    for linha in linhas_raw[idx_cab + 1:]:
        if len(linha) <= max(cols.values()):
            continue
        data = _data_br(linha[cols["data"]])
        valor = _num_br(linha[cols["valor"]])
        if data is None or valor is None:
            continue
        desc = linha[cols["descricao"]].strip() if "descricao" in cols else ""
        resultado.append({"data": data, "descricao": desc, "valor": valor, "fitid": ""})
    if not resultado:
        return [], "Nenhuma linha válida encontrada no arquivo."
    return resultado, None


def normalizar_para_conta(linhas, tipo_conta, inverter):
    """Ajusta o sinal ao padrao usado no banco de dados.

    - Conta corrente (BANK): entrada positiva, saida negativa (igual ao OFX).
    - Cartao (CREDIT): compra positiva, pagamento negativo (invertido em relacao ao OFX).
    """
    saida = []
    for l in linhas:
        v = float(l["valor"])
        if inverter:
            v = -v
        if tipo_conta == "CREDIT":
            tipo = "DEBIT" if v > 0 else "CREDIT"
        else:
            tipo = "CREDIT" if v > 0 else "DEBIT"
        saida.append({**l, "valor": round(v, 2), "tipo": tipo})
    return saida


def chip_filter_html(nome, label, opcoes, selecionados, onchange="aplicarFiltros()"):
    """Filtro em chip com dropdown, busca e multi-selecao.

    opcoes: lista de (value, texto) ou (value, texto_curto, texto_completo).
    """
    n_sel = len(selecionados)
    partes = []
    for opt in opcoes:
        val, texto = opt[0], opt[1]
        titulo = opt[2] if len(opt) > 2 else texto
        curto = opt[3] if len(opt) > 3 else None
        marcado = "checked" if str(val) in selecionados else ""
        attr_curto = f' data-curto="{curto}"' if curto else ""
        partes.append(
            f'<label class="chip-opt" data-tip="{titulo}"{attr_curto}>'
            f'<input type="checkbox" name="{nome}" value="{val}" {marcado} '
            f'onchange="{onchange}"> {texto}</label>'
        )
    opts_html = "".join(partes)
    return f"""
    <div class="chipfilter">
      <button type="button" class="chip-btn {"ativo" if n_sel else ""}" data-label="{label}" onclick="cfToggle(this)">
        <span class="chip-plus">+</span> {label}{f' ({n_sel})' if n_sel else ''}
        {f'<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' if n_sel else ''}
      </button>
      <div class="chip-panel">
        <div class="chip-search-wrap"><input type="text" class="chip-search" placeholder="Procure {label.lower()}..." oninput="cfFiltrar(this)" onkeydown="cfKeydown(event, this)"></div>
        <div class="chip-list">{opts_html}</div>
      </div>
    </div>
    """


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


def get_ultima_sincronizacao():
    """Busca o status da ultima execucao de sync registrada pelo bussola-financeira-app."""
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT executado_em, status, transacoes_novas, transacoes_atualizadas, mensagem_erro "
            "FROM cartao.sync_log ORDER BY executado_em DESC LIMIT 1;"
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return {"executado_em": None, "status": None}
        executado_local = row["executado_em"] - timedelta(hours=3) if row["executado_em"] else None
        return {
            "executado_em": executado_local.strftime("%d/%m/%Y %H:%M") if executado_local else None,
            "status": row["status"],
            "transacoes_novas": row["transacoes_novas"],
            "transacoes_atualizadas": row["transacoes_atualizadas"],
            "mensagem_erro": row["mensagem_erro"],
        }
    except Exception as e:
        return {"executado_em": None, "status": "erro", "mensagem_erro": str(e)}


def disparar_sincronizacao():
    """Chama o endpoint /sync do bussola-financeira-app para forcar uma atualizacao imediata."""
    try:
        req = urllib.request.Request(BUSSOLA_SYNC_URL, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        return True, None
    except urllib.error.URLError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


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
        # marca lancamentos que entraram por importacao de arquivo (nao vieram do Pluggy),
        # para permitir exclui-los sem risco de "ressuscitarem" numa sincronizacao
        cur.execute("ALTER TABLE cartao.transacao ADD COLUMN IF NOT EXISTS importado boolean DEFAULT false;")
        # natureza definida no proprio lancamento, quando ele foge do padrao da categoria
        # (ex: um PIX de R$ 98 mil que foi a compra de um terreno, e nao consumo)
        cur.execute("ALTER TABLE cartao.transacao ADD COLUMN IF NOT EXISTS natureza text;")
        # renomeia a dimensao antiga (nao roda de novo depois de renomeada)
        cur.execute("UPDATE cartao.dimensao SET nome = 'Projeto' WHERE nome = 'Projeto / Evento';")
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
        # natureza de cada categoria (base do DRE). ON CONFLICT DO NOTHING para
        # nunca sobrescrever uma classificacao que o usuario tenha ajustado.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cartao.categoria_natureza ("
            "categoria text PRIMARY KEY, natureza text NOT NULL DEFAULT 'despesa');"
        )
        for categoria, natureza in SEED_NATUREZAS.items():
            cur.execute(
                "INSERT INTO cartao.categoria_natureza (categoria, natureza) VALUES (%s,%s) "
                "ON CONFLICT (categoria) DO NOTHING;",
                (categoria, natureza),
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

        # juros e tarifas passaram a contar como despesa real: garante o grupo
        # "Despesas Financeiras" tambem nas bases que ja tinham sido semeadas
        cur.execute("SELECT id FROM cartao.grupo_custo WHERE nome = 'Despesas Financeiras';")
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO cartao.grupo_custo (nome) VALUES ('Despesas Financeiras') RETURNING id;"
            )
            grupo_fin_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO cartao.subgrupo_custo (grupo_id, nome) VALUES (%s, 'Juros & Tarifas') RETURNING id;",
                (grupo_fin_id,),
            )
            sub_fin_id = cur.fetchone()[0]
            for categoria in ("Interests charged", "Credit card fees", "Tax on financial operations"):
                cur.execute(
                    "INSERT INTO cartao.categoria_subgrupo (categoria, subgrupo_id) VALUES (%s,%s) "
                    "ON CONFLICT (categoria) DO UPDATE SET subgrupo_id = EXCLUDED.subgrupo_id;",
                    (categoria, sub_fin_id),
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

        # conta sintetica para lancamentos manuais (dinheiro em especie), fora do Pluggy
        cur.execute(
            "INSERT INTO cartao.pluggy_item (item_id, connector_name, status) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'Manual', 'OK') "
            "ON CONFLICT (item_id) DO NOTHING;"
        )
        cur.execute(
            "INSERT INTO cartao.conta (account_id, item_id, nome, tipo, numero_final) VALUES "
            "('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', "
            "'Dinheiro', 'MANUAL', NULL) "
            "ON CONFLICT (account_id) DO NOTHING;"
        )
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM cartao.dimensao;")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO cartao.dimensao (nome, obrigatoria, ordem) VALUES ('Responsável', true, 1) RETURNING id;")
            resp_id = cur.fetchone()[0]
            for nome in ("Ronaldo", "Andrea", "Amanda", "Compartilhado"):
                cur.execute("INSERT INTO cartao.dimensao_valor (dimensao_id, nome) VALUES (%s,%s);", (resp_id, nome))

            cur.execute("INSERT INTO cartao.dimensao (nome, obrigatoria, ordem) VALUES ('Projeto', false, 2) RETURNING id;")
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
    ("Despesas Financeiras", None, None, [
        ("Juros & Tarifas", None, None, ["Interests charged", "Credit card fees", "Tax on financial operations"]),
    ]),
]

migrate()


BASE_CSS = """
<style>
  :root {
    --bg: #f7f7f5;
    --surface: #ffffff;
    --ink: #18191b;
    --ink-soft: #5c5f66;
    --ink-faint: #9297a1;
    --line: #e7e7e4;
    --line-soft: #f0f0ee;
    --accent: #2f5fdb;
    --accent-soft: #eef2fd;
    --good: #1f8a53;
    --good-soft: #eaf6ef;
    --bad: #c23c34;
    --bad-soft: #fbeceb;
    --radius: 10px;
    --radius-sm: 7px;
    --shadow-1: 0 1px 1px rgba(20,20,20,.03), 0 1px 3px rgba(20,20,20,.04);
    --shadow-2: 0 8px 24px rgba(20,20,20,.10), 0 2px 6px rgba(20,20,20,.06);
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    background: var(--bg);
    margin: 0;
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }

  /* ---------- topbar ---------- */
  .topbar {
    background: var(--surface);
    color: var(--ink);
    padding: 14px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--line);
  }
  .topbar > div:first-child { display: flex; align-items: baseline; gap: 9px; }
  .marca { font-weight: 700; font-size: 15px; letter-spacing: -0.02em; color: var(--ink); }
  .marca-pagina { font-size: 12.5px; color: var(--ink-faint); }
  .topbar a { color: var(--ink-soft); text-decoration: none; font-size: 13.5px; transition: color .15s; }
  .topbar a:hover { color: var(--ink); }

  .wrap { max-width: 1200px; margin: 24px auto; padding: 0 20px; }

  /* ---------- filters bar ---------- */
  .filters {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 14px 18px;
    margin-bottom: 18px;
    display: flex;
    gap: 20px;
    align-items: center;
    flex-wrap: wrap;
  }
  .filters label { font-size: 12px; color: var(--ink-faint); margin-right: 7px; text-transform: uppercase; letter-spacing: .03em; }
  select, input[type=month] {
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 13.5px;
    background: var(--surface);
    color: var(--ink);
  }
  select:focus, input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

  /* ---------- table ---------- */
  table { width: 100%; border-collapse: collapse; background: var(--surface); border-radius: var(--radius); overflow: hidden; border: 1px solid var(--line); }
  th { text-align: left; background: var(--bg); padding: 11px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-faint); font-weight: 600; border-bottom: 1px solid var(--line); }
  td { padding: 10px 12px; border-top: 1px solid var(--line-soft); font-size: 13.5px; vertical-align: middle; }
  tr.conferida { background: var(--good-soft); }
  tbody tr { cursor: pointer; transition: background .1s; }
  tbody tr:hover { background: var(--accent-soft); }
  tr.conferida:hover { background: #dcf1e3; }
  tr.duplicada td { text-decoration: line-through; color: var(--ink-faint); }
  tr.duplicada { background: #faf6ee; }
  tr.duplicada:hover { background: #f4ecdb; }
  .valor { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-weight: 500; }

  .obs-input { width: 100%; padding: 6px 8px; border: 1px solid var(--line); border-radius: var(--radius-sm); font-size: 12.5px; color: var(--ink); }
  .cat-select { padding: 6px 7px; border-radius: var(--radius-sm); border: 1px solid var(--line); font-size: 12.5px; max-width: 180px; color: var(--ink); }
  .status { font-size: 11px; color: var(--good); margin-left: 6px; opacity: 0; transition: opacity .3s; font-weight: 500; }
  .status.show { opacity: 1; }

  /* ---------- tabela compacta de lancamentos ---------- */
  table.compacta { table-layout: fixed; font-size: 12.5px; }
  table.compacta th { padding: 8px 8px; font-size: 10px; }
  table.compacta td { padding: 5px 8px; font-size: 12.5px; }
  table.compacta .cel-data { width: 78px; color: var(--ink-soft); font-size: 11.5px; line-height: 1.25; }
  table.compacta .cel-desc { width: auto; min-width: 240px; max-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  table.compacta .cel-origem { width: 158px; overflow: hidden; color: var(--ink-soft); font-size: 11.5px; }
  table.compacta .cel-valor { width: 96px; }
  table.compacta .cel-check { width: 42px; text-align: center; }
  table.compacta .cel-obs { width: 130px; }
  table.compacta .cel-status { width: 44px; }
  table.compacta .cel-dim { width: 116px; }
  table.compacta select { width: 100%; max-width: 100%; padding: 4px 5px; font-size: 11.5px; border-radius: 5px; }
  table.compacta .obs-input { padding: 4px 6px; font-size: 11.5px; }
  table.compacta input[type=checkbox] { width: 14px; height: 14px; accent-color: var(--accent); }

  /* ---------- chips dos filtros selecionados ---------- */
  .chips-sel { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .chip-tag {
    display: inline-flex; align-items: center; gap: 5px; background: var(--accent-soft); color: var(--accent);
    border: 1px solid #d5e0fa; border-radius: 20px; padding: 3px 8px; font-size: 11.5px; font-weight: 500; max-width: 190px;
  }
  .chip-tag span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chip-tag b { cursor: pointer; font-weight: 700; opacity: .65; }
  .chip-tag b:hover { opacity: 1; }

  .btn-perigo { background: var(--bad); color: #fff; border: none; padding: 8px 14px; border-radius: var(--radius-sm); cursor: pointer; font-size: 13px; font-weight: 500; }
  .btn-perigo:hover { opacity: .88; }

  /* ---------- selo do banco ---------- */
  .selo {
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 23px; height: 16px; padding: 0 5px; border-radius: 4px;
    font-size: 9.5px; font-weight: 700; letter-spacing: .02em;
    margin-right: 6px; vertical-align: middle; flex: none;
  }
  .cel-origem { display: flex; align-items: center; }
  .cel-origem > span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* ---------- tooltip proprio (o nativo do navegador demora ~1s) ---------- */
  #tooltip {
    position: fixed; z-index: 9999; pointer-events: none;
    background: #1f2126; color: #fff; padding: 6px 9px; border-radius: 6px;
    font-size: 12px; line-height: 1.4; max-width: 360px;
    box-shadow: 0 4px 14px rgba(0,0,0,.18);
    opacity: 0; transition: opacity .1s; white-space: normal;
  }
  #tooltip.show { opacity: 1; }

  /* ---------- login ---------- */
  .login-box { max-width: 340px; margin: 100px auto; background: var(--surface); padding: 32px; border-radius: 14px; border: 1px solid var(--line); box-shadow: var(--shadow-2); }
  .login-box h2 { font-size: 19px; letter-spacing: -0.01em; margin: 0 0 18px 0; }
  .login-box input { width: 100%; padding: 10px 12px; margin: 6px 0; border: 1px solid var(--line); border-radius: var(--radius-sm); font-size: 14px; }
  .login-box input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .login-box button, .filters button {
    background: var(--ink); color: #fff; border: none; padding: 9px 18px; border-radius: var(--radius-sm);
    cursor: pointer; font-size: 14px; font-weight: 500; transition: opacity .15s;
  }
  .login-box button:hover, .filters button:hover { opacity: .85; }
  .err { color: var(--bad); font-size: 13px; }

  .summary { font-size: 13px; color: var(--ink-soft); margin-bottom: 10px; }

  /* ---------- summary cards ---------- */
  .cards { display: flex; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; align-items: stretch; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 13px 15px; flex: 1 1 0; min-width: 0; }
  .card .label {
    font-size: 10px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: .03em; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .card .val { font-size: 21px; font-weight: 650; margin-top: 5px; letter-spacing: -0.015em; white-space: nowrap; }
  .card .sub { font-size: 11px; color: var(--ink-faint); margin-top: 3px; white-space: nowrap; }

  .cat-breakdown { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px 18px; margin-bottom: 18px; }
  .cat-breakdown h3 { margin: 0 0 12px 0; font-size: 13px; color: var(--ink-soft); font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }
  /* bloco recolhivel (ex: Gasto por categoria) */
  details.cat-breakdown { padding: 0; }
  details.cat-breakdown > summary {
    list-style: none; cursor: pointer; padding: 14px 18px; font-size: 13px; color: var(--ink-soft);
    font-weight: 600; text-transform: uppercase; letter-spacing: .03em; display: flex;
    align-items: center; justify-content: space-between; user-select: none;
  }
  details.cat-breakdown > summary::-webkit-details-marker { display: none; }
  details.cat-breakdown > summary::after { content: '▾'; font-size: 12px; color: var(--ink-faint); transition: transform .15s; }
  details.cat-breakdown[open] > summary::after { transform: rotate(180deg); }
  details.cat-breakdown > summary:hover { color: var(--ink); }
  details.cat-breakdown > .det-body { padding: 0 18px 14px 18px; }
  .cat-row { display: flex; justify-content: space-between; font-size: 13.5px; padding: 7px 0; border-bottom: 1px solid var(--line-soft); }
  .cat-row:last-child { border-bottom: none; }

  .ver-btn { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 5px 10px; font-size: 12px; cursor: pointer; color: var(--ink-soft); transition: all .15s; }
  .ver-btn:hover { background: var(--bg); border-color: var(--ink-faint); color: var(--ink); }

  /* ---------- modal ---------- */
  .modal-bg { display: none; position: fixed; inset: 0; background: rgba(15,15,15,.5); align-items: center; justify-content: center; z-index: 50; backdrop-filter: blur(2px); }
  .modal-bg.show { display: flex; }
  .modal { background: var(--surface); border-radius: 14px; padding: 26px; width: 420px; max-width: 92vw; box-shadow: var(--shadow-2); border: 1px solid var(--line); }
  .modal h3 { margin-top: 0; font-size: 16px; letter-spacing: -0.01em; }
  .modal .row { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--line-soft); font-size: 13.5px; }
  .modal .row span:first-child { color: var(--ink-faint); }
  .modal .close { float: right; cursor: pointer; color: var(--ink-faint); font-size: 20px; line-height: 1; transition: color .15s; }
  .modal .close:hover { color: var(--ink); }

  .cartao-cell { max-width: 150px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* ---------- nav ---------- */
  .nav-menu { display: flex; gap: 22px; align-items: center; }
  .nav-menu > a { color: var(--ink-soft); text-decoration: none; font-size: 13.5px; font-weight: 500; transition: color .15s; }
  .nav-menu > a:hover { color: var(--ink); }
  .nav-menu > a.ativo { color: var(--ink); }
  .dropdown { position: relative; display: inline-block; }
  .dropbtn { color: var(--ink-soft); font-size: 13.5px; font-weight: 500; cursor: pointer; background: none; border: none; font-family: inherit; padding: 0; display: flex; align-items: center; gap: 4px; }
  .dropbtn:hover { color: var(--ink); }
  .dropdown-content { display: none; position: absolute; right: 0; top: 26px; background: var(--surface); min-width: 210px; border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow-2); border: 1px solid var(--line); z-index: 100; }
  .dropdown-content a { display: block; padding: 10px 14px; color: var(--ink-soft); text-decoration: none; font-size: 13px; }
  .dropdown-content a:hover { background: var(--bg); color: var(--ink); }
  .dropdown-content a.ativo { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  /* abre por clique (classe .aberto), nao por hover/focus: com :focus-within o menu
     ficava preso aberto depois de clicar, exigindo dois cliques para reabrir */
  .dropdown.aberto .dropdown-content { display: block; }
  .dropdown.aberto .dropbtn { color: var(--ink); }

  .multisel { padding: 7px; border: 1px solid var(--line); border-radius: var(--radius-sm); min-width: 170px; font-size: 13px; }

  /* ---------- relatorios ---------- */
  .rel-filtros { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin-bottom: 18px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .rel-filtros label { font-size: 11px; color: var(--ink-faint); display: block; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .03em; }
  .rel-grupo-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--line-soft); font-size: 13.5px; }
  .rel-grupo-row .barra { background: var(--bg); border-radius: 4px; height: 6px; margin-top: 4px; overflow: hidden; }
  .rel-grupo-row .barra div { background: var(--accent); height: 100%; border-radius: 4px; }

  .chipfilter { position: relative; display: inline-block; }
  .chip-btn {
    display: flex; align-items: center; gap: 6px; background: var(--surface); border: 1px solid var(--line);
    border-radius: 20px; padding: 8px 14px; font-size: 13px; color: var(--ink-soft); cursor: pointer;
    font-family: inherit; white-space: nowrap; transition: all .15s;
  }
  .chip-btn:hover { border-color: var(--ink-faint); color: var(--ink); }
  .chip-btn.ativo { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); font-weight: 600; }
  .chip-btn .chip-plus { font-size: 15px; line-height: 1; }
  .chip-btn .chip-clear { margin-left: 2px; color: var(--ink-faint); font-weight: 700; }
  .chip-btn .chip-clear:hover { color: var(--bad); }
  .chip-panel { display: none; position: absolute; top: calc(100% + 8px); left: 0; background: var(--surface); border-radius: 12px; box-shadow: var(--shadow-2); width: 260px; z-index: 200; overflow: hidden; border: 1px solid var(--line); }
  .chip-panel.show { display: block; }
  .chip-search-wrap { padding: 10px; border-bottom: 1px solid var(--line-soft); }
  .chip-search { width: 100%; padding: 8px 10px; border: none; background: var(--bg); border-radius: var(--radius-sm); font-size: 13px; outline: none; }
  .chip-list { max-height: 260px; overflow-y: auto; padding: 6px; }
  .chip-opt { display: flex; align-items: center; gap: 9px; padding: 8px 9px; border-radius: var(--radius-sm); font-size: 13px; cursor: pointer; color: var(--ink); }
  .chip-opt:hover, .chip-opt.chip-hover { background: var(--bg); }
  .chip-opt input { accent-color: var(--accent); width: 15px; height: 15px; }

  .rel-grupo-detalhe { background: var(--bg); border-radius: var(--radius-sm); margin: 4px 0 10px 0; overflow: hidden; }
  .rel-mini-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .rel-mini-table th { text-align: left; padding: 7px 10px; color: var(--ink-faint); font-weight: 600; text-transform: uppercase; font-size: 10.5px; border-bottom: 1px solid var(--line); background: transparent; }
  .rel-mini-table td { padding: 7px 10px; border-bottom: 1px solid var(--line-soft); border-top: none; }
  .rel-mini-table .valor { text-align: right; white-space: nowrap; }
  .rel-datewrap { display: flex; gap: 8px; align-items: center; background: var(--surface); border: 1px solid var(--line); border-radius: 20px; padding: 6px 12px; }
  .rel-datewrap input[type=date] { border: none; font-size: 13px; padding: 2px; outline: none; }
  .rel-actions { margin-left: auto; display: flex; gap: 10px; align-items: center; }

  .chart-card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 18px; margin-bottom: 18px; }
  .chart-card h3 { margin: 0 0 14px 0; font-size: 13px; color: var(--ink-soft); font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }

  /* ---------- sync widget ---------- */
  .sync-widget { display: flex; align-items: center; gap: 9px; font-size: 12px; color: var(--ink-faint); }
  .sync-widget .sync-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-faint); flex-shrink: 0; }
  .sync-widget .sync-dot.ok { background: var(--good); }
  .sync-widget .sync-dot.erro { background: var(--bad); }
  .sync-widget .sync-dot.rodando { background: #d69a2d; animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  .sync-btn {
    background: var(--surface); color: var(--ink-soft); border: 1px solid var(--line); padding: 6px 12px;
    border-radius: var(--radius-sm); cursor: pointer; font-size: 12px; font-family: inherit; font-weight: 500;
    transition: all .15s;
  }
  .sync-btn:hover { border-color: var(--ink-faint); color: var(--ink); }
  .sync-btn:disabled { opacity: .6; cursor: default; }
</style>
"""


def topbar_html(titulo, ativo=None):
    def cls(nome):
        return "ativo" if ativo == nome else ""
    return f"""
      <div class="topbar">
        <div>
          <span class="marca">{APP_NOME}</span>
          <span class="marca-pagina">{titulo} · {session.get('user')}</span>
        </div>
        <div class="nav-menu">
          <a href="/" class="{cls('inicio')}">Lançamentos</a>
          <div class="dropdown">
            <button type="button" class="dropbtn" onclick="menuToggle(event, this)">Relatórios ▾</button>
            <div class="dropdown-content">
              <a href="/relatorios" class="{cls('relatorios')}">Relatórios</a>
              <a href="/dre" class="{cls('dre')}">DRE / Centro de Custos</a>
              <a href="/investimentos" class="{cls('investimentos')}">Investimentos</a>
            </div>
          </div>
          <div class="dropdown">
            <button type="button" class="dropbtn" onclick="menuToggle(event, this)">Configurações ▾</button>
            <div class="dropdown-content">
              <a href="/naturezas" class="{cls('naturezas')}">Natureza das categorias</a>
              <a href="/grupos" class="{cls('grupos')}">Gerenciar grupos</a>
              <a href="/dimensoes" class="{cls('dimensoes')}">Gerenciar dimensões</a>
              <a href="/regras" class="{cls('regras')}">Regras automáticas</a>
              <a href="/cartoes" class="{cls('cartoes')}">Gerenciar cartões</a>
              <a href="/importar" class="{cls('importar')}">Importar extrato / fatura</a>
            </div>
          </div>
          <div class="sync-widget">
            <span class="sync-dot" id="syncDot"></span>
            <span id="syncTexto">Verificando...</span>
            <button class="sync-btn" id="syncBtn" onclick="dispararSync()">Atualizar agora</button>
          </div>
          <a href="/logout">Sair</a>
        </div>
      </div>
      <script>
        // ---- tooltip proprio: o balao nativo do navegador so aparece depois de ~1s ----
        (function() {{
          let el = null, timer = null;
          function criar() {{
            if (!el) {{
              el = document.createElement('div');
              el.id = 'tooltip';
              document.body.appendChild(el);
            }}
            return el;
          }}
          function posicionar(e, t) {{
            const m = 14;
            let x = e.clientX + m, y = e.clientY + m;
            const r = t.getBoundingClientRect();
            if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - m;
            if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - m;
            t.style.left = Math.max(6, x) + 'px';
            t.style.top = Math.max(6, y) + 'px';
          }}
          document.addEventListener('mouseover', function(e) {{
            const alvo = e.target.closest('[data-tip]');
            if (!alvo) return;
            const texto = alvo.getAttribute('data-tip');
            if (!texto) return;
            clearTimeout(timer);
            timer = setTimeout(function() {{
              const t = criar();
              t.textContent = texto;
              t.classList.add('show');
              posicionar(e, t);
            }}, 120);
          }});
          document.addEventListener('mousemove', function(e) {{
            if (el && el.classList.contains('show')) posicionar(e, el);
          }});
          document.addEventListener('mouseout', function(e) {{
            if (!e.target.closest('[data-tip]')) return;
            clearTimeout(timer);
            if (el) el.classList.remove('show');
          }});
          document.addEventListener('click', function() {{
            clearTimeout(timer);
            if (el) el.classList.remove('show');
          }});
        }})();

        // menu do topo: abre/fecha no clique e fecha ao clicar fora ou apertar Esc
        function menuToggle(e, btn) {{
          e.stopPropagation();
          const drop = btn.closest('.dropdown');
          const abrir = !drop.classList.contains('aberto');
          document.querySelectorAll('.dropdown.aberto').forEach(d => d.classList.remove('aberto'));
          if (abrir) drop.classList.add('aberto');
          btn.blur();
        }}
        document.addEventListener('click', function(e) {{
          if (!e.target.closest('.dropdown')) {{
            document.querySelectorAll('.dropdown.aberto').forEach(d => d.classList.remove('aberto'));
          }}
        }});
        document.addEventListener('keydown', function(e) {{
          if (e.key === 'Escape') document.querySelectorAll('.dropdown.aberto').forEach(d => d.classList.remove('aberto'));
        }});

        function syncEhSucesso(status) {{
          if (!status) return false;
          const s = String(status).toLowerCase();
          return s === 'ok' || s === 'success' || s === 'sucesso';
        }}
        function syncFormatarTexto(d) {{
          if (!d.executado_em) return 'Sem sincronização registrada';
          let txt = 'Atualizado em ' + d.executado_em;
          if (d.status && !syncEhSucesso(d.status)) txt += ' (erro)';
          return txt;
        }}
        async function syncCarregarStatus() {{
          try {{
            const r = await fetch('/api/sync-status');
            const d = await r.json();
            document.getElementById('syncTexto').textContent = syncFormatarTexto(d);
            const dot = document.getElementById('syncDot');
            dot.className = 'sync-dot ' + (syncEhSucesso(d.status) ? 'ok' : (d.status ? 'erro' : ''));
          }} catch (e) {{
            document.getElementById('syncTexto').textContent = 'Status indisponível';
          }}
        }}
        async function dispararSync() {{
          const btn = document.getElementById('syncBtn');
          const dot = document.getElementById('syncDot');
          btn.disabled = true;
          btn.textContent = 'Atualizando...';
          dot.className = 'sync-dot rodando';
          document.getElementById('syncTexto').textContent = 'Sincronizando com o Pluggy...';
          try {{
            const r = await fetch('/api/sync-agora', {{ method: 'POST' }});
            const d = await r.json();
            document.getElementById('syncTexto').textContent = syncFormatarTexto(d);
            dot.className = 'sync-dot ' + (syncEhSucesso(d.status) ? 'ok' : 'erro');
          }} catch (e) {{
            document.getElementById('syncTexto').textContent = 'Falha ao atualizar';
            dot.className = 'sync-dot erro';
          }} finally {{
            btn.disabled = false;
            btn.textContent = 'Atualizar agora';
          }}
        }}
        syncCarregarStatus();
      </script>
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
    <html><head><title>Entrar · Meu Dinheiro</title>{BASE_CSS}</head>
    <body>
      <div class="login-box">
        <h2>Meu Dinheiro</h2>
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


@app.route("/api/sync-status")
@login_required
def api_sync_status():
    return jsonify(get_ultima_sincronizacao())


@app.route("/api/sync-agora", methods=["POST"])
@login_required
def api_sync_agora():
    ok, erro = disparar_sincronizacao()
    if not ok:
        return jsonify({"executado_em": None, "status": "erro", "mensagem_erro": erro}), 502
    return jsonify(get_ultima_sincronizacao())


@app.route("/")
@login_required
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
    categorias = sorted(categorias_db | set(CATEGORIAS_EXTRA), key=lambda c: cat_pt(c).lower())

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

    cur.execute("SELECT vencimento_fatura FROM cartao.conta WHERE tipo = 'CREDIT' LIMIT 1;")
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
        data_fmt = data_local.strftime("%d/%m/%y<br>%H:%M")
        data_fmt_full = data_local.strftime("%d/%m/%Y %H:%M")
        obs = (r["observacao"] or "").replace('"', "&quot;")
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
        else:
            cor_valor = ""
            valor_fmt = f'R$ {r["valor"]:,.2f}'

        dim_tds = []
        dim_detalhes = {}
        for d in dimensoes:
            valor_sel = mapa_dim_transacao.get((str(rid), d["id"]))
            faltando = d["obrigatoria"] and not valor_sel
            estilo = ' style="border-color:#c23c34;background:#fbeceb"' if faltando else ""
            dim_tds.append(
                f'<td class="cel-dim"><select class="dim-select" data-dim="{d["id"]}"{estilo} '
                f'onchange="salvar(\'{rid}\', this)">{dim_options(d["id"], valor_sel)}</select></td>'
            )
            nomes_valor = {v["id"]: v["nome"] for v in valores_por_dim.get(d["id"], [])}
            dim_detalhes[d["nome"]] = nomes_valor.get(valor_sel, "(nao definido)")

        trs.append(
            f'<tr class="{classes}" data-id="{rid}" onclick="linhaClick(event, \'{rid}\')">'
            f'<td class="cel-data" data-tip="{data_fmt_full}">{data_fmt}</td>'
            f'<td class="cel-desc" data-tip="{desc}">{desc}</td>'
            f'<td class="cel-origem" data-tip="{origem_completa(r["account_id"], r["numero_cartao_final"])}">{origem_curta(r["account_id"], r["numero_cartao_final"])}</td>'
            f'<td class="cel-dim"><select class="cat-select" onchange="salvar(\'{rid}\', this)">{cat_options(r["categoria"])}</select></td>'
            + "".join(dim_tds) +
            f'<td class="valor cel-valor" style="{cor_valor}">{valor_fmt}</td>'
            f'<td class="cel-obs"><input class="obs-input" type="text" value="{obs}" placeholder="obs..." onblur="salvar(\'{rid}\', this)"></td>'
            f'<td class="cel-check"><input class="conf-check" type="checkbox" {checked} onchange="salvar(\'{rid}\', this)">'
            f'<input class="dup-check" type="checkbox" {dup_checked} hidden></td>'
            f'<td class="cel-status"><span class="status" id="status-{rid}">ok</span></td>'
            f'</tr>'
        )
        detalhes = {
            "data": data_fmt_full,
            "descricao": desc,
            "categoria": cat_pt(r["categoria"]),
            "valor": valor_fmt,
            "valor_original": f'{r["valor_original"]:,.2f} {r["moeda_original"] or ""}' if r["valor_original"] is not None else "-",
            "status": r["status"] or "-",
            "tipo": r["tipo"] or "-",
            "origem": origem_completa(r["account_id"], r["numero_cartao_final"]),
            "parcela": f'{r["parcela_atual"]}/{r["parcela_total"]}' if r["parcela_total"] and r["parcela_total"] > 1 else "À vista",
            "conferida": "Sim" if r["conferida"] else "Não",
            "conferida_por": r["conferida_por"] or "-",
            "observacao": r["observacao"] or "-",
            "_manual": bool(eh_manual),
            "_natureza": r["natureza"] or "",
            "_natureza_efetiva": NATUREZAS.get(r["natureza_efetiva"], r["natureza_efetiva"]),
        }
        detalhes.update(dim_detalhes)
        detalhes_js[str(rid)] = detalhes

    total = resumo["total"] or 0
    conf = resumo["conferidas"] or 0
    gasto_real = resumo["gasto_real"] or 0
    receita_mes = resumo["receita_mes"] or 0
    resultado_mes = receita_mes - gasto_real
    cor_resultado = "#1f8a53" if resultado_mes >= 0 else "#c23c34"
    colspan_total = 8 + len(dimensoes)
    body_rows = "".join(trs) if trs else f'<tr><td colspan="{colspan_total}" style="padding:20px;text-align:center;color:#888">Nenhum lançamento neste filtro.</td></tr>'
    dim_headers = "".join(f'<th class="cel-dim">{d["nome"]}{" *" if d["obrigatoria"] else ""}</th>' for d in dimensoes)

    cat_rows_html = "".join(
        f'<div class="cat-row"><span>{cat_pt(c["categoria"])}</span><span>R$ {c["total"]:,.2f}</span></div>'
        for c in por_categoria
    ) or '<div class="cat-row"><span>Sem dados</span></div>'

    origem_filtro_html = chip_filter_html("origem", "Origem", origem_opcoes, origem_sel, onchange="aplicarFiltros()")
    categoria_options_manual = "".join(f'<option value="{c}">{cat_pt(c)}</option>' for c in categorias)
    natureza_options = "".join(
        f'<option value="{k}">{v}</option>' for k, v in NATUREZAS.items() if k != "fluxo"
    )

    return f"""
    <html><head><title>Lançamentos · Meu Dinheiro</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Lançamentos', 'inicio')}
      <div class="wrap">
        <div class="filters" style="flex-wrap:wrap;gap:14px">
          <div>
            <label>Mes</label>
            <input type="month" id="mesInput" value="{mes}" onchange="aplicarFiltros()">
          </div>
          <div>
            <label>Status</label>
            <select id="statusInput" onchange="aplicarFiltros()">
              <option value="todas" {"selected" if status=="todas" else ""}>Todas</option>
              <option value="pendente" {"selected" if status=="pendente" else ""}>Pendentes</option>
              <option value="conferida" {"selected" if status=="conferida" else ""}>Conferidas</option>
            </select>
          </div>
          {origem_filtro_html}
          <div class="chips-sel" id="chipsSel"></div>
          <div style="margin-left:auto">
            <button type="button" onclick="toggleFormManual()">+ Lançamento manual</button>
          </div>
        </div>

        <div id="formManual" class="cat-breakdown" style="display:none">
          <h3>Novo lançamento manual (dinheiro)</h3>
          <form onsubmit="return salvarManual(event)" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="date" id="manualData" required value="{datetime.now().strftime('%Y-%m-%d')}" style="padding:7px 9px;border:1px solid var(--line);border-radius:6px">
            <input type="text" id="manualDescricao" placeholder="Descrição" required style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;flex:1;min-width:160px">
            <select id="manualDirecao" style="padding:7px 9px">
              <option value="saida">Saída</option>
              <option value="entrada">Entrada</option>
            </select>
            <input type="number" id="manualValor" step="0.01" min="0.01" placeholder="Valor (R$)" required style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;width:130px">
            <select id="manualCategoria" style="padding:7px 9px">{categoria_options_manual}</select>
            <button type="submit">Adicionar</button>
            <span id="manualStatus" style="font-size:12px;color:#888"></span>
          </form>
        </div>

        <div class="cards">
          <div class="card"><div class="label" title="Receitas do mês">Receitas do mês</div><div class="val" style="color:#1f8a53">R$ {receita_mes:,.2f}</div></div>
          <div class="card"><div class="label" title="Despesas do mês">Despesas do mês</div><div class="val" style="color:#c23c34">R$ {gasto_real:,.2f}</div></div>
          <div class="card"><div class="label" title="Resultado do mês (receitas menos despesas)">Resultado do mês</div><div class="val" style="color:{cor_resultado}">R$ {resultado_mes:,.2f}</div></div>
          <div class="card"><div class="label" title="Conferidas">Conferidas</div><div class="val">{conf} / {total}</div></div>
          <div class="card"><div class="label" title="Fechamento da fatura">Fechamento fatura</div><div class="val">Dia {FATURA_DIA_FECHAMENTO}</div><div class="sub">Próx: {proximo_fechamento.strftime('%d/%m/%y')}</div></div>
          <div class="card"><div class="label" title="Vencimento da fatura">Vencimento fatura</div><div class="val">{'Dia ' + str(dia_vencimento) if dia_vencimento else '-'}</div><div class="sub">{'Próx: ' + proximo_vencimento.strftime('%d/%m/%y') if proximo_vencimento else ''}</div></div>
        </div>

        <details class="cat-breakdown">
          <summary>Gasto por categoria (mês)</summary>
          <div class="det-body">{cat_rows_html}</div>
        </details>

        <table class="compacta">
          <thead><tr>
            <th class="cel-data">Data</th><th class="cel-desc">Descricao</th><th class="cel-origem">Origem</th><th class="cel-dim">Categoria</th>{dim_headers}<th class="cel-valor" style="text-align:right">Valor</th><th class="cel-obs">Obs</th><th class="cel-check">Conf</th><th class="cel-status"></th>
          </tr></thead>
          <tbody>{body_rows}</tbody>
        </table>
      </div>

      <div class="modal-bg" id="modalBg" onclick="if(event.target===this) fecharModal()">
        <div class="modal">
          <span class="close" onclick="fecharModal()">&times;</span>
          <h3>Detalhes do lançamento</h3>
          <div id="modalBody"></div>
          <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line-soft)">
            <div style="font-size:13px;margin-bottom:6px">Natureza deste lançamento</div>
            <select id="modalNatureza" onchange="salvarNaturezaModal()" style="width:100%;padding:7px 9px">
              <option value="">Seguir a categoria</option>
              {natureza_options}
            </select>
            <div style="font-size:11.5px;color:var(--ink-faint);margin-top:5px">
              Use quando o lançamento foge do padrão da categoria — por exemplo um PIX que foi
              a compra de um terreno ou veículo: não é despesa, é aquisição de bem.
            </div>
          </div>
          <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line-soft)">
            <label style="display:flex;align-items:center;gap:9px;font-size:13px;cursor:pointer">
              <input type="checkbox" id="modalDup" onchange="toggleDuplicadaModal()" style="width:15px;height:15px;accent-color:var(--accent)">
              Marcar como duplicada
            </label>
            <div style="font-size:11.5px;color:var(--ink-faint);margin-top:5px">
              Lançamentos duplicados ficam riscados e não entram nos totais.
            </div>
          </div>
          <div id="modalAcoes" style="margin-top:14px;display:none">
            <button type="button" class="btn-perigo" onclick="excluirManual()">Excluir lançamento</button>
            <div style="font-size:11.5px;color:var(--ink-faint);margin-top:7px">Só lançamentos manuais ou importados de arquivo podem ser excluídos.</div>
          </div>
        </div>
      </div>

      <script>
        // ---- chip filter (origem): filtra sem fechar o painel ----
        function cfToggle(btn) {{
          const panel = btn.nextElementSibling;
          const abrir = !panel.classList.contains('show');
          document.querySelectorAll('.chip-panel.show').forEach(p => {{ if (p !== panel) p.classList.remove('show'); }});
          if (abrir) {{
            panel.classList.add('show');
            const search = panel.querySelector('.chip-search');
            if (search) {{ search.value = ''; cfFiltrar(search); search.focus(); }}
          }} else {{
            panel.classList.remove('show');
          }}
        }}
        document.addEventListener('click', function(e) {{
          if (!e.target.closest('.chipfilter') && !e.target.closest('.chip-tag')) {{
            document.querySelectorAll('.chip-panel.show').forEach(p => p.classList.remove('show'));
          }}
        }});
        function cfClear(e, btn) {{
          e.stopPropagation();
          const panel = btn.closest('.chipfilter').querySelector('.chip-panel');
          panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
          aplicarFiltros();
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
              aplicarFiltros();
            }}
          }} else if (e.key === 'Escape') {{
            panel.classList.remove('show');
          }}
        }}

        function atualizarChipLabels() {{
          document.querySelectorAll('.chipfilter').forEach(cf => {{
            const btn = cf.querySelector('.chip-btn');
            const label = btn.dataset.label;
            const n = cf.querySelectorAll('input[type=checkbox]:checked').length;
            btn.classList.toggle('ativo', n > 0);
            btn.innerHTML = '<span class="chip-plus">+</span> ' + label + (n ? ' (' + n + ')' : '') +
              (n ? '<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' : '');
          }});
          // chips pequenos ao lado mostrando o que esta selecionado
          const cont = document.getElementById('chipsSel');
          const marcados = Array.from(document.querySelectorAll('.chipfilter input[type=checkbox]:checked'));
          cont.innerHTML = marcados.map(cb => {{
            const lbl = cb.closest('.chip-opt');
            const curto = lbl.dataset.curto || lbl.textContent.trim();
            const completo = lbl.getAttribute('data-tip') || curto;
            return '<span class="chip-tag" title="' + completo + '"><span>' + curto + '</span>' +
                   '<b onclick="desmarcarOrigem(\\'' + cb.value + '\\')">&times;</b></span>';
          }}).join('');
        }}
        function desmarcarOrigem(valor) {{
          const cb = document.querySelector('.chipfilter input[type=checkbox][value="' + valor + '"]');
          if (cb) {{ cb.checked = false; aplicarFiltros(); }}
        }}

        // ---- aplica filtros via AJAX: o dropdown continua aberto ----
        function coletarQuery() {{
          const params = new URLSearchParams();
          params.set('mes', document.getElementById('mesInput').value);
          params.set('status', document.getElementById('statusInput').value);
          document.querySelectorAll('.chipfilter input[type=checkbox]:checked').forEach(cb => params.append(cb.name, cb.value));
          return params;
        }}
        function aplicarFiltros() {{
          atualizarChipLabels();
          const params = coletarQuery();
          history.replaceState(null, '', '/?' + params.toString());
          fetch('/?' + params.toString(), {{ headers: {{ 'X-Parcial': '1' }} }})
            .then(r => r.text())
            .then(html => {{
              const doc = new DOMParser().parseFromString(html, 'text/html');
              const novaTabela = doc.querySelector('table.compacta');
              const novosCards = doc.querySelector('.cards');
              const novaCat = doc.querySelector('details.cat-breakdown');
              if (novaTabela) document.querySelector('table.compacta').replaceWith(novaTabela);
              if (novosCards) document.querySelector('.cards').replaceWith(novosCards);
              const catAtual = document.querySelector('details.cat-breakdown');
              if (novaCat && catAtual) {{
                // preserva o estado aberto/fechado escolhido pelo usuario
                novaCat.open = catAtual.open;
                catAtual.replaceWith(novaCat);
              }}
              const scriptNovo = doc.querySelector('script[data-detalhes]');
              if (scriptNovo) {{
                try {{ window.detalhes = JSON.parse(scriptNovo.textContent); }} catch (e) {{}}
              }}
            }});
        }}

        window.detalhes = {json.dumps(detalhes_js)};
        let idAtualModal = null;
        function verDetalhes(id) {{
          const d = window.detalhes[id];
          if (!d) return;
          idAtualModal = id;
          const labels = {{
            data: 'Data', descricao: 'Descrição', categoria: 'Categoria', valor: 'Valor (R$)',
            valor_original: 'Valor original', status: 'Status', tipo: 'Tipo', origem: 'Origem',
            parcela: 'Parcela', conferida: 'Conferida', conferida_por: 'Conferida por', observacao: 'Observação'
          }};
          let html = '';
          for (const k in labels) {{
            html += '<div class="row"><span>' + labels[k] + '</span><span>' + d[k] + '</span></div>';
          }}
          for (const k in d) {{
            if (!(k in labels) && k.charAt(0) !== '_') {{
              html += '<div class="row"><span>' + k + '</span><span>' + d[k] + '</span></div>';
            }}
          }}
          document.getElementById('modalBody').innerHTML = html;
          document.getElementById('modalAcoes').style.display = d._manual ? 'block' : 'none';
          // reflete o estado atual de "duplicada" da linha correspondente
          const trAtual = document.querySelector('tr[data-id="' + id + '"]');
          const dupAtual = trAtual ? trAtual.querySelector('.dup-check') : null;
          document.getElementById('modalDup').checked = dupAtual ? dupAtual.checked : false;
          const selNat = document.getElementById('modalNatureza');
          selNat.options[0].textContent = 'Seguir a categoria (' + (d._natureza_efetiva || 'Despesa') + ')';
          selNat.value = d._natureza || '';
          document.getElementById('modalBg').classList.add('show');
        }}
        function salvarNaturezaModal() {{
          if (!idAtualModal) return;
          const nat = document.getElementById('modalNatureza').value;
          const tr = document.querySelector('tr[data-id="' + idAtualModal + '"]');
          if (!tr) return;
          if (window.detalhes[idAtualModal]) window.detalhes[idAtualModal]._natureza = nat;
          salvar(idAtualModal, tr.querySelector('.cat-select'));
          // a natureza muda os totais do mes, entao recarrega os numeros
          setTimeout(() => window.location.reload(), 600);
        }}
        function toggleDuplicadaModal() {{
          if (!idAtualModal) return;
          const marcado = document.getElementById('modalDup').checked;
          const tr = document.querySelector('tr[data-id="' + idAtualModal + '"]');
          if (!tr) return;
          const dupCheck = tr.querySelector('.dup-check');
          dupCheck.checked = marcado;
          const obsInput = tr.querySelector('.obs-input');
          if (marcado && !obsInput.value.trim()) {{
            obsInput.value = DUPLICADA_OBS_PADRAO;
          }}
          salvar(idAtualModal, dupCheck);
        }}
        function fecharModal() {{
          document.getElementById('modalBg').classList.remove('show');
          idAtualModal = null;
        }}
        function excluirManual() {{
          if (!idAtualModal) return;
          const d = window.detalhes[idAtualModal] || {{}};
          if (!confirm('Excluir definitivamente este lançamento manual?\\n\\n' + (d.descricao || '') + '  ' + (d.valor || ''))) return;
          fetch('/api/lancamento-manual/' + idAtualModal, {{ method: 'DELETE' }})
            .then(r => r.json())
            .then(res => {{
              if (res.ok) {{ fecharModal(); window.location.reload(); }}
              else alert(res.erro || 'Não foi possível excluir.');
            }});
        }}
        function linhaClick(e, id) {{
          const tag = e.target.tagName;
          if (['SELECT','INPUT','OPTION','BUTTON'].includes(tag)) return;
          verDetalhes(id);
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
            natureza: (window.detalhes[id] || {{}})._natureza || '',
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
                  if (sel) {{ sel.style.borderColor = '#c23c34'; sel.style.background = '#fbeceb'; }}
                }});
                alert('Nao foi possivel confirmar: preencha os campos obrigatorios destacados em vermelho.');
              }}
              const s = document.getElementById('status-' + id);
              if (s) {{ s.classList.add('show'); setTimeout(() => s.classList.remove('show'), 1500); }}
            }}
          }});
          filaSalvar[id] = atual;
        }}
        function toggleFormManual() {{
          const f = document.getElementById('formManual');
          f.style.display = f.style.display === 'none' ? 'block' : 'none';
        }}
        function salvarManual(e) {{
          e.preventDefault();
          const statusEl = document.getElementById('manualStatus');
          statusEl.textContent = 'Salvando...';
          const payload = {{
            data: document.getElementById('manualData').value,
            descricao: document.getElementById('manualDescricao').value,
            direcao: document.getElementById('manualDirecao').value,
            valor: document.getElementById('manualValor').value,
            categoria: document.getElementById('manualCategoria').value
          }};
          fetch('/api/lancamento-manual', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
          }}).then(r => r.json()).then(d => {{
            if (d.ok) {{ window.location.reload(); }}
            else {{ statusEl.textContent = d.erro || 'Falha ao salvar'; }}
          }}).catch(() => {{ statusEl.textContent = 'Falha ao salvar'; }});
          return false;
        }}
        atualizarChipLabels();
      </script>
      <script type="application/json" data-detalhes>{json.dumps(detalhes_js)}</script>
    </body></html>
    """


@app.route("/api/lancamento-manual", methods=["POST"])
@login_required
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


@app.route("/api/lancamento-manual/<transacao_id>", methods=["DELETE"])
@login_required
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
    <html><head><title>Gerenciar Cartoes · Meu Dinheiro</title>{BASE_CSS}</head>
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
    <html><head><title>Gerenciar Dimensoes · Meu Dinheiro</title>{BASE_CSS}</head>
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
        (set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA)) - CATEGORIAS_NEUTRAS_PADRAO,
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
    <html><head><title>Regras Automaticas · Meu Dinheiro</title>{BASE_CSS}</head>
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

    base = f"FROM cartao.transacao t {JOIN_NATUREZA} WHERE COALESCE(t.duplicada, false) = false "

    cur.execute(
        f"SELECT t.categoria, SUM({VAL_DESPESA}) AS total {base} "
        f"AND to_char(t.data_transacao,'YYYY') = %s AND {NATUREZA_SQL} = 'despesa' "
        "AND t.categoria IS NOT NULL GROUP BY t.categoria;",
        (ano,),
    )
    anual_por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}

    mensal_por_cat = {}
    if eh_ano_atual:
        cur.execute(
            f"SELECT t.categoria, SUM({VAL_DESPESA}) AS total {base} "
            f"AND to_char(t.data_transacao,'YYYY-MM') = %s AND {NATUREZA_SQL} = 'despesa' "
            "AND t.categoria IS NOT NULL GROUP BY t.categoria;",
            (mes_atual_str,),
        )
        mensal_por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}

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

    # ---- tabela do DRE: um mes por linha, do mais recente para o mais antigo ----
    rec_ano = sum(m["receita"] for m in meses_dre.values())
    desp_ano = sum(m["despesa"] for m in meses_dre.values())
    inv_ano = sum(m["investimento"] + m["bem"] for m in meses_dre.values())
    resultado_ano = rec_ano - desp_ano

    def _cor(v):
        return "#1f8a53" if v >= 0 else "#c23c34"

    linhas_dre = []
    for mes_key in sorted(meses_dre, reverse=True):
        m = meses_dre[mes_key]
        res = m["receita"] - m["despesa"]
        inv = m["investimento"] + m["bem"]
        margem = (res / m["receita"] * 100) if m["receita"] else 0
        linhas_dre.append(
            f'<tr>'
            f'<td>{MESES_ABREV[int(mes_key[5:7]) - 1]}/{mes_key[2:4]}</td>'
            f'<td class="valor" style="color:#1f8a53">{_fmt_moeda(m["receita"])}</td>'
            f'<td class="valor" style="color:#c23c34">{_fmt_moeda(m["despesa"])}</td>'
            f'<td class="valor" style="color:{_cor(res)};font-weight:600">{_fmt_moeda(res)}</td>'
            f'<td class="valor" style="color:#5c5f66">{("%.0f%%" % margem) if m["receita"] else "-"}</td>'
            f'<td class="valor" style="color:#5c5f66">{_fmt_moeda(inv) if inv else "-"}</td>'
            f'</tr>'
        )
    corpo_dre = "".join(linhas_dre) or '<tr><td colspan="6" style="padding:18px;text-align:center;color:#888">Sem lançamentos neste ano.</td></tr>'

    return f"""
    <html><head><title>DRE / Centro de Custos · Meu Dinheiro</title>{BASE_CSS}</head>
    <body>
      {topbar_html('DRE / Centro de Custos', 'dre')}
      <div class="wrap">
        <div class="filters">
          <div>
            <label>Ano</label>
            <select onchange="window.location='/dre?ano='+this.value">{anos_opcoes}</select>
          </div>
        </div>

        <div class="cards">
          <div class="card"><div class="label">Receitas do ano</div><div class="val" style="color:#1f8a53">{_fmt_moeda(rec_ano)}</div></div>
          <div class="card"><div class="label">Despesas do ano</div><div class="val" style="color:#c23c34">{_fmt_moeda(desp_ano)}</div></div>
          <div class="card"><div class="label">Resultado do ano</div><div class="val" style="color:{_cor(resultado_ano)}">{_fmt_moeda(resultado_ano)}</div></div>
          <div class="card"><div class="label">Investido / bens</div><div class="val" style="color:#5c5f66">{_fmt_moeda(inv_ano)}</div></div>
        </div>

        <div class="cat-breakdown">
          <h3>Resultado mês a mês</h3>
          <div style="font-size:12px;color:var(--ink-soft);margin-bottom:12px;line-height:1.6">
            <strong>Resultado = Receitas − Despesas.</strong> Investimentos, compra de bens (terreno, veículo),
            pagamento de fatura e transferências entre contas próprias <strong>não são despesa</strong> —
            não empobrecem, apenas mudam a forma do patrimônio. Por isso ficam de fora do resultado e
            aparecem na última coluna. Já os juros e tarifas <strong>são despesa</strong>, porque o dinheiro sai e não volta.
          </div>
          <table class="compacta">
            <thead><tr>
              <th>Mês</th>
              <th style="text-align:right">Receitas</th>
              <th style="text-align:right">Despesas</th>
              <th style="text-align:right">Resultado</th>
              <th style="text-align:right" title="Quanto sobrou de cada R$ 100 recebidos">Margem</th>
              <th style="text-align:right" title="Aplicações financeiras e compra de bens no mês">Investido / bens</th>
            </tr></thead>
            <tbody>{corpo_dre}</tbody>
          </table>
        </div>

        <div style="font-size:13px;color:var(--ink-soft);margin:22px 0 10px 0">
          <strong>Despesas por centro de custo</strong> — abaixo, só o que é consumo de fato.
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
        (set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA)) - CATEGORIAS_NEUTRAS_PADRAO,
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
    <html><head><title>Gerenciar Grupos de Custo · Meu Dinheiro</title>{BASE_CSS}</head>
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


def _montar_filtro_relatorio(dimensoes):
    """Le os filtros da querystring (request.args) e monta where/params/group_expr reutilizaveis
    tanto pela pagina quanto pelos endpoints de dados (AJAX)."""
    categorias_sel = request.args.getlist("categoria")
    cartoes_sel = request.args.getlist("cartao")
    origens_sel = request.args.getlist("origem")
    data_ini = request.args.get("data_ini") or ""
    data_fim = request.args.get("data_fim") or ""
    agrupar = request.args.get("agrupar") or "categoria"
    dim_sel = {}
    for d in dimensoes:
        vals = request.args.getlist(f"dim_{d['id']}")
        if vals:
            dim_sel[d["id"]] = vals

    # visao do relatorio: o que estamos medindo. Por padrao, despesas (consumo real).
    # Investimentos, aquisicao de bens e transferencias NAO sao despesa - ver NATUREZAS.
    visao = request.args.get("visao") or "despesa"
    if visao not in ("despesa", "receita", "investimento", "tudo"):
        visao = "despesa"

    where = ["COALESCE(t.duplicada, false) = false"]
    params = []
    if visao == "despesa":
        where.append(NATUREZA_SQL + " = 'despesa'")
    elif visao == "receita":
        where.append(NATUREZA_SQL + " = 'receita'")
    elif visao == "investimento":
        where.append(NATUREZA_SQL + " IN ('investimento', 'bem')")
    else:  # tudo: mostra o fluxo de caixa completo, menos o que so troca de bolso
        where.append(NATUREZA_SQL + " <> 'transferencia'")

    if categorias_sel:
        where.append("t.categoria IN %s")
        params.append(tuple(categorias_sel))
    if cartoes_sel:
        where.append("t.numero_cartao_final IN %s")
        params.append(tuple(cartoes_sel))
    if origens_sel:
        where.append("t.account_id IN %s")
        params.append(tuple(origens_sel))
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
    elif agrupar == "origem":
        group_expr = "t.account_id::text"
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

    # valor somado conforme a visao: na visao de receita invertemos o sinal para
    # que entrada apareca positiva (VAL_DESPESA e positivo quando o dinheiro sai)
    soma_expr = f"-{VAL_DESPESA}" if visao == "receita" else VAL_DESPESA

    return {
        "categorias_sel": categorias_sel,
        "cartoes_sel": cartoes_sel,
        "origens_sel": origens_sel,
        "data_ini": data_ini,
        "data_fim": data_fim,
        "agrupar": agrupar,
        "visao": visao,
        "dim_sel": dim_sel,
        "where_sql": where_sql,
        "params": params,
        "join_extra": join_extra,
        "join_natureza": JOIN_NATUREZA,
        "group_expr": group_expr,
        "soma_expr": soma_expr,
    }


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

    cur.execute("SELECT DISTINCT categoria FROM cartao.transacao WHERE categoria IS NOT NULL;")
    categorias_db = {r["categoria"] for r in cur.fetchall()}
    todas_categorias = sorted(categorias_db | set(CATEGORIAS_EXTRA), key=lambda c: cat_pt(c).lower())

    cur.execute("SELECT DISTINCT numero_cartao_final FROM cartao.transacao WHERE numero_cartao_final IS NOT NULL;")
    finais_usados = sorted({r["numero_cartao_final"] for r in cur.fetchall()})

    contas_by_id, origem_opcoes = carregar_origens(cur)

    cfg = _montar_filtro_relatorio(dimensoes)
    cur.close()
    conn.close()

    chip_filter = chip_filter_html

    dims_filtros_html = "".join(
        chip_filter(f"dim_{d['id']}", d["nome"],
                    [(v["id"], v["nome"]) for v in valores_por_dim.get(d["id"], [])],
                    cfg["dim_sel"].get(d["id"], []))
        for d in dimensoes if valores_por_dim.get(d["id"])
    )

    cartao_opcoes = [(c["final4"], f'{c["prefixo"]} - final {c["final4"]}') for c in cartoes_cadastrados]
    registrados = {c["final4"] for c in cartoes_cadastrados}
    cartao_opcoes += [(f, f"final {f}") for f in finais_usados if f not in registrados]

    agrupar_opcoes = [("categoria", "Categoria"), ("origem", "Origem"), ("cartao", "Cartão"), ("mes", "Período (mês)")]
    agrupar_opcoes += [(f"dim_{d['id']}", d["nome"]) for d in dimensoes]
    agrupar_opcoes_html = "".join(
        f'<option value="{val}" {"selected" if val == cfg["agrupar"] else ""}>{label}</option>'
        for val, label in agrupar_opcoes
    )

    return f"""
    <html><head><title>Relatórios · Meu Dinheiro</title>{BASE_CSS}
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
    </head>
    <body>
      {topbar_html('Relatórios', 'relatorios')}
      <div class="wrap">
        <div style="font-size:12px;color:#888;margin-bottom:10px">
          Pagamento de fatura, transferência entre contas próprias, aplicações e compra de bens
          <strong>não são despesa</strong> — só trocam a forma do dinheiro. Por isso ficam fora da visão de Despesas
          (veja a <a href="/naturezas">classificação de naturezas</a>).
        </div>
        <div class="rel-filtros">
          <select name="visao" id="selVisao" class="chip-btn" style="border-radius:20px" onchange="aplicarFiltros()">
            <option value="despesa" {"selected" if cfg["visao"]=="despesa" else ""}>Despesas</option>
            <option value="receita" {"selected" if cfg["visao"]=="receita" else ""}>Receitas</option>
            <option value="investimento" {"selected" if cfg["visao"]=="investimento" else ""}>Investimentos e bens</option>
            <option value="tudo" {"selected" if cfg["visao"]=="tudo" else ""}>Tudo (fluxo de caixa)</option>
          </select>
          <select name="agrupar" id="selAgrupar" class="chip-btn" style="border-radius:20px" onchange="aplicarFiltros()">{agrupar_opcoes_html}</select>
          {chip_filter('origem', 'Origem', origem_opcoes, cfg["origens_sel"])}
          {chip_filter('categoria', 'Categoria', [(c, cat_pt(c)) for c in todas_categorias], cfg["categorias_sel"])}
          {chip_filter('cartao', 'Cartão', cartao_opcoes, cfg["cartoes_sel"])}
          {dims_filtros_html}
          <div class="rel-datewrap">
            <input type="date" name="data_ini" id="inputDataIni" value="{cfg["data_ini"]}" onchange="aplicarFiltros()">
            <span style="color:#bbb">–</span>
            <input type="date" name="data_fim" id="inputDataFim" value="{cfg["data_fim"]}" onchange="aplicarFiltros()">
          </div>
          <div class="rel-actions">
            <a href="/relatorios" class="chip-btn" style="text-decoration:none">Limpar tudo</a>
          </div>
        </div>
        <div class="chips-sel" id="chipsSel" style="margin:-6px 0 14px 0"></div>

        <div class="cards">
          <div class="card"><div class="label" id="labelTotal">Total no filtro</div><div class="val" id="totalGeral">-</div></div>
          <div class="card"><div class="label">Lançamentos</div><div class="val" id="qtdGeral">-</div></div>
        </div>

        <div class="chart-card">
          <h3 id="graficoTitulo">Gráfico</h3>
          <canvas id="chartGrupos" height="90"></canvas>
        </div>

        <div class="cat-breakdown">
          <h3>Totais agrupados <span style="font-weight:400;font-size:12px;color:#999">(clique em um grupo para ver os lançamentos)</span></h3>
          <div id="gruposCont"><div style="color:#888;padding:10px 0">Carregando...</div></div>
        </div>
      </div>

      <script>
        // ---- chip filters: dropdown com busca, checkbox toggle e navegacao por teclado ----
        function cfToggle(btn) {{
          const panel = btn.nextElementSibling;
          const abrir = !panel.classList.contains('show');
          document.querySelectorAll('.chip-panel.show').forEach(p => {{ if (p !== panel) p.classList.remove('show'); }});
          if (abrir) {{
            panel.classList.add('show');
            const search = panel.querySelector('.chip-search');
            if (search) {{ search.value = ''; cfFiltrar(search); search.focus(); }}
          }} else {{
            panel.classList.remove('show');
          }}
        }}
        document.addEventListener('click', function(e) {{
          if (!e.target.closest('.chipfilter')) {{
            document.querySelectorAll('.chip-panel.show').forEach(p => p.classList.remove('show'));
          }}
        }});
        function cfClear(e, btn) {{
          e.stopPropagation();
          const panel = btn.closest('.chipfilter').querySelector('.chip-panel');
          panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
          aplicarFiltros();
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
              aplicarFiltros();
            }}
          }} else if (e.key === 'Escape') {{
            panel.classList.remove('show');
          }}
        }}

        // ---- filtros aplicados em tempo real via AJAX (o dropdown nao fecha) ----
        function fmtMoeda(v) {{
          return 'R$ ' + Number(v).toLocaleString('pt-BR', {{minimumFractionDigits:2, maximumFractionDigits:2}});
        }}
        function atualizarChipLabels() {{
          document.querySelectorAll('.chipfilter').forEach(cf => {{
            const btn = cf.querySelector('.chip-btn');
            const label = btn.dataset.label;
            const n = cf.querySelectorAll('input[type=checkbox]:checked').length;
            btn.classList.toggle('ativo', n > 0);
            btn.innerHTML = '<span class="chip-plus">+</span> ' + label + (n ? ' (' + n + ')' : '') +
              (n ? '<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' : '');
          }});
          // chips pequenos mostrando tudo que esta selecionado
          const cont = document.getElementById('chipsSel');
          if (cont) {{
            const marcados = Array.from(document.querySelectorAll('.chipfilter input[type=checkbox]:checked'));
            cont.innerHTML = marcados.map(cb => {{
              const lbl = cb.closest('.chip-opt');
              const curto = lbl.dataset.curto || lbl.textContent.trim();
              const completo = lbl.getAttribute('data-tip') || curto;
              return '<span class="chip-tag" title="' + completo + '"><span>' + curto + '</span>' +
                     '<b onclick="desmarcarFiltro(\\'' + cb.name + '\\', \\'' + cb.value + '\\')">&times;</b></span>';
            }}).join('');
          }}
        }}
        function desmarcarFiltro(nome, valor) {{
          const cb = document.querySelector('.chipfilter input[name="' + nome + '"][value="' + valor + '"]');
          if (cb) {{ cb.checked = false; aplicarFiltros(); }}
        }}
        function coletarQuery() {{
          const params = new URLSearchParams();
          params.set('visao', document.getElementById('selVisao').value);
          params.set('agrupar', document.getElementById('selAgrupar').value);
          document.querySelectorAll('.chip-opt input[type=checkbox]:checked').forEach(cb => params.append(cb.name, cb.value));
          const di = document.getElementById('inputDataIni').value;
          const df = document.getElementById('inputDataFim').value;
          if (di) params.set('data_ini', di);
          if (df) params.set('data_fim', df);
          return params;
        }}
        function aplicarFiltros() {{
          atualizarChipLabels();
          const params = coletarQuery();
          history.replaceState(null, '', '/relatorios?' + params.toString());
          carregarDados(params);
        }}
        function carregarDados(params) {{
          fetch('/relatorios/dados?' + params.toString()).then(r => r.json()).then(renderResultado);
        }}
        const LABEL_VISAO = {{ despesa: 'Total de despesas', receita: 'Total de receitas',
                              investimento: 'Investido / adquirido', tudo: 'Fluxo de caixa (líquido)' }};
        function renderResultado(data) {{
          document.getElementById('totalGeral').textContent = fmtMoeda(data.total_geral);
          document.getElementById('labelTotal').textContent = LABEL_VISAO[data.visao] || 'Total no filtro';
          document.getElementById('qtdGeral').textContent = data.qtd_geral;
          const ehPeriodo = data.agrupar === 'mes';
          document.getElementById('graficoTitulo').textContent =
            ehPeriodo ? 'Evolução mês a mês' : 'Gráfico (' + data.agrupar_label + ')';
          renderGrupos(data.grupos, ehPeriodo);
          renderChart(data.grupos, ehPeriodo);
        }}

        // ---- lista de totais agrupados, clicavel para ver os lancamentos de cada grupo ----
        window.__grupos = [];
        function renderGrupos(grupos, ehPeriodo) {{
          // o grafico fica na ordem cronologica (linha do tempo); ja a lista abaixo
          // mostra o mes mais recente no topo, que e o que se quer olhar primeiro
          const lista = ehPeriodo ? grupos.slice().reverse() : grupos;
          window.__grupos = lista;
          const cont = document.getElementById('gruposCont');
          if (!lista.length) {{
            cont.innerHTML = '<div style="color:#888;padding:10px 0">Nenhum lancamento encontrado com esses filtros.</div>';
            return;
          }}
          // na linha do tempo a barra fica proporcional ao maior mes (fica legivel),
          // e mostramos a variacao em relacao ao mes anterior
          const maxTotal = Math.max.apply(null, lista.map(g => Math.abs(g.total)).concat([1]));
          cont.innerHTML = lista.map((g, i) => {{
            const larguraBarra = ehPeriodo ? (Math.abs(g.total) / maxTotal * 100) : Math.max(g.pct, 0);
            let direita = '<strong>' + fmtMoeda(g.total) + '</strong> <span style="color:#aaa">' + g.pct + '%</span>';
            // lista invertida: o mes anterior e o de baixo (i + 1)
            if (ehPeriodo && i < lista.length - 1) {{
              const ant = lista[i + 1].total;
              if (ant) {{
                const varPct = (g.total - ant) / Math.abs(ant) * 100;
                const cor = varPct > 0 ? 'var(--bad)' : 'var(--good)';
                const sinal = varPct > 0 ? '▲' : '▼';
                direita = '<strong>' + fmtMoeda(g.total) + '</strong> ' +
                          '<span style="color:' + cor + ';font-size:12px" title="variação em relação ao mês anterior">' +
                          sinal + ' ' + Math.abs(varPct).toFixed(1) + '%</span>';
              }}
            }}
            return '<div>' +
              '<div class="rel-grupo-row" style="cursor:pointer" onclick="toggleGrupoDetalhe(' + i + ')">' +
                '<div style="flex:1">' +
                  '<div style="display:flex;justify-content:space-between">' +
                    '<span>' + (g.selo || '') + g.nome + ' <span style="color:#aaa">(' + g.qtd + ')</span></span>' +
                    '<span>' + direita + '</span>' +
                  '</div>' +
                  '<div class="barra"><div style="width:' + larguraBarra + '%"></div></div>' +
                '</div>' +
              '</div>' +
              '<div class="rel-grupo-detalhe" id="grupoDetalhe' + i + '" style="display:none"></div>' +
            '</div>';
          }}).join('');
        }}
        function toggleGrupoDetalhe(i) {{
          const el = document.getElementById('grupoDetalhe' + i);
          const abrir = el.style.display === 'none';
          document.querySelectorAll('.rel-grupo-detalhe').forEach(d => {{ if (d !== el) d.style.display = 'none'; }});
          if (!abrir) {{ el.style.display = 'none'; return; }}
          el.style.display = 'block';
          if (el.dataset.loaded === '1') return;
          el.innerHTML = '<div style="padding:10px;color:#888;font-size:13px">Carregando...</div>';
          const g = window.__grupos[i];
          const params = coletarQuery();
          if (g.valor === null || g.valor === undefined) {{ params.set('valor_none', '1'); }}
          else {{ params.set('valor', g.valor); }}
          fetch('/relatorios/lancamentos?' + params.toString())
            .then(r => r.json())
            .then(data => {{
              el.dataset.loaded = '1';
              if (!data.lancamentos.length) {{
                el.innerHTML = '<div style="padding:10px;color:#888;font-size:13px">Nenhum lancamento.</div>';
                return;
              }}
              el.innerHTML = '<table class="rel-mini-table"><thead><tr><th>Data</th><th>Descrição</th><th>Origem</th><th>Categoria</th><th>Valor</th></tr></thead><tbody>' +
                data.lancamentos.map(l => (
                  '<tr><td>' + l.data + '</td><td>' + l.descricao + '</td>' +
                  '<td data-tip="' + (l.origem_completa || '') + '">' + l.origem + '</td><td>' + l.categoria + '</td>' +
                  '<td class="valor">' + fmtMoeda(l.valor) + '</td></tr>'
                )).join('') +
                '</tbody></table>' +
                (data.total >= 300 ? '<div style="padding:8px 10px;color:#999;font-size:12px">Mostrando os 300 lancamentos mais recentes deste grupo.</div>' : '');
            }});
        }}

        // ---- grafico dinamico conforme os filtros aplicados ----
        let chartInstance = null;
        let chartTipoAtual = null;
        function renderChart(grupos, ehPeriodo) {{
          if (!window.Chart) return;
          const labels = grupos.map(g => g.nome);
          const valores = grupos.map(g => g.total);
          // linha do tempo (mes a mes) fica melhor como linha; os demais, como barras
          const tipo = ehPeriodo ? 'line' : 'bar';
          const dataset = ehPeriodo
            ? {{ label: 'Total (R$)', data: valores, borderColor: '#2e6fd6', backgroundColor: 'rgba(46,111,214,.12)',
                 fill: true, tension: .3, pointRadius: 4, pointHoverRadius: 6, pointBackgroundColor: '#2e6fd6', borderWidth: 2 }}
            : {{ label: 'Total (R$)', data: valores, backgroundColor: '#2e6fd6', borderRadius: 4, maxBarThickness: 46 }};

          if (chartInstance && chartTipoAtual === tipo) {{
            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0] = dataset;
            chartInstance.update();
            return;
          }}
          if (chartInstance) chartInstance.destroy();
          chartTipoAtual = tipo;
          chartInstance = new Chart(document.getElementById('chartGrupos'), {{
            type: tipo,
            data: {{ labels: labels, datasets: [dataset] }},
            options: {{
              responsive: true,
              plugins: {{
                legend: {{ display: false }},
                tooltip: {{ callbacks: {{ label: c => fmtMoeda(c.parsed.y) }} }}
              }},
              scales: {{
                y: {{ beginAtZero: true, ticks: {{ callback: v => 'R$ ' + Number(v).toLocaleString('pt-BR') }} }}
              }}
            }}
          }});
        }}

        document.addEventListener('DOMContentLoaded', function() {{
          carregarDados(new URLSearchParams(window.location.search));
        }});
      </script>
    </body></html>
    """


@app.route("/relatorios/dados")
@login_required
def relatorios_dados():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cfg = _montar_filtro_relatorio(dimensoes)

    # agrupando por periodo o resultado e uma linha do tempo: ordena cronologicamente
    # (do mais antigo para o mais recente). Nos demais agrupamentos, maior valor primeiro.
    ordem = f"{cfg['group_expr']} ASC" if cfg["agrupar"] == "mes" else "total DESC"
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
            return cat_pt(g)
        if cfg["agrupar"] == "cartao":
            if not g:
                return "(sem cartao)"
            prefixo = nomes_cartao.get(g)
            return f"{prefixo} - final {g}" if prefixo else f"final {g}"
        if cfg["agrupar"] == "origem":
            c = contas_by_id.get(str(g))
            return c["label"] if c else "(sem origem)"
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


@app.route("/relatorios/lancamentos")
@login_required
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
        c = contas_by_id.get(str(r["account_id"]))
        if not c:
            return "-", "-"
        # se for cartao de credito e tiver apelido cadastrado, o apelido e mais informativo
        if c["tipo"] == "CREDIT" and r["numero_cartao_final"] and nomes_cartao.get(r["numero_cartao_final"]):
            return c["selo"] + nomes_cartao[r["numero_cartao_final"]], f'{c["label"]} - {nome_cartao_curto(r["numero_cartao_final"])}'
        return c["selo"] + c["label_curto"], c["label"]

    lancamentos = []
    for r in rows:
        curto, completo = origem_de(r)
        lancamentos.append({
            "data": (r["data_transacao"] - timedelta(hours=3)).strftime("%d/%m/%Y"),
            "descricao": r["descricao"],
            "origem": curto,
            "origem_completa": completo,
            "cartao": nome_cartao_curto(r["numero_cartao_final"]),
            "categoria": cat_pt(r["categoria"]),
            "valor": float(r["valor"] or 0),
        })
    return jsonify({"lancamentos": lancamentos, "total": len(lancamentos)})


def _ler_arquivo_importacao(arquivo):
    """Devolve (linhas, erro). Detecta OFX ou CSV pelo conteudo/extensao."""
    raw = arquivo.read()
    if not raw:
        return None, "Arquivo vazio."
    if len(raw) > 8 * 1024 * 1024:
        return None, "Arquivo muito grande (limite de 8 MB)."
    texto = _decodificar(raw)
    nome = (arquivo.filename or "").lower()
    if "<STMTTRN>" in texto.upper() or nome.endswith((".ofx", ".qfx")):
        linhas = parse_ofx(texto)
        if not linhas:
            return None, "Não encontrei transações no OFX. O arquivo pode estar em outro formato."
        return linhas, None
    return parse_csv(texto)


@app.route("/api/importar/preview", methods=["POST"])
@login_required
def importar_preview():
    arquivo = request.files.get("arquivo")
    account_id = request.form.get("origem")
    inverter = request.form.get("inverter") == "1"
    if not arquivo or not account_id:
        return jsonify({"ok": False, "erro": "Escolha o arquivo e a origem."}), 400

    linhas, erro = _ler_arquivo_importacao(arquivo)
    if erro:
        return jsonify({"ok": False, "erro": erro}), 400

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tipo FROM cartao.conta WHERE account_id = %s;", (account_id,))
    conta = cur.fetchone()
    if not conta:
        cur.close()
        conn.close()
        return jsonify({"ok": False, "erro": "Origem inválida."}), 400

    linhas = normalizar_para_conta(linhas, conta["tipo"], inverter)
    datas = [l["data"] for l in linhas]

    # o que ja existe no periodo, para marcar duplicados (mesma data + mesmo valor)
    cur.execute(
        "SELECT to_char(data_transacao, 'YYYY-MM-DD') AS d, "
        "ROUND(COALESCE(valor_brl, valor_original), 2) AS v, descricao "
        "FROM cartao.transacao WHERE account_id = %s AND data_transacao::date BETWEEN %s AND %s;",
        (account_id, min(datas), max(datas)),
    )
    existentes = {}
    for r in cur.fetchall():
        existentes.setdefault((r["d"], float(r["v"])), []).append(r["descricao"] or "")
    cur.close()
    conn.close()

    itens = []
    for l in linhas:
        chave = (l["data"].isoformat(), float(l["valor"]))
        ja_tem = existentes.get(chave)
        itens.append({
            "data": l["data"].isoformat(),
            "data_fmt": l["data"].strftime("%d/%m/%Y"),
            "descricao": l["descricao"] or "(sem descrição)",
            "valor": l["valor"],
            "tipo": l["tipo"],
            "fitid": l.get("fitid") or "",
            "duplicado": bool(ja_tem),
            "ja_existe_como": (ja_tem[0][:60] if ja_tem else ""),
        })
    itens.sort(key=lambda i: i["data"])
    novos = sum(1 for i in itens if not i["duplicado"])
    return jsonify({
        "ok": True,
        "itens": itens,
        "total": len(itens),
        "novos": novos,
        "duplicados": len(itens) - novos,
        "periodo": f'{itens[0]["data_fmt"]} a {itens[-1]["data_fmt"]}' if itens else "-",
    })


@app.route("/api/importar/confirmar", methods=["POST"])
@login_required
def importar_confirmar():
    dados = request.get_json(force=True)
    account_id = dados.get("origem")
    itens = dados.get("itens") or []
    if not account_id or not itens:
        return jsonify({"ok": False, "erro": "Nada para importar."}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        inseridos = 0
        for it in itens:
            data = _data_br(it.get("data"))
            valor = float(it.get("valor"))
            desc = (it.get("descricao") or "").strip()[:300]
            if not data:
                continue
            # id estavel: reimportar o mesmo arquivo nao duplica
            semente = it.get("fitid") or f'{data.isoformat()}|{valor}|{desc}'
            tid = str(uuid.uuid5(IMPORT_NAMESPACE, f"{account_id}|{semente}"))
            cur.execute(
                "INSERT INTO cartao.transacao ("
                "transacao_id, account_id, descricao, descricao_bruta, valor_original, moeda_original, "
                "valor_brl, data_transacao, status, tipo, importado, criado_em, atualizado_em, sincronizado_em"
                ") VALUES (%s,%s,%s,%s,%s,'BRL',%s,%s,'POSTED',%s, true, now(), now(), now()) "
                "ON CONFLICT (transacao_id) DO UPDATE SET importado = true "
                "RETURNING (xmax = 0) AS novo;",
                (tid, account_id, desc, desc, valor, valor,
                 f"{data.isoformat()} 12:00:00-03:00", it.get("tipo") or "DEBIT"),
            )
            if cur.fetchone()[0]:
                inseridos += 1
        conn.commit()
        # aplica as regras de classificacao automatica nos recem-importados
        cur2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        aplicar_regras(cur2)
        conn.commit()
        cur2.close()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "inseridos": inseridos, "ignorados": len(itens) - inseridos})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400


@app.route("/importar")
@login_required
def importar_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _, origem_opcoes = carregar_origens(cur)
    cur.close()
    conn.close()

    origem_options = "".join(
        f'<option value="{val}">{completo}</option>' for val, _curto, completo in origem_opcoes
    )

    return f"""
    <html><head><title>Importar extrato / fatura · Meu Dinheiro</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Importar extrato / fatura', 'importar')}
      <div class="wrap">
        <div class="cat-breakdown">
          <h3>1. Escolha o arquivo</h3>
          <div style="font-size:13px;color:var(--ink-soft);margin-bottom:14px;line-height:1.6">
            Aceita <strong>OFX</strong> (o mais confiável) ou <strong>CSV</strong> exportado do internet banking.
            Use isto para completar períodos que o Pluggy não traz mais — a API dele devolve
            no máximo os 500 lançamentos mais recentes de cada conta.<br>
            Nada é gravado antes de você conferir a prévia, e reimportar o mesmo arquivo não duplica.
          </div>
          <form id="formImport" onsubmit="return enviarPreview(event)" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <input type="file" id="arquivo" accept=".ofx,.qfx,.csv,.txt" required
                   style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--surface)">
            <select id="origem" required style="padding:8px 10px">{origem_options}</select>
            <label style="display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--ink-soft)">
              <input type="checkbox" id="inverter" style="width:15px;height:15px;accent-color:var(--accent)">
              Inverter sinal (+/−)
            </label>
            <button type="submit">Ver prévia</button>
          </form>
          <div id="msg" style="margin-top:12px;font-size:13px"></div>
        </div>

        <div id="blocoPreview" style="display:none">
          <div class="cards" id="resumoPreview"></div>
          <div class="cat-breakdown">
            <h3>2. Confira e importe</h3>
            <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:10px">
              Linhas marcadas como <strong>já existe</strong> vêm desmarcadas — são lançamentos que já estão no
              sistema com a mesma data e o mesmo valor. Se o sinal estiver invertido (despesa como entrada),
              marque "Inverter sinal" e gere a prévia de novo.
            </div>
            <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
              <button type="button" class="ver-btn" onclick="marcarTodos(true)">Marcar todos</button>
              <button type="button" class="ver-btn" onclick="marcarTodos(false)">Desmarcar todos</button>
              <button type="button" class="ver-btn" onclick="marcarSomenteNovos()">Só os novos</button>
              <button type="button" id="btnImportar" onclick="confirmarImport()" style="margin-left:auto">Importar selecionados</button>
            </div>
            <table class="compacta">
              <thead><tr>
                <th class="cel-check">Imp</th><th class="cel-data">Data</th><th class="cel-desc">Descrição</th>
                <th class="cel-valor" style="text-align:right">Valor</th><th class="cel-origem">Situação</th>
              </tr></thead>
              <tbody id="corpoPreview"></tbody>
            </table>
          </div>
        </div>
      </div>

      <script>
        let itensPreview = [];
        function fmtMoeda(v) {{
          return 'R$ ' + Number(v).toLocaleString('pt-BR', {{minimumFractionDigits:2, maximumFractionDigits:2}});
        }}
        function enviarPreview(e) {{
          e.preventDefault();
          const arq = document.getElementById('arquivo').files[0];
          if (!arq) return false;
          const msg = document.getElementById('msg');
          msg.textContent = 'Lendo arquivo...';
          msg.style.color = 'var(--ink-soft)';
          const fd = new FormData();
          fd.append('arquivo', arq);
          fd.append('origem', document.getElementById('origem').value);
          fd.append('inverter', document.getElementById('inverter').checked ? '1' : '0');
          fetch('/api/importar/preview', {{ method: 'POST', body: fd }})
            .then(r => r.json())
            .then(d => {{
              if (!d.ok) {{ msg.textContent = d.erro; msg.style.color = 'var(--bad)'; return; }}
              msg.textContent = d.total + ' lançamentos lidos (' + d.periodo + ').';
              msg.style.color = 'var(--good)';
              itensPreview = d.itens;
              renderPreview(d);
            }})
            .catch(() => {{ msg.textContent = 'Falha ao ler o arquivo.'; msg.style.color = 'var(--bad)'; }});
          return false;
        }}
        function renderPreview(d) {{
          document.getElementById('resumoPreview').innerHTML =
            '<div class="card"><div class="label">Lidos</div><div class="val">' + d.total + '</div></div>' +
            '<div class="card"><div class="label">Novos</div><div class="val" style="color:var(--good)">' + d.novos + '</div></div>' +
            '<div class="card"><div class="label">Já existem</div><div class="val" style="color:var(--ink-faint)">' + d.duplicados + '</div></div>' +
            '<div class="card"><div class="label">Período</div><div class="val" style="font-size:15px">' + d.periodo + '</div></div>';
          document.getElementById('corpoPreview').innerHTML = d.itens.map((it, i) => (
            '<tr' + (it.duplicado ? ' style="color:var(--ink-faint)"' : '') + '>' +
              '<td class="cel-check"><input type="checkbox" data-i="' + i + '"' + (it.duplicado ? '' : ' checked') + '></td>' +
              '<td class="cel-data">' + it.data_fmt + '</td>' +
              '<td class="cel-desc" title="' + it.descricao.replace(/"/g, '&quot;') + '">' + it.descricao + '</td>' +
              '<td class="valor cel-valor" style="' + (it.valor < 0 ? 'color:var(--good)' : '') + '">' + fmtMoeda(it.valor) + '</td>' +
              '<td class="cel-origem">' + (it.duplicado ? 'já existe' : 'novo') + '</td>' +
            '</tr>'
          )).join('');
          document.getElementById('blocoPreview').style.display = 'block';
        }}
        function marcarTodos(v) {{
          document.querySelectorAll('#corpoPreview input[type=checkbox]').forEach(cb => cb.checked = v);
        }}
        function marcarSomenteNovos() {{
          document.querySelectorAll('#corpoPreview input[type=checkbox]').forEach(cb => {{
            cb.checked = !itensPreview[cb.dataset.i].duplicado;
          }});
        }}
        function confirmarImport() {{
          const sel = Array.from(document.querySelectorAll('#corpoPreview input[type=checkbox]:checked'))
                           .map(cb => itensPreview[cb.dataset.i]);
          if (!sel.length) {{ alert('Nenhuma linha selecionada.'); return; }}
          if (!confirm('Importar ' + sel.length + ' lançamento(s)?')) return;
          const btn = document.getElementById('btnImportar');
          btn.disabled = true; btn.textContent = 'Importando...';
          fetch('/api/importar/confirmar', {{
            method: 'POST', headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ origem: document.getElementById('origem').value, itens: sel }})
          }}).then(r => r.json()).then(d => {{
            btn.disabled = false; btn.textContent = 'Importar selecionados';
            if (!d.ok) {{ alert(d.erro || 'Falha ao importar.'); return; }}
            const msg = document.getElementById('msg');
            msg.style.color = 'var(--good)';
            msg.textContent = d.inseridos + ' lançamento(s) importado(s).' +
              (d.ignorados ? ' ' + d.ignorados + ' já existiam e foram ignorados.' : '');
            document.getElementById('blocoPreview').style.display = 'none';
          }}).catch(() => {{
            btn.disabled = false; btn.textContent = 'Importar selecionados';
            alert('Falha ao importar.');
          }});
        }}
      </script>
    </body></html>
    """


@app.route("/investimentos")
@login_required
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

    if posicoes is None:
        return f"""
        <html><head><title>Investimentos · Meu Dinheiro</title>{BASE_CSS}</head>
        <body>
          {topbar_html('Investimentos', 'investimentos')}
          <div class="wrap"><div class="cat-breakdown">
            <h3>Ainda não sincronizado</h3>
            <div style="font-size:13px;color:var(--ink-soft)">
              Os investimentos são carregados na próxima sincronização com o Pluggy.
              Clique em <strong>Atualizar agora</strong> no topo e recarregue esta página.
            </div>
          </div></div>
        </body></html>
        """

    ativos = [p for p in posicoes if float(p["saldo"] or 0) > 0]
    encerrados = len(posicoes) - len(ativos)

    saldo_total = sum(float(p["saldo"] or 0) for p in ativos)
    bruto_total = sum(float(p["valor_bruto"] or 0) for p in ativos)
    aplicado_total = sum(float(p["valor_aplicado"] or 0) for p in ativos)
    ir_total = sum(float(p["impostos"] or 0) for p in ativos)
    rendimento_bruto = bruto_total - aplicado_total
    rend_pct = (rendimento_bruto / aplicado_total * 100) if aplicado_total else 0

    def _dt(v):
        return v.strftime("%d/%m/%Y") if v else "-"

    linhas = []
    for p in ativos:
        aplicado = float(p["valor_aplicado"] or 0)
        bruto = float(p["valor_bruto"] or 0)
        rend = bruto - aplicado
        pct = (rend / aplicado * 100) if aplicado else 0
        taxa = ""
        if p["taxa"] and float(p["taxa"]) > 0:
            taxa = f'{float(p["taxa"]):g}% {p["tipo_taxa"] or ""}'.strip()
        linhas.append(
            f'<tr>'
            f'<td>{(p["nome"] or "-")[:46]}<div style="font-size:11px;color:var(--ink-faint)">'
            f'{p["subtipo"] or p["tipo"] or ""}{" · " + taxa if taxa else ""}</div></td>'
            f'<td class="valor">{_fmt_moeda(aplicado)}</td>'
            f'<td class="valor">{_fmt_moeda(bruto)}</td>'
            f'<td class="valor" style="color:#1f8a53">{_fmt_moeda(rend)}<div style="font-size:11px">{pct:.1f}%</div></td>'
            f'<td class="valor" style="color:var(--ink-faint)">{_fmt_moeda(float(p["impostos"] or 0))}</td>'
            f'<td class="valor" style="font-weight:600">{_fmt_moeda(float(p["saldo"] or 0))}</td>'
            f'<td style="font-size:11.5px;color:var(--ink-soft)">{_dt(p["data_vencimento"])}</td>'
            f'</tr>'
        )
    corpo = "".join(linhas) or '<tr><td colspan="7" style="padding:18px;text-align:center;color:#888">Nenhum investimento com saldo.</td></tr>'

    # evolucao mes a mes (so a partir do momento em que passamos a guardar o retrato)
    linhas_hist = []
    anterior = None
    for h in reversed(historico):
        saldo = float(h["saldo"] or 0)
        aplicado = float(h["aplicado"] or 0)
        variacao = ""
        if anterior is not None:
            dif = anterior - saldo
            cor = "#1f8a53" if dif >= 0 else "#c23c34"
            variacao = f'<span style="color:{cor}">{_fmt_moeda(dif)}</span>'
        mes = h["mes"]
        linhas_hist.append(
            f'<tr><td>{MESES_ABREV[int(mes[5:7]) - 1]}/{mes[2:4]}</td>'
            f'<td class="valor">{_fmt_moeda(aplicado)}</td>'
            f'<td class="valor">{_fmt_moeda(saldo)}</td>'
            f'<td class="valor">{variacao or "-"}</td></tr>'
        )
        anterior = saldo
    bloco_hist = ""
    if len(historico) > 1:
        bloco_hist = f"""
        <div class="cat-breakdown">
          <h3>Evolução do saldo</h3>
          <table class="compacta">
            <thead><tr><th>Mês</th><th style="text-align:right">Aplicado</th>
            <th style="text-align:right">Saldo</th><th style="text-align:right">Variação</th></tr></thead>
            <tbody>{"".join(linhas_hist)}</tbody>
          </table>
        </div>"""
    else:
        bloco_hist = """
        <div class="cat-breakdown">
          <h3>Evolução do saldo</h3>
          <div style="font-size:13px;color:var(--ink-soft)">
            O Pluggy devolve apenas a posição de hoje, sem histórico. A partir de agora o app
            guarda um retrato do saldo a cada sincronização, então a evolução mês a mês
            começa a aparecer no próximo mês.
          </div>
        </div>"""

    return f"""
    <html><head><title>Investimentos · Meu Dinheiro</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Investimentos', 'investimentos')}
      <div class="wrap">
        <div class="cards">
          <div class="card"><div class="label">Saldo líquido</div><div class="val">{_fmt_moeda(saldo_total)}</div></div>
          <div class="card"><div class="label">Valor aplicado</div><div class="val" style="font-size:19px">{_fmt_moeda(aplicado_total)}</div></div>
          <div class="card"><div class="label">Rendimento bruto</div><div class="val" style="color:#1f8a53;font-size:19px">{_fmt_moeda(rendimento_bruto)}</div><div class="sub">{rend_pct:.1f}% sobre o aplicado</div></div>
          <div class="card"><div class="label">IR a recolher</div><div class="val" style="color:#c23c34;font-size:19px">{_fmt_moeda(ir_total)}</div></div>
          <div class="card"><div class="label">Aplicações ativas</div><div class="val">{len(ativos)}</div><div class="sub">{encerrados} encerradas</div></div>
        </div>

        <div class="cat-breakdown">
          <h3>Posição por aplicação</h3>
          <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:12px;line-height:1.6">
            O <strong>saldo é patrimônio</strong>, não entra no DRE — aplicar e resgatar só muda a forma do dinheiro.
            O que entra no resultado é o <strong>rendimento</strong> (receita financeira) e o <strong>IR</strong> (despesa financeira).
          </div>
          <table class="compacta">
            <thead><tr>
              <th>Aplicação</th>
              <th style="text-align:right">Aplicado</th>
              <th style="text-align:right">Bruto hoje</th>
              <th style="text-align:right">Rendimento</th>
              <th style="text-align:right">IR</th>
              <th style="text-align:right">Saldo líquido</th>
              <th>Vencimento</th>
            </tr></thead>
            <tbody>{corpo}</tbody>
          </table>
        </div>

        {bloco_hist}
      </div>
    </body></html>
    """


@app.route("/naturezas", methods=["GET", "POST"])
@login_required
def naturezas_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        categoria = request.form.get("categoria")
        natureza = request.form.get("natureza")
        if categoria and natureza in NATUREZAS:
            cur.execute(
                "INSERT INTO cartao.categoria_natureza (categoria, natureza) VALUES (%s,%s) "
                "ON CONFLICT (categoria) DO UPDATE SET natureza = EXCLUDED.natureza;",
                (categoria, natureza),
            )
            conn.commit()

    cur.execute("SELECT categoria, natureza FROM cartao.categoria_natureza;")
    atual = {r["categoria"]: r["natureza"] for r in cur.fetchall()}

    # so lista categorias que existem de fato nos lancamentos (+ as extras)
    cur.execute(
        f"SELECT t.categoria, COUNT(*) AS qtd, SUM({VAL_DESPESA}) AS total "
        f"FROM cartao.transacao t JOIN cartao.conta c ON c.account_id = t.account_id "
        "WHERE t.categoria IS NOT NULL AND COALESCE(t.duplicada, false) = false "
        "GROUP BY t.categoria;"
    )
    usadas = {r["categoria"]: r for r in cur.fetchall()}
    cur.close()
    conn.close()

    categorias = sorted(set(usadas) | set(CATEGORIAS_EXTRA) | set(atual), key=lambda c: cat_pt(c).lower())

    def linha(c):
        nat = atual.get(c, NATUREZA_PADRAO)
        info = usadas.get(c)
        qtd = info["qtd"] if info else 0
        total = float(info["total"] or 0) if info else 0.0
        opts = "".join(
            f'<option value="{k}" {"selected" if k == nat else ""}>{v}</option>'
            for k, v in NATUREZAS.items()
        )
        aviso = "" if nat == "despesa" else '<span style="color:var(--ink-faint);font-size:11px"> fora do resultado</span>' if nat in NATUREZAS_NEUTRAS else ""
        return (
            f'<tr>'
            f'<td>{cat_pt(c)}<div style="font-size:11px;color:var(--ink-faint)">{c}</div></td>'
            f'<td class="valor">{qtd or "-"}</td>'
            f'<td class="valor">{_fmt_moeda(total) if qtd else "-"}</td>'
            f'<td><form method="post" style="display:flex;gap:6px;align-items:center">'
            f'<input type="hidden" name="categoria" value="{c}">'
            f'<select name="natureza" onchange="this.form.submit()" style="padding:5px 7px;font-size:12px">{opts}</select>'
            f'{aviso}</form></td>'
            f'</tr>'
        )

    return f"""
    <html><head><title>Naturezas · Meu Dinheiro</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Natureza das categorias', 'naturezas')}
      <div class="wrap">
        <div class="cat-breakdown">
          <h3>Como cada categoria entra no DRE</h3>
          <div style="font-size:13px;color:var(--ink-soft);line-height:1.7">
            O DRE mede o <strong>resultado</strong> do período: Receitas − Despesas. Nem todo dinheiro que sai é despesa.
            <ul style="margin:8px 0 0 0;padding-left:18px">
              <li><strong>Receita</strong> — entra e aumenta seu patrimônio (salário, PIX recebido, depósitos).</li>
              <li><strong>Despesa</strong> — sai e não volta: consumo, juros, tarifas. É o que reduz o resultado.</li>
              <li><strong>Investimento</strong> — aplicação financeira, previdência. Você continua com o dinheiro, em outra forma. Não é despesa.</li>
              <li><strong>Aquisição de bem</strong> — terreno, veículo, imóvel. Troca de dinheiro por bem: não entra no resultado.
                  O que entraria é a depreciação (e terreno não deprecia).</li>
              <li><strong>Transferência</strong> — pagamento de fatura do cartão, movimentação entre contas próprias. Só troca de bolso.</li>
              <li><strong>Depende da direção</strong> — PIX, TED, dinheiro: o que entra vira receita, o que sai vira despesa.</li>
            </ul>
          </div>
        </div>
        <div class="cat-breakdown">
          <table class="compacta">
            <thead><tr>
              <th>Categoria</th><th style="text-align:right">Lanç.</th>
              <th style="text-align:right" title="Positivo = dinheiro saiu">Total</th><th>Natureza</th>
            </tr></thead>
            <tbody>{"".join(linha(c) for c in categorias)}</tbody>
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
