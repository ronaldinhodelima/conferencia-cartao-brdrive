import os
import csv
import functools
import hashlib
import html
import io
import json
import re
import secrets
import unicodedata
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Flask, request, redirect, session, jsonify, render_template

app = Flask(__name__)

# URL do serviço bussola-financeira-app que faz a sincronizacao com o Pluggy.
# Pode ser sobrescrita via env var caso o dominio mude.
BUSSOLA_SYNC_URL = os.environ.get(
    "BUSSOLA_SYNC_URL", "https://hdgffcvh3ljqe61dczztaycz.coolify.brdrive.net/sync"
)
app.secret_key = os.environ.get("SECRET_KEY", "troque-isto-em-producao")
# cookie de sessao so trafega por HTTPS (o Traefik do Coolify ja forca https) e
# nunca e enviado em navegacao cross-site - reduz roubo de sessao via rede ou CSRF.
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# usuarios iniciais (env). Servem apenas para criar os primeiros acessos:
# depois do primeiro boot os usuarios passam a viver na tabela cartao.usuario,
# com senha guardada em hash. Trocar a senha pela tela nao depende mais da env.
USERS = {
    login: senha
    for login, senha in (
        (os.environ.get("APP_USER_1", "ronaldo"), os.environ.get("APP_PASS_1")),
        (os.environ.get("APP_USER_2", "andrea"), os.environ.get("APP_PASS_2")),
    )
    if senha  # sem senha na env, essa conta de emergencia fica desativada
}

# ---------------------------------------------------------------------------
# Permissoes: cada acao do sistema que pode ser liberada ou bloqueada.
# ---------------------------------------------------------------------------
PERMISSOES = {
    "lancamentos_ver": ("Ver lançamentos", "Abrir a tela de lançamentos e consultar o que foi gasto."),
    "lancamentos_editar": ("Editar lançamentos", "Mudar categoria, responsável, projeto, observação e marcar duplicadas."),
    "lancamentos_conferir": ("Conferir lançamentos", "Marcar um lançamento como conferido."),
    "lancamentos_manual": ("Lançar dinheiro manual", "Criar e excluir lançamentos em espécie."),
    "importar": ("Importar extrato / fatura", "Subir arquivos OFX e CSV para completar períodos."),
    "relatorios": ("Ver relatórios", "Relatórios, DRE e investimentos."),
    "cadastros": ("Gerenciar cadastros", "Grupos de custo, dimensões, regras automáticas, cartões e naturezas."),
    "sincronizar": ("Sincronizar com o banco", "Usar o botão Atualizar agora."),
    "usuarios": ("Gerenciar usuários", "Criar usuários, trocar senhas e definir permissões."),
}

# perfis prontos - atalho para nao precisar marcar permissao por permissao
PERFIS = {
    "admin": ("Administrador", list(PERMISSOES.keys())),
    "operador": ("Operador", [
        "lancamentos_ver", "lancamentos_editar", "lancamentos_conferir",
        "lancamentos_manual", "importar", "relatorios", "sincronizar",
    ]),
    "leitura": ("Somente leitura", ["lancamentos_ver", "relatorios"]),
}


def hash_senha(senha, salt=None):
    """Guarda a senha como hash PBKDF2 - a senha em si nunca fica salva."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt.encode(), 200_000)
    return f"pbkdf2$200000${salt}${dk.hex()}"


def senha_confere(senha, guardado):
    try:
        _, iteracoes, salt, esperado = (guardado or "").split("$")
        dk = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt.encode(), int(iteracoes))
        return secrets.compare_digest(dk.hex(), esperado)
    except (ValueError, AttributeError):
        return False


def permissoes_do_perfil(perfil, extras=None):
    base = list(PERFIS.get(perfil, PERFIS["leitura"])[1])
    for p in (extras or []):
        if p in PERMISSOES and p not in base:
            base.append(p)
    return base


def pode(permissao):
    """Permissao do usuario logado na sessao."""
    return permissao in (session.get("permissoes") or [])


def requer(permissao):
    """Bloqueia a rota para quem nao tem a permissao."""
    def decorador(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user"):
                return redirect("/login")
            if not pode(permissao):
                titulo, _ = PERMISSOES.get(permissao, (permissao, ""))
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "erro": f"Sem permissão para: {titulo}"}), 403
                return f"""
                <html><head><title>Sem permissão · {APP_NOME}</title>{BASE_CSS}</head>
                <body>{topbar_html('Sem permissão')}
                  <div class="wrap"><div class="cat-breakdown">
                    <h3>Você não tem acesso a esta área</h3>
                    <div style="font-size:13px;color:var(--ink-soft)">
                      Esta tela exige a permissão <strong>{titulo}</strong>.
                      Peça a um administrador para liberar em Configurações → Usuários e permissões.
                    </div>
                  </div></div>
                </body></html>
                """, 403
            return view(*args, **kwargs)
        return wrapped
    return decorador

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

# conta sintetica usada para lancamentos manuais (dinheiro em especie), fora do Pluggy
CONTA_MANUAL_ID = "00000000-0000-0000-0000-000000000002"

APP_NOME = "Pé de Meia"

# Logos oficiais Pé de Meia (fundo claro, nao transparente/escuro)


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


def origem_label(tipo, connector_name, nome_conta, titular=None):
    """Rotulo amigavel (completo) de origem a partir do tipo da conta + nome do banco detectado."""
    banco = detectar_banco(nome_conta, connector_name)
    if tipo == "CREDIT":
        base = f"Cartão de Crédito {banco}"
    elif tipo == "BANK":
        base = f"Conta Corrente {banco}"
    elif tipo == "MANUAL":
        base = "Dinheiro (manual)"
    else:
        base = esc(nome_conta) or "Outra origem"
    return f"{base} · {esc(titular)}" if titular else base


def origem_label_curto(tipo, connector_name, nome_conta, titular=None):
    """Rotulo curto de origem, usado na UI ao lado do selo do banco."""
    banco = detectar_banco(nome_conta, connector_name)
    if tipo == "CREDIT":
        base = f"Cartão {banco}"
    elif tipo == "BANK":
        base = f"Conta Corrente {banco}"
    elif tipo == "MANUAL":
        base = "Dinheiro"
    else:
        base = esc(nome_conta) or "Outra"
    return f"{base} ({esc(titular)})" if titular else base


def carregar_origens(cur):
    """Le todas as contas (Pluggy + manual) e devolve estruturas prontas de origem.

    O nome do banco costuma so aparecer no nome de UMA das contas da conexao (ex: a conta
    corrente traz a razao social do banco, o cartao traz so 'Cartao de credito'). Por isso
    detectamos o banco olhando todas as contas da conexao (item_id) e aplicamos para todas.
    """
    cur.execute(
        "SELECT c.account_id, c.item_id, c.tipo, c.nome, c.numero_final, p.connector_name, it.titular "
        "FROM cartao.conta c JOIN cartao.pluggy_item p ON p.item_id = c.item_id "
        "LEFT JOIN cartao.item_titular it ON it.item_id = c.item_id "
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
        titular = c["titular"]
        completo = origem_label(c["tipo"], banco, c["nome"], titular)
        curto = origem_label_curto(c["tipo"], banco, c["nome"], titular)
        selo = selo_banco_html(detectar_banco(c["nome"], banco), c["tipo"])
        aid = str(c["account_id"])
        contas_by_id[aid] = {
            **c, "banco": banco, "label": completo, "label_curto": curto, "selo": selo, "titular": titular,
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
        attr_curto = f' data-curto="{esc(curto)}"' if curto else ""
        partes.append(
            f'<label class="chip-opt" data-tip="{esc(titulo)}"{attr_curto}>'
            f'<input type="checkbox" name="{nome}" value="{esc(val)}" {marcado} '
            f'onchange="{onchange}"> {esc(texto)}</label>'
        )
    opts_html = "".join(partes)
    label_esc = esc(label)
    return f"""
    <div class="chipfilter">
      <button type="button" class="chip-btn {"ativo" if n_sel else ""}" data-label="{label_esc}" onclick="cfToggle(this)">
        <span class="chip-plus">+</span> {label_esc}{f' ({n_sel})' if n_sel else ''}
        {f'<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' if n_sel else ''}
      </button>
      <div class="chip-panel">
        <div class="chip-search-wrap"><input type="text" class="chip-search" placeholder="Procure {label_esc.lower()}..." oninput="cfFiltrar(this)" onkeydown="cfKeydown(event, this)"></div>
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


def esc(valor):
    """Escapa texto que veio de input do usuario antes de embutir no HTML (evita XSS).
    Uso: em qualquer f-string de HTML que interpola nome de categoria, dimensao, grupo,
    observacao etc - qualquer campo de texto livre editavel pela tela."""
    if valor is None:
        return ""
    return html.escape(str(valor), quote=True)


def json_script(obj):
    """json.dumps seguro para embutir dentro de <script>...</script>. json.dumps sozinho
    NAO escapa "</" - uma descricao de lancamento contendo literalmente "</script>" fecharia
    a tag e executaria HTML/JS arbitrario para qualquer um que abrisse a tela."""
    return json.dumps(obj).replace("</", "<\\/")


def cat_pt_puro(categoria):
    """Nome da categoria em texto puro, SEM escapar.

    Use nos templates Jinja: lá o escaping é automático, então escapar aqui faria
    escapar duas vezes e a tela mostraria "&amp;lt;" em vez do caractere.
    Nas telas que ainda montam HTML por f-string, use cat_pt() (que já escapa)."""
    if not categoria:
        return "-"
    if categoria in CATEGORIA_PT_DB:
        return CATEGORIA_PT_DB[categoria]
    return CATEGORIA_PT.get(categoria, categoria)


def cat_pt(categoria):
    """Nome da categoria já escapado, para interpolar direto em f-string de HTML."""
    return esc(cat_pt_puro(categoria))


def chave_alfa(texto):
    """Chave de ordenacao alfabetica que ignora acentos, maiusculas/minusculas
    e espacos nas bordas - para que 'Água' venha antes de 'Banco', por exemplo."""
    texto = (texto or "").strip().casefold()
    sem_acento = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sem_acento if not unicodedata.combining(c))


# Overrides de nome de categoria e categorias ocultas, definidos pelo usuario
# em /categorias. Cache em memoria (processo unico) recarregado a cada escrita.
CATEGORIA_PT_DB = {}
CATEGORIAS_OCULTAS = set()


def recarregar_categorias_db():
    global CATEGORIA_PT_DB, CATEGORIAS_OCULTAS
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT categoria, nome_pt FROM cartao.categoria;")
        CATEGORIA_PT_DB = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT categoria FROM cartao.categoria_oculta;")
        CATEGORIAS_OCULTAS = {r[0] for r in cur.fetchall()}
        cur.close()
        conn.close()
    except Exception:
        pass


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
        headers = {"X-Sync-Secret": os.environ["SYNC_SECRET"]} if os.environ.get("SYNC_SECRET") else {}
        req = urllib.request.Request(BUSSOLA_SYNC_URL, method="POST", headers=headers)
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
        cur.execute("CREATE TABLE IF NOT EXISTS cartao.schema_version (versao integer PRIMARY KEY);")
        conn.commit()
        cur.execute("SELECT COALESCE(MAX(versao), 0) FROM cartao.schema_version;")
        versao_atual = cur.fetchone()[0]

        # tudo abaixo ja rodou em producao (schema atual = versao 1). So roda de novo
        # se for um banco novo (versao 0) - evita bater no Postgres com ~30 comandos
        # DDL redundantes a cada boot. Migracoes futuras: adicionar um novo bloco
        # "if versao_atual < N" abaixo deste, terminando em "INSERT ... VALUES (N)".
        if versao_atual < 1:
            cur.execute("ALTER TABLE cartao.transacao ADD COLUMN IF NOT EXISTS duplicada boolean DEFAULT false;")
            # marca lancamentos que entraram por importacao de arquivo (nao vieram do Pluggy),
            # para permitir exclui-los sem risco de "ressuscitarem" numa sincronizacao
            cur.execute("ALTER TABLE cartao.transacao ADD COLUMN IF NOT EXISTS importado boolean DEFAULT false;")
            # natureza definida no proprio lancamento, quando ele foge do padrao da categoria
            # (ex: um PIX de R$ 98 mil que foi a compra de um terreno, e nao consumo)
            cur.execute("ALTER TABLE cartao.transacao ADD COLUMN IF NOT EXISTS natureza text;")
            # renomeia a dimensao antiga (nao roda de novo depois de renomeada)
            cur.execute("UPDATE cartao.dimensao SET nome = 'Projeto' WHERE nome = 'Projeto / Evento';")

            # usuarios e permissoes. A senha fica em hash - nunca em texto puro.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cartao.usuario ("
                "usuario text PRIMARY KEY, "
                "nome text, "
                "senha_hash text NOT NULL, "
                "perfil text NOT NULL DEFAULT 'leitura', "
                "permissoes text[] NOT NULL DEFAULT '{}', "
                "ativo boolean NOT NULL DEFAULT true, "
                "criado_em timestamptz DEFAULT now(), "
                "ultimo_acesso timestamptz);"
            )
            conn.commit()

            # primeiro boot: cria os acessos que hoje vivem nas variaveis de ambiente,
            # ja como administradores, para ninguem ficar de fora do sistema
            cur.execute("SELECT COUNT(*) FROM cartao.usuario;")
            if cur.fetchone()[0] == 0:
                for login, senha in USERS.items():
                    if not login:
                        continue
                    cur.execute(
                        "INSERT INTO cartao.usuario (usuario, nome, senha_hash, perfil, permissoes) "
                        "VALUES (%s,%s,%s,'admin',%s) ON CONFLICT (usuario) DO NOTHING;",
                        (login, login.capitalize(), hash_senha(senha), permissoes_do_perfil("admin")),
                    )
                conn.commit()
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
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cartao.categoria ("
                "categoria text PRIMARY KEY, nome_pt text NOT NULL, criado_em timestamptz DEFAULT now());"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cartao.categoria_oculta (categoria text PRIMARY KEY);"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cartao.item_titular ("
                "item_id uuid PRIMARY KEY REFERENCES cartao.pluggy_item(item_id) ON DELETE CASCADE, "
                "titular text NOT NULL);"
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
            cur.execute("INSERT INTO cartao.schema_version (versao) VALUES (1);")
            conn.commit()

        if versao_atual < 2:
            # teto de gasto passa a ser por valor de dimensao (ex: "Ronaldo: R$3000/mes"),
            # nao mais por centro de custo - ver conversa que motivou essa mudanca.
            cur.execute("ALTER TABLE cartao.dimensao_valor ADD COLUMN IF NOT EXISTS teto_mensal numeric;")
            cur.execute("ALTER TABLE cartao.dimensao_valor ADD COLUMN IF NOT EXISTS teto_anual numeric;")
            cur.execute("INSERT INTO cartao.schema_version (versao) VALUES (2);")
            conn.commit()

        if versao_atual < 3:
            # Fechamento/vencimento da fatura passam a ser por cartao - antes o
            # fechamento era uma constante unica no codigo, o que so funcionava com
            # um cartao (Unicred, Nubank Ronaldo e Nubank Andrea fecham em dias
            # diferentes). As datas vem do Pluggy: vencimento_fatura ja existia,
            # fechamento_fatura foi adicionado aqui.
            cur.execute("ALTER TABLE cartao.conta ADD COLUMN IF NOT EXISTS fechamento_fatura DATE;")
            # dia_fechamento/dia_vencimento foram uma tentativa de sobrescrita manual,
            # DESCONTINUADA logo depois: a tela ficou confusa e o dado do banco e mais
            # confiavel. Os ALTER continuam aqui so porque a migracao ja rodou em
            # producao - reescrever migracao aplicada criaria divergencia de schema.
            # As colunas ficam sem uso; nada le nem grava nelas.
            cur.execute("ALTER TABLE cartao.conta ADD COLUMN IF NOT EXISTS dia_fechamento integer;")
            cur.execute("ALTER TABLE cartao.conta ADD COLUMN IF NOT EXISTS dia_vencimento integer;")
            cur.execute("INSERT INTO cartao.schema_version (versao) VALUES (3);")
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
recarregar_categorias_db()


BASE_CSS_HEAD = """
<link rel="icon" type="image/png" href="/static/favicon.png">
"""

BASE_CSS = BASE_CSS_HEAD + """
<link rel="stylesheet" href="/static/app.css">
<script src="/static/tabelas.js"></script>
"""


def topbar_html(titulo, ativo=None):
    def cls(nome):
        return "ativo" if ativo == nome else ""
    return f"""
      <div class="topbar">
        <a href="/" class="marca-box" style="text-decoration:none" title="Ir para o início">
          <img class="marca-icon" src="/static/logo-topbar.png" alt="Pé de Meia">
          <div>
            <span class="marca">{APP_NOME}</span><br>
            <span class="marca-pagina">{titulo} · {session.get('user')}</span>
          </div>
        </a>
        <div class="nav-menu">
          {f'<a href="/" class="{cls("inicio")}">Lançamentos</a>' if pode("lancamentos_ver") else ""}
          {f'''<div class="dropdown">
            <button type="button" class="dropbtn" onclick="menuToggle(event, this)">Relatórios ▾</button>
            <div class="dropdown-content">
              <a href="/relatorios" class="{cls('relatorios')}">Relatórios</a>
              <a href="/dre" class="{cls('dre')}">DRE / Centro de Custos</a>
              <a href="/investimentos" class="{cls('investimentos')}">Investimentos</a>
            </div>
          </div>''' if pode("relatorios") else ""}
          {f'''<div class="dropdown">
            <button type="button" class="dropbtn" onclick="menuToggle(event, this)">Configurações ▾</button>
            <div class="dropdown-content">
              {f'<a href="/pendencias" class="{cls("pendencias")}">Pendências de classificação</a>' if pode("cadastros") else ""}
              {f'<a href="/categorias" class="{cls("categorias")}">Gerenciar categorias</a>' if pode("cadastros") else ""}
              {f'<a href="/grupos" class="{cls("grupos")}">Centro de Custos</a>' if pode("cadastros") else ""}
              {f'<a href="/dimensoes" class="{cls("dimensoes")}">Gerenciar dimensões</a>' if pode("cadastros") else ""}
              {f'<a href="/regras" class="{cls("regras")}">Regras automáticas</a>' if pode("cadastros") else ""}
              {f'<a href="/contas" class="{cls("contas")}">Configurações de Contas / Cartão</a>' if pode("cadastros") else ""}
              {f'<a href="/usuarios" class="{cls("usuarios")}">Usuários e permissões</a>' if pode("usuarios") else ""}
            </div>
          </div>''' if (pode("cadastros") or pode("usuarios")) else ""}
          {'''<div class="sync-widget">
            <span class="sync-dot" id="syncDot"></span>
            <span id="syncTexto">Verificando...</span>
            <button class="sync-btn" id="syncBtn" onclick="dispararSync()">Atualizar agora</button>
          </div>''' if pode("sincronizar") else ""}
          <a href="/logout">Sair</a>
        </div>
      </div>
      <script src="/static/topbar.js"></script>
    """


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = (request.form.get("usuario", "") or "").strip()
        p = request.form.get("senha", "")
        conta = None
        try:
            conn = get_conn()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT usuario, nome, senha_hash, perfil, permissoes, ativo "
                "FROM cartao.usuario WHERE lower(usuario) = lower(%s);",
                (u,),
            )
            conta = cur.fetchone()
            if conta and conta["ativo"] and senha_confere(p, conta["senha_hash"]):
                cur.execute("UPDATE cartao.usuario SET ultimo_acesso = now() WHERE usuario = %s;",
                            (conta["usuario"],))
                conn.commit()
                session["user"] = conta["usuario"]
                session["nome"] = conta["nome"] or conta["usuario"]
                session["perfil"] = conta["perfil"]
                session["permissoes"] = list(conta["permissoes"] or [])
                cur.close()
                conn.close()
                return redirect("/")
            cur.close()
            conn.close()
        except Exception as e:
            print("Aviso: falha ao autenticar pelo banco:", e)

        # rede de seguranca: se a tabela ainda nao existe (primeiro boot), aceita a env
        if conta is None and u in USERS and USERS[u] == p:
            session["user"] = u
            session["nome"] = u
            session["perfil"] = "admin"
            session["permissoes"] = permissoes_do_perfil("admin")
            return redirect("/")

        error = "Usuário ou senha inválidos." if not (conta and not conta["ativo"]) \
            else "Este acesso está desativado. Fale com um administrador."
    return render_template("login.html", titulo="Entrar", erro=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/sync-status")
@login_required
def api_sync_status():
    return jsonify(get_ultima_sincronizacao())


@app.route("/api/sync-agora", methods=["POST"])
@requer("sincronizar")
def api_sync_agora():
    ok, erro = disparar_sincronizacao()
    if not ok:
        return jsonify({"executado_em": None, "status": "erro", "mensagem_erro": erro}), 502
    return jsonify(get_ultima_sincronizacao())


@app.route("/")
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

    def cat_options(selected):
        return "".join(
            f'<option value="{esc(c)}" {"selected" if c == selected else ""}>{cat_pt(c)}</option>'
            for c in categorias
        )

    def dim_options(dimensao_id, selecionado):
        opts = ['<option value="">(nao definido)</option>']
        for v in valores_por_dim.get(dimensao_id, []):
            sel = "selected" if selecionado == v["id"] else ""
            opts.append(f'<option value="{v["id"]}" {sel}>{esc(v["nome"])}</option>')
        return "".join(opts)

    # a tela nao deve oferecer acao que a API vai recusar
    pode_editar = pode("lancamentos_editar")
    pode_conferir = pode("lancamentos_conferir")
    pode_manual = pode("lancamentos_manual")
    dis_editar = "" if pode_editar else " disabled"
    dis_conferir = "" if pode_conferir else " disabled"

    trs = []
    detalhes_js = {}
    for r in rows:
        checked = "checked" if r["conferida"] else ""
        dup_checked = "checked" if r["duplicada"] else ""
        classes = " ".join(c for c in ["conferida" if r["conferida"] else "", "duplicada" if r["duplicada"] else ""] if c)
        data_local = r["data_transacao"] - timedelta(hours=3)
        data_fmt = data_local.strftime("%d/%m/%y<br>%H:%M")
        data_fmt_full = data_local.strftime("%d/%m/%Y %H:%M")
        obs = esc(r["observacao"])
        rid = r["transacao_id"]
        desc = r["descricao"] or ""
        desc_esc = esc(desc)

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

        dim_tds = []
        dim_detalhes = {}
        for d in dimensoes:
            valor_sel = mapa_dim_transacao.get((str(rid), d["id"]))
            faltando = d["obrigatoria"] and not valor_sel
            estilo = ' style="border-color:#c23c34;background:#fbeceb"' if faltando else ""
            dim_tds.append(
                f'<td class="cel-dim" data-col="dim_{d["id"]}"><select class="dim-select" data-dim="{d["id"]}"{estilo}{dis_editar} '
                f'onchange="salvar(\'{rid}\', this)">{dim_options(d["id"], valor_sel)}</select></td>'
            )
            nomes_valor = {v["id"]: v["nome"] for v in valores_por_dim.get(d["id"], [])}
            dim_detalhes[d["nome"]] = nomes_valor.get(valor_sel, "(nao definido)")

        trs.append(
            f'<tr class="{classes}" data-id="{rid}" onclick="linhaClick(event, \'{rid}\')">'
            f'<td class="cel-data" data-col="data" data-tip="{data_fmt_full}" data-sort="{data_local.timestamp()}">{data_fmt}</td>'
            f'<td class="cel-desc" data-col="desc" data-tip="{desc_esc}">{desc_esc}</td>'
            f'<td class="cel-origem" data-col="origem" data-tip="{origem_completa(r["account_id"], r["numero_cartao_final"])}">{origem_curta(r["account_id"], r["numero_cartao_final"])}</td>'
            f'<td class="cel-dim" data-col="categoria"><select class="cat-select"{dis_editar} onchange="salvar(\'{rid}\', this)">{cat_options(r["categoria"])}</select></td>'
            + "".join(dim_tds) +
            f'<td class="valor cel-valor" data-col="valor" style="{cor_valor}" data-sort="{valor_sort}">{valor_fmt}</td>'
            f'<td class="cel-obs" data-col="obs"><input class="obs-input" type="text" value="{obs}" placeholder="obs..."{dis_editar} onblur="salvar(\'{rid}\', this)"></td>'
            f'<td class="cel-check" data-col="check"><input class="conf-check" type="checkbox" {checked}{dis_conferir} onchange="salvar(\'{rid}\', this)">'
            f'<input class="dup-check" type="checkbox" {dup_checked} hidden></td>'
            f'<td class="cel-status" data-col="status"><span class="status" id="status-{rid}">ok</span></td>'
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
    dim_headers = "".join(f'<th class="cel-dim" data-col="dim_{d["id"]}">{esc(d["nome"])}</th>' for d in dimensoes)

    cat_rows_html = "".join(
        f'<div class="cat-row"><span>{cat_pt(c["categoria"])}</span><span>R$ {c["total"]:,.2f}</span></div>'
        for c in por_categoria
    ) or '<div class="cat-row"><span>Sem dados</span></div>'

    origem_filtro_html = chip_filter_html("origem", "Origem", origem_opcoes, origem_sel, onchange="aplicarFiltros()")
    categoria_options_manual = "".join(f'<option value="{esc(c)}">{cat_pt(c)}</option>' for c in categorias)
    natureza_options = "".join(
        f'<option value="{k}">{v}</option>' for k, v in NATUREZAS.items() if k != "fluxo"
    )

    return f"""
    <html><head><title>Lançamentos · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Lançamentos', 'inicio')}
      <div class="wrap">
        <div class="filters" style="flex-wrap:wrap;gap:14px">
          <div>
            <label>Mes</label>
            <div style="display:flex;align-items:center;gap:4px">
              <button type="button" onclick="mudarMes(-1)" title="Mês anterior"
                      style="padding:6px 12px;font-size:15px;line-height:1">‹</button>
              <input type="month" id="mesInput" value="{mes}" onchange="aplicarFiltros()">
              <button type="button" onclick="mudarMes(1)" title="Próximo mês"
                      style="padding:6px 12px;font-size:15px;line-height:1">›</button>
            </div>
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
          {'<div style="margin-left:auto"><button type="button" onclick="toggleFormManual()">+ Lançamento manual</button></div>' if pode_manual else ""}
        </div>

        <div id="formManual" class="cat-breakdown" style="display:none" {"hidden" if not pode_manual else ""}>
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
        </div>

        <details class="cat-breakdown">
          <summary>Gasto por categoria (mês)</summary>
          <div class="det-body">{cat_rows_html}</div>
        </details>

        <table class="compacta ajustavel" id="tabela-lancamentos" data-tabela="lancamentos">
          <thead><tr>
            <th class="cel-data" data-col="data">Data</th><th class="cel-desc" data-col="desc">Descricao</th><th class="cel-origem" data-col="origem">Origem</th><th class="cel-dim" data-col="categoria">Categoria</th>{dim_headers}<th class="cel-valor" data-col="valor" style="text-align:right">Valor</th><th class="cel-obs" data-col="obs">Obs</th><th class="cel-check" data-col="check">OK</th><th class="cel-status" data-col="status"></th>
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
            <button type="button" class="btn-perigo" onclick="excluirManual()"{"" if pode_manual else " disabled"}>Excluir lançamento</button>
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
        // avanca/retrocede um mes no filtro. Usa Date pra virar o ano sozinho
        // (dezembro -> janeiro do ano seguinte) em vez de somar no numero do mes.
        function mudarMes(delta) {{
          const campo = document.getElementById('mesInput');
          const partes = (campo.value || '').split('-').map(Number);
          if (partes.length !== 2 || !partes[0] || !partes[1]) return;
          const d = new Date(partes[0], partes[1] - 1 + delta, 1);
          campo.value = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
          aplicarFiltros();
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
              if (novaTabela) {{
                document.querySelector('table.compacta').replaceWith(novaTabela);
                // a tabela nova veio do servidor sem os listeners nem as alcas de
                // redimensionar (sao criados por JS) - replaceWith descarta o elemento
                // antigo junto com tudo que estava anexado nele, entao precisa reativar
                ativarTabelaAjustavel(novaTabela, 'lancamentos');
              }}
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

        window.detalhes = {json_script(detalhes_js)};
        let idAtualModal = null;
        function escHtml(s) {{
          // escapa antes de jogar em innerHTML - descricao/observacao sao texto livre
          // digitado pelo usuario (lancamento manual, importacao) e nao podem virar HTML/JS
          return String(s ?? '').replace(/[&<>"']/g, c => ({{
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
          }})[c]);
        }}
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
            html += '<div class="row"><span>' + labels[k] + '</span><span>' + escHtml(d[k]) + '</span></div>';
          }}
          for (const k in d) {{
            if (!(k in labels) && k.charAt(0) !== '_') {{
              html += '<div class="row"><span>' + escHtml(k) + '</span><span>' + escHtml(d[k]) + '</span></div>';
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
      <script type="application/json" data-detalhes>{json_script(detalhes_js)}</script>
    </body></html>
    """


@app.route("/api/lancamento-manual", methods=["POST"])
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


@app.route("/api/lancamento-manual/<transacao_id>", methods=["DELETE"])
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




@app.route("/api/transacao/<transacao_id>", methods=["POST"])
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


@app.route("/cartoes")
@requer("cadastros")
def cartoes():
    # Fundido em /contas (Configuracoes de Contas / Cartao): o apelido do cartao
    # descreve a origem do dinheiro, mesmo assunto do titular da conta.
    return redirect("/contas")


@app.route("/dimensoes", methods=["GET", "POST"])
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
                    erro = f"Ja existe uma dimensao chamada '{esc(nome)}'."
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
                    erro = f"Ja existe o valor '{esc(nome)}' nessa dimensao."
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


@app.route("/regras", methods=["GET", "POST"])
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
        (set(CATEGORIA_PT) | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_NEUTRAS_PADRAO - CATEGORIAS_OCULTAS,
        key=lambda c: chave_alfa(cat_pt(c)),
    )

    cur.close()
    conn.close()

    def cat_options_regra(selecionado=None):
        return "".join(
            f'<option value="{esc(c)}" {"selected" if c == selecionado else ""}>{cat_pt(c)}</option>'
            for c in todas_categorias
        )

    def dim_options_regra(dimensao_id, selecionado=None):
        opts = ['<option value="">(nao definir)</option>']
        for v in valores_por_dim.get(dimensao_id, []):
            sel = "selected" if selecionado == v["id"] else ""
            opts.append(f'<option value="{v["id"]}" {sel}>{esc(v["nome"])}</option>')
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
                f'<input name="padrao" value="{esc(r["padrao"])}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:220px"></div>'
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
    <html><head><title>Regras Automaticas · Pé de Meia</title>{BASE_CSS}</head>
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


# ---- filtros disponiveis nos templates Jinja ----
# Existem para o template nao precisar de logica: {{ valor|moeda }} em vez de
# montar a string no Python e passar HTML pronto (que perderia o escaping).
@app.template_filter("moeda")
def _filtro_moeda(v):
    return _fmt_moeda(float(v or 0))


@app.template_filter("num")
def _filtro_num(v):
    """Numero sem casas decimais desnecessarias; vazio quando nao ha valor.
    Usado em campo de formulario, onde None tem que virar string vazia."""
    if v is None or v == "":
        return ""
    return f"{float(v):g}"


@app.context_processor
def _globais_template():
    return {"barra": _barra_html}


@app.route("/dre")
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

    blocos = []
    total_geral_anual = 0.0
    for g in sorted(grupos.values(), key=lambda x: chave_alfa(x["nome"])):
        g_anual = 0.0
        subs_html = []
        for s in sorted(g["subgrupos"].values(), key=lambda x: chave_alfa(x["nome"])):
            s_anual = sum(anual_por_cat.get(c, 0.0) for c in s["categorias"])
            g_anual += s_anual
            cats_pt = ", ".join(cat_pt(c) for c in s["categorias"]) or "sem categorias vinculadas"
            subs_html.append(
                f'<div style="padding:10px 0;border-top:1px solid #f2f2f2">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
                f'<strong style="font-size:14px">{s["nome"]}</strong>'
                f'<span style="font-size:14px">{_fmt_moeda(s_anual)} no ano</span>'
                f'</div>'
                f'<div style="font-size:11px;color:#aaa;margin-top:2px">{cats_pt}</div>'
                f'</div>'
            )
        total_geral_anual += g_anual
        blocos.append(
            f'<div class="cat-breakdown">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
            f'<h3 style="margin:0">{g["nome"]}</h3>'
            f'<span style="font-size:18px;font-weight:600">{_fmt_moeda(g_anual)}</span>'
            f'</div>'
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
            f'<div style="font-size:12px;color:#888;margin-bottom:8px">Categorias sem centro de custo definido em <a href="/grupos">Centro de Custos</a>.</div>'
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
    <html><head><title>DRE / Centro de Custos · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      {topbar_html('DRE / Centro de Custos', 'dre')}
      <div class="wrap">
        {aviso_pend}
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
          <div class="tabela-scroll">
          <table class="compacta ajustavel" data-tabela="dre-mensal">
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
                erro = f"Já existe um centro de custo chamado '{esc(nome)}'."
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
                erro = f"Já existe um subgrupo chamado '{esc(nome)}' nesse centro de custo."
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
            cur.execute(
                "INSERT INTO cartao.categoria_subgrupo (categoria, subgrupo_id) VALUES (%s,%s) "
                "ON CONFLICT (categoria) DO UPDATE SET subgrupo_id = EXCLUDED.subgrupo_id;",
                (request.form.get("categoria"), subgrupo_id),
            )
            conn.commit()

    cur.execute("SELECT id, nome FROM cartao.grupo_custo;")
    grupos_db = sorted(cur.fetchall(), key=lambda g: chave_alfa(g["nome"]))
    cur.execute("SELECT id, grupo_id, nome FROM cartao.subgrupo_custo;")
    subgrupos_db = sorted(cur.fetchall(), key=lambda s: chave_alfa(s["nome"]))
    cur.execute("SELECT categoria, subgrupo_id FROM cartao.categoria_subgrupo;")
    mapa_categoria = {r["categoria"]: r["subgrupo_id"] for r in cur.fetchall()}
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
        {"chave": c, "nome": cat_pt_puro(c), "subgrupo_id": mapa_categoria.get(c)}
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
        grupos=grupos_db,
        subgrupos_por_grupo=subgrupos_por_grupo,
        categorias=categorias,
        categorias_por_subgrupo=categorias_por_subgrupo,
        sem_vinculo=sem_vinculo,
    )


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
    todas_categorias = sorted((categorias_db | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_OCULTAS, key=lambda c: chave_alfa(cat_pt(c)))

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
    <html><head><title>Relatórios · Pé de Meia</title>{BASE_CSS}
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
@requer("relatorios")
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
    nomes_cartao = {r["final4"]: esc(r["prefixo"]) for r in cur.fetchall()}

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
    nomes_cartao = {r["final4"]: esc(r["prefixo"]) for r in cur.fetchall()}

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
@requer("importar")
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
@requer("importar")
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
@requer("importar")
def importar_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _, origem_opcoes = carregar_origens(cur)
    cur.close()
    conn.close()

    return render_template(
        "importar.html",
        titulo="Importar extrato / fatura",
        topbar=topbar_html("Importar extrato / fatura", "importar"),
        origem_opcoes=origem_opcoes,
    )


@app.route("/investimentos")
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

    if posicoes is None:
        return f"""
        <html><head><title>Investimentos · Pé de Meia</title>{BASE_CSS}</head>
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
          <div class="tabela-scroll">
          <table class="compacta ajustavel" data-tabela="investimentos-historico">
            <thead><tr><th>Mês</th><th style="text-align:right">Aplicado</th>
            <th style="text-align:right">Saldo</th><th style="text-align:right">Variação</th></tr></thead>
            <tbody>{"".join(linhas_hist)}</tbody>
          </table>
          </div>
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
    <html><head><title>Investimentos · Pé de Meia</title>{BASE_CSS}</head>
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
          <div class="tabela-scroll">
          <table class="compacta ajustavel" data-tabela="investimentos-posicao">
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
        </div>

        {bloco_hist}
      </div>
    </body></html>
    """


@app.route("/contas", methods=["GET", "POST"])
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


@app.route("/api/categoria-lancamentos")
@requer("cadastros")
def api_categoria_lancamentos():
    """Lista os lancamentos de uma categoria - usado pelo botao 'protegida' em /categorias,
    pra mostrar o que esta impedindo a remocao sem precisar ir pra tela de Lancamentos."""
    categoria = request.args.get("categoria") or ""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT t.data_transacao, t.descricao, COALESCE(t.valor_brl, t.valor_original) AS valor "
        "FROM cartao.transacao t WHERE t.categoria = %s ORDER BY t.data_transacao DESC LIMIT 300;",
        (categoria,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([
        {
            "data": (r["data_transacao"] - timedelta(hours=3)).strftime("%d/%m/%Y") if r["data_transacao"] else "-",
            "descricao": r["descricao"] or "-",
            "valor": float(r["valor"]) if r["valor"] is not None else 0,
        }
        for r in rows
    ])


def levantar_pendencias(cur):
    """Levanta o que esta mal classificado e pode distorcer o DRE.

    Tres coisas, em ordem de gravidade contabil:

    1. categoria SEM natureza definida - o app assume 'despesa' por padrao, entao
       uma categoria nova que o Pluggy inventou (ex: um investimento) entra como
       despesa silenciosamente e infla o resultado. E o caso mais grave porque
       ninguem decidiu nada: aconteceu sozinho.
    2. categoria de DESPESA sem centro de custo - nao afeta o resultado (a despesa
       e contada de qualquer forma), mas some dos totais por grupo do DRE. So vale
       para despesa: vincular receita ou transferencia a centro de custo nao faz
       sentido contabil (centro de custo e analise de gasto).
    3. lancamentos com natureza manual - excecao marcada no proprio lancamento, que
       sobrepoe a natureza da categoria. Funciona, mas fica invisivel: o certo e
       mover o lancamento para uma categoria que ja tenha a natureza correta.
    """
    cur.execute(
        "SELECT DISTINCT t.categoria FROM cartao.transacao t "
        "LEFT JOIN cartao.categoria_natureza n ON n.categoria = t.categoria "
        "WHERE t.categoria IS NOT NULL AND n.categoria IS NULL;"
    )
    sem_natureza = sorted(
        (r["categoria"] for r in cur.fetchall() if r["categoria"] not in CATEGORIAS_OCULTAS),
        key=lambda c: chave_alfa(cat_pt(c)),
    )

    cur.execute(
        "SELECT DISTINCT t.categoria FROM cartao.transacao t "
        "JOIN cartao.categoria_natureza n ON n.categoria = t.categoria "
        "LEFT JOIN cartao.categoria_subgrupo cs ON cs.categoria = t.categoria "
        "WHERE n.natureza = 'despesa' AND cs.subgrupo_id IS NULL;"
    )
    despesa_sem_centro = sorted(
        (r["categoria"] for r in cur.fetchall() if r["categoria"] not in CATEGORIAS_OCULTAS),
        key=lambda c: chave_alfa(cat_pt(c)),
    )

    cur.execute("SELECT COUNT(*) AS n FROM cartao.transacao WHERE natureza IS NOT NULL;")
    natureza_manual = cur.fetchone()["n"]

    return {
        "sem_natureza": sem_natureza,
        "despesa_sem_centro": despesa_sem_centro,
        "natureza_manual": natureza_manual,
        "total": len(sem_natureza) + len(despesa_sem_centro),
    }


def aviso_pendencias_html(pend):
    """Faixa de alerta mostrada no topo das telas de uso diario. So aparece quando
    ha algo que realmente pode distorcer numero - nunca polui a tela a toa."""
    if not pend["total"]:
        return ""
    partes = []
    if pend["sem_natureza"]:
        n = len(pend["sem_natureza"])
        partes.append(f'<strong>{n}</strong> categoria{"s" if n > 1 else ""} sem natureza definida'
                      f' (entra{"m" if n > 1 else ""} como despesa por padrão)')
    if pend["despesa_sem_centro"]:
        n = len(pend["despesa_sem_centro"])
        partes.append(f'<strong>{n}</strong> categoria{"s" if n > 1 else ""} de despesa sem centro de custo')
    return (
        '<div style="background:var(--bad-soft);border:1px solid var(--bad);border-radius:10px;'
        'padding:10px 14px;margin-bottom:14px;font-size:13px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
        '<span>⚠</span><span>' + " · ".join(partes) + '</span>'
        '<a href="/pendencias" style="margin-left:auto;color:var(--bad);font-weight:600">Revisar agora →</a>'
        '</div>'
    )


@app.route("/pendencias", methods=["GET", "POST"])
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


@app.route("/categorias", methods=["GET", "POST"])
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
                elif nome in CATEGORIA_PT.values() or nome in CATEGORIA_PT_DB.values():
                    erro = "Já existe uma categoria com esse nome."
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
                if not novo_nome:
                    erro = "Informe o novo nome."
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

    return render_template(
        "categorias.html",
        titulo="Gerenciar categorias",
        topbar=topbar_html("Gerenciar categorias", "categorias"),
        aviso=aviso,
        erro=erro,
        categorias=categorias,
        naturezas=NATUREZAS,
        naturezas_neutras=NATUREZAS_NEUTRAS,
    )


@app.route("/naturezas", methods=["GET", "POST"])
@requer("cadastros")
def naturezas_view():
    # Fundido em /categorias (natureza contábil agora é uma coluna da mesma tela).
    return redirect("/categorias")


@app.route("/usuarios", methods=["GET", "POST"])
@requer("usuarios")
def usuarios_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aviso = erro = None

    def total_admins_ativos(excluindo=None):
        cur.execute(
            "SELECT COUNT(*) AS n FROM cartao.usuario "
            "WHERE ativo = true AND 'usuarios' = ANY(permissoes) AND usuario <> %s;",
            (excluindo or "",),
        )
        return cur.fetchone()["n"]

    if request.method == "POST":
        acao = request.form.get("acao")
        alvo = (request.form.get("usuario") or "").strip()
        try:
            if acao == "criar":
                login = (request.form.get("novo_usuario") or "").strip().lower()
                senha = request.form.get("nova_senha") or ""
                perfil = request.form.get("perfil") or "leitura"
                if not login or not senha:
                    erro = "Informe usuário e senha."
                elif len(senha) < 6:
                    erro = "A senha precisa ter pelo menos 6 caracteres."
                else:
                    cur.execute(
                        "INSERT INTO cartao.usuario (usuario, nome, senha_hash, perfil, permissoes) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (usuario) DO NOTHING RETURNING usuario;",
                        (login, (request.form.get("novo_nome") or login.capitalize()).strip(),
                         hash_senha(senha), perfil, permissoes_do_perfil(perfil)),
                    )
                    aviso = f'Usuário "{esc(login)}" criado.' if cur.fetchone() else "Já existe um usuário com esse login."

            elif acao == "permissoes":
                perfil = request.form.get("perfil") or "leitura"
                marcadas = [p for p in request.form.getlist("perm") if p in PERMISSOES]
                # nao deixa tirar o proprio acesso de gerenciar usuarios
                if alvo == session.get("user") and "usuarios" not in marcadas:
                    erro = "Você não pode remover a sua própria permissão de gerenciar usuários."
                elif "usuarios" not in marcadas and total_admins_ativos(alvo) == 0:
                    erro = "É preciso ter ao menos um administrador com acesso a usuários."
                else:
                    cur.execute(
                        "UPDATE cartao.usuario SET perfil = %s, permissoes = %s, nome = %s WHERE usuario = %s;",
                        (perfil, marcadas, (request.form.get("nome") or "").strip() or alvo, alvo),
                    )
                    aviso = f'Permissões de "{esc(alvo)}" atualizadas.'

            elif acao == "senha":
                nova = request.form.get("senha") or ""
                if len(nova) < 6:
                    erro = "A senha precisa ter pelo menos 6 caracteres."
                else:
                    cur.execute("UPDATE cartao.usuario SET senha_hash = %s WHERE usuario = %s;",
                                (hash_senha(nova), alvo))
                    aviso = f'Senha de "{esc(alvo)}" alterada.'

            elif acao == "ativar":
                ativo = request.form.get("ativo") == "1"
                if not ativo and alvo == session.get("user"):
                    erro = "Você não pode desativar o seu próprio acesso."
                elif not ativo and total_admins_ativos(alvo) == 0:
                    erro = "É preciso manter ao menos um administrador ativo."
                else:
                    cur.execute("UPDATE cartao.usuario SET ativo = %s WHERE usuario = %s;", (ativo, alvo))
                    aviso = f'Acesso de "{esc(alvo)}" ' + ("reativado." if ativo else "desativado.")

            elif acao == "excluir":
                if alvo == session.get("user"):
                    erro = "Você não pode excluir o seu próprio usuário."
                elif total_admins_ativos(alvo) == 0:
                    erro = "É preciso manter ao menos um administrador."
                else:
                    cur.execute("DELETE FROM cartao.usuario WHERE usuario = %s;", (alvo,))
                    aviso = f'Usuário "{esc(alvo)}" excluído.'
            conn.commit()
        except Exception as e:
            conn.rollback()
            erro = str(e)

    cur.execute(
        "SELECT usuario, nome, perfil, permissoes, ativo, criado_em, ultimo_acesso "
        "FROM cartao.usuario ORDER BY ativo DESC, usuario;"
    )
    contas = cur.fetchall()
    cur.close()
    conn.close()

    def perfil_options(atual):
        return "".join(
            f'<option value="{k}" {"selected" if k == atual else ""}>{v[0]}</option>'
            for k, v in PERFIS.items()
        )

    def _dt(v):
        return v.strftime("%d/%m/%Y %H:%M") if v else "nunca"

    blocos = []
    for c in contas:
        perms = list(c["permissoes"] or [])
        eu = c["usuario"] == session.get("user")
        checks = "".join(
            f'<label class="perm-item" data-tip="{desc}">'
            f'<input type="checkbox" name="perm" value="{chave}" {"checked" if chave in perms else ""}>'
            f'<span>{titulo}</span></label>'
            for chave, (titulo, desc) in PERMISSOES.items()
        )
        estado = ('<span class="tag-ativo">ativo</span>' if c["ativo"]
                  else '<span class="tag-inativo">desativado</span>')
        blocos.append(f"""
        <details class="cat-breakdown" style="padding:0">
          <summary style="cursor:pointer;padding:14px 18px;display:flex;align-items:center;gap:10px">
            <strong style="font-size:14px">{c["nome"] or c["usuario"]}</strong>
            <span style="color:var(--ink-faint);font-size:12.5px">{c["usuario"]}</span>
            {estado}
            <span style="color:var(--ink-faint);font-size:11.5px">{PERFIS.get(c["perfil"], ("Personalizado",))[0]}</span>
            <span style="margin-left:auto;color:var(--ink-faint);font-size:11.5px">
              último acesso: {_dt(c["ultimo_acesso"])}</span>
          </summary>
          <div style="padding:0 18px 18px 18px">
            <form method="post">
              <input type="hidden" name="acao" value="permissoes">
              <input type="hidden" name="usuario" value="{c["usuario"]}">
              <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
                <input name="nome" value="{esc(c["nome"]) if c["nome"] else ""}" placeholder="Nome"
                       style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;width:190px">
                <span style="font-size:12px;color:var(--ink-faint)">Perfil</span>
                <select name="perfil" onchange="aplicarPerfil(this)" data-usuario="{c["usuario"]}"
                        style="padding:7px 9px">{perfil_options(c["perfil"])}</select>
                <span style="font-size:11.5px;color:var(--ink-faint)">
                  escolher um perfil marca as permissões dele; depois você pode ajustar uma a uma
                </span>
              </div>
              <div class="perm-grid" data-usuario="{c["usuario"]}">{checks}</div>
              <button type="submit" style="margin-top:12px">Salvar permissões</button>
            </form>

            <div style="display:flex;gap:22px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid var(--line-soft)">
              <form method="post" style="display:flex;gap:8px;align-items:center">
                <input type="hidden" name="acao" value="senha">
                <input type="hidden" name="usuario" value="{c["usuario"]}">
                <input name="senha" type="password" placeholder="Nova senha" minlength="6"
                       style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;width:170px">
                <button type="submit" class="ver-btn">Trocar senha</button>
              </form>
              <form method="post" style="display:flex;align-items:center">
                <input type="hidden" name="acao" value="ativar">
                <input type="hidden" name="usuario" value="{c["usuario"]}">
                <input type="hidden" name="ativo" value="{'0' if c["ativo"] else '1'}">
                <button type="submit" class="ver-btn" {"disabled" if eu else ""}>
                  {"Desativar acesso" if c["ativo"] else "Reativar acesso"}</button>
              </form>
              <form method="post" onsubmit="return confirm('Excluir o usuário {c["usuario"]}? Esta ação não pode ser desfeita.')">
                <input type="hidden" name="acao" value="excluir">
                <input type="hidden" name="usuario" value="{c["usuario"]}">
                <button type="submit" class="btn-perigo" {"disabled" if eu else ""}>Excluir</button>
              </form>
            </div>
          </div>
        </details>""")

    msg = ""
    if aviso:
        msg = f'<div class="aviso-ok">{aviso}</div>'
    if erro:
        msg = f'<div class="aviso-erro">{erro}</div>'

    legenda = "".join(
        f'<div class="cat-row"><span><strong>{t}</strong></span><span style="color:var(--ink-soft);font-size:12.5px">{d}</span></div>'
        for t, d in PERMISSOES.values()
    )

    return f"""
    <html><head><title>Usuários e permissões · {APP_NOME}</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Usuários e permissões', 'usuarios')}
      <div class="wrap">
        {msg}
        <div class="cat-breakdown">
          <h3>Novo usuário</h3>
          <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="hidden" name="acao" value="criar">
            <input name="novo_usuario" placeholder="Login (ex: joao)" required
                   style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;width:170px">
            <input name="novo_nome" placeholder="Nome"
                   style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;width:180px">
            <input name="nova_senha" type="password" placeholder="Senha (mín. 6)" minlength="6" required
                   style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;width:170px">
            <select name="perfil" style="padding:7px 9px">{perfil_options("operador")}</select>
            <button type="submit">Criar usuário</button>
          </form>
        </div>

        {"".join(blocos)}

        <details class="cat-breakdown" style="padding:0">
          <summary style="cursor:pointer;padding:14px 18px;font-weight:600;font-size:13px;color:var(--ink-soft)">
            O que cada permissão libera
          </summary>
          <div style="padding:0 18px 16px 18px">{legenda}</div>
        </details>
      </div>

      <script>
        const PERFIS = {json.dumps({k: v[1] for k, v in PERFIS.items()})};
        function aplicarPerfil(sel) {{
          const perms = PERFIS[sel.value] || [];
          const grade = document.querySelector('.perm-grid[data-usuario="' + sel.dataset.usuario + '"]');
          if (!grade) return;
          grade.querySelectorAll('input[type=checkbox]').forEach(cb => {{
            cb.checked = perms.includes(cb.value);
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
