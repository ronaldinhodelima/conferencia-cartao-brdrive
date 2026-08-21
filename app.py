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
from flask import Flask, request, redirect, session, jsonify

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

# dia de fechamento da fatura (fixo, informado pelo usuario - Pluggy nao sincroniza esse dado)
FATURA_DIA_FECHAMENTO = 12

# conta sintetica usada para lancamentos manuais (dinheiro em especie), fora do Pluggy
CONTA_MANUAL_ID = "00000000-0000-0000-0000-000000000002"

APP_NOME = "Pé de Meia"

# Logos oficiais Pé de Meia (fundo claro, nao transparente/escuro)
LOGO_FAVICON_B64 = "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"
LOGO_TOPBAR_B64 = "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"
LOGO_HERO_B64 = "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"


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


def cat_pt(categoria):
    if not categoria:
        return "-"
    if categoria in CATEGORIA_PT_DB:
        return esc(CATEGORIA_PT_DB[categoria])
    return esc(CATEGORIA_PT.get(categoria, categoria))


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
<link rel="icon" type="image/png" href="data:image/png;base64,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">
"""

BASE_CSS = BASE_CSS_HEAD + """
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
  .topbar > div:first-child { display: flex; align-items: center; gap: 9px; }
  .marca { font-weight: 700; font-size: 15px; letter-spacing: -0.02em; color: var(--ink); }
  .marca-box { display: flex; align-items: center; gap: 9px; }
  .marca-icon { width: 30px; height: 30px; border-radius: 8px; display: block; object-fit: cover; }

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
  .tabela-scroll { overflow-x: auto; max-width: 100%; }
  table.compacta { table-layout: fixed; font-size: 12.5px; width: max-content; min-width: 100%; }
  table.compacta th { padding: 8px 8px; font-size: 10px; }
  table.compacta td { padding: 5px 8px; font-size: 12.5px; }
  table.compacta .cel-data { width: 78px; color: var(--ink-soft); font-size: 11.5px; line-height: 1.25; }
  table.compacta .cel-desc { width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  table.compacta .cel-origem { width: 158px; overflow: hidden; color: var(--ink-soft); font-size: 11.5px; }
  table.compacta .cel-valor { width: 96px; }
  table.compacta .cel-check { width: 42px; text-align: center; }
  table.compacta .cel-obs { width: 130px; }
  table.compacta .cel-status { width: 44px; }
  table.compacta .cel-dim { width: 116px; }
  table.compacta select { width: 100%; max-width: 100%; padding: 4px 5px; font-size: 11.5px; border-radius: 5px; }
  table.compacta .obs-input { padding: 4px 6px; font-size: 11.5px; }

  /* ---------- colunas ajustaveis: redimensionar, reordenar, ordenar ---------- */
  /* sem rolagem: a tabela ocupa 100% e redimensionar uma coluna tira/da espaco
     da coluna vizinha, como planilha - nunca estoura a largura da tela. */
  table.ajustavel { width: 100% !important; min-width: 0 !important; table-layout: fixed; }
  table.ajustavel th[data-col] {
    position: relative; cursor: grab; user-select: none;
    font-size: 10px !important; font-weight: 600; line-height: 1.3; box-sizing: border-box;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  table.ajustavel th[data-col]:active { cursor: grabbing; }
  table.ajustavel th[data-col].arrastando { opacity: .4; }
  table.ajustavel th[data-col].arrastar-sobre { box-shadow: inset 2px 0 0 var(--accent); }
  /* divisor entre colunas - sempre visivel, destaca no hover pra indicar que da pra arrastar */
  table.ajustavel th[data-col]:not(:last-child) { border-right: 1px solid var(--line); }
  table.ajustavel th[data-col] .col-resize-handle {
    position: absolute; right: -5px; top: 0; bottom: 0; width: 10px; cursor: col-resize;
    z-index: 5;
  }
  table.ajustavel th[data-col] .col-resize-handle::after {
    content: ""; position: absolute; right: 4px; top: 15%; bottom: 15%; width: 2px;
    background: transparent; border-radius: 2px;
  }
  table.ajustavel th[data-col] .col-resize-handle:hover::after { background: var(--accent); }
  table.ajustavel th[data-col].sort-asc::after { content: " ▲"; font-size: 9px; color: var(--accent); }
  table.ajustavel th[data-col].sort-desc::after { content: " ▼"; font-size: 9px; color: var(--accent); }
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

  /* ---------- usuarios e permissoes ---------- */
  .perm-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 6px; }
  .perm-item {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px; cursor: pointer;
    border: 1px solid var(--line); border-radius: var(--radius-sm); font-size: 13px; background: var(--surface);
  }
  .perm-item:hover { border-color: var(--ink-faint); }
  .perm-item input { accent-color: var(--accent); width: 15px; height: 15px; }
  .perm-item:has(input:checked) { border-color: var(--accent); background: var(--accent-soft); }
  .tag-ativo, .tag-inativo {
    font-size: 10.5px; font-weight: 600; padding: 2px 7px; border-radius: 20px; text-transform: uppercase; letter-spacing: .03em;
  }
  .tag-ativo { background: var(--good-soft); color: var(--good); }
  .tag-inativo { background: var(--bad-soft); color: var(--bad); }
  .aviso-ok, .aviso-erro { padding: 11px 15px; border-radius: var(--radius); font-size: 13.5px; margin-bottom: 16px; }
  .aviso-ok { background: var(--good-soft); color: var(--good); border: 1px solid #cfe9d9; }
  .aviso-erro { background: var(--bad-soft); color: var(--bad); border: 1px solid #f2d3d0; }
  button:disabled { opacity: .45; cursor: not-allowed; }

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
        <div class="marca-box">
          <img class="marca-icon" src="data:image/png;base64,{LOGO_TOPBAR_B64}" alt="Pé de Meia">
          <div>
            <span class="marca">{APP_NOME}</span><br>
            <span class="marca-pagina">{titulo} · {session.get('user')}</span>
          </div>
        </div>
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
              {f'<a href="/categorias" class="{cls("categorias")}">Gerenciar categorias</a>' if pode("cadastros") else ""}
              {f'<a href="/grupos" class="{cls("grupos")}">Centro de Custos</a>' if pode("cadastros") else ""}
              {f'<a href="/dimensoes" class="{cls("dimensoes")}">Gerenciar dimensões</a>' if pode("cadastros") else ""}
              {f'<a href="/regras" class="{cls("regras")}">Regras automáticas</a>' if pode("cadastros") else ""}
              {f'<a href="/cartoes" class="{cls("cartoes")}">Gerenciar cartões</a>' if pode("cadastros") else ""}
              {f'<a href="/contas" class="{cls("contas")}">Gerenciar contas</a>' if pode("cadastros") else ""}
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
          // o widget so existe para quem tem permissao de sincronizar
          if (!document.getElementById('syncTexto')) return;
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
    err_html = '<p class="err">' + error + '</p>' if error else ''
    return f"""
    <html><head><title>Entrar · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      <div class="login-box">
        <div style="text-align:center;margin-bottom:6px">
          <img src="data:image/png;base64,{LOGO_HERO_B64}" alt="Pé de Meia" style="width:150px;height:auto;display:inline-block">
        </div>
        <h2 style="text-align:center">Pé de Meia</h2>
        <form method="post" autocomplete="on">
          <input name="usuario" placeholder="Usuario" autocomplete="username" autofocus>
          <div style="position:relative">
            <input id="campo-senha" name="senha" type="password" placeholder="Senha"
                   autocomplete="current-password" style="width:100%;padding-right:36px;box-sizing:border-box">
            <button type="button" onclick="mostrarSenha()"
                    style="position:absolute;right:6px;top:50%;transform:translateY(-50%);
                           background:none;border:none;cursor:pointer;font-size:15px;padding:2px 4px;color:var(--ink-faint)"
                    aria-label="Mostrar senha" title="Mostrar senha">👁</button>
          </div>
          <button type="submit">Entrar</button>
        </form>
        {err_html}
      </div>
      <script>
        function mostrarSenha() {{
          const campo = document.getElementById('campo-senha');
          campo.type = campo.type === 'password' ? 'text' : 'password';
        }}
      </script>
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

    cur.execute("SELECT vencimento_fatura FROM cartao.conta WHERE tipo = 'CREDIT' LIMIT 1;")
    conta_row = cur.fetchone()

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

    dia_vencimento = conta_row["vencimento_fatura"].day if conta_row and conta_row["vencimento_fatura"] else None
    proximo_fechamento = proxima_ocorrencia_dia(FATURA_DIA_FECHAMENTO)
    proximo_vencimento = proxima_ocorrencia_dia(dia_vencimento) if dia_vencimento else None

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
          <div class="card"><div class="label" title="Fechamento da fatura">Fechamento fatura</div><div class="val">Dia {FATURA_DIA_FECHAMENTO}</div><div class="sub">Próx: {proximo_fechamento.strftime('%d/%m/%y')}</div></div>
          <div class="card"><div class="label" title="Vencimento da fatura">Vencimento fatura</div><div class="val">{'Dia ' + str(dia_vencimento) if dia_vencimento else '-'}</div><div class="sub">{'Próx: ' + proximo_vencimento.strftime('%d/%m/%y') if proximo_vencimento else ''}</div></div>
        </div>

        <details class="cat-breakdown">
          <summary>Gasto por categoria (mês)</summary>
          <div class="det-body">{cat_rows_html}</div>
        </details>

        <div style="display:flex;justify-content:flex-end;margin-bottom:6px">
          <button type="button" class="ver-btn" onclick="redefinirColunas('lancamentos')"
                  title="Volta a ordem, largura e ordenação das colunas ao padrão">↺ Redefinir colunas</button>
        </div>
        <table class="compacta ajustavel" id="tabela-lancamentos">
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

        // ---- colunas ajustaveis: redimensionar, reordenar, ordenar por clique ----
        // Preferencias (ordem, largura, ordenacao) ficam salvas no navegador (localStorage),
        // por tabela - cada tela chama ativarTabelaAjustavel com sua propria chave.
        function redefinirColunas(chave) {{
          localStorage.removeItem('pedemeia_tabela_' + chave);
          window.location.reload();
        }}
        function ativarTabelaAjustavel(table, chave) {{
          const thead = table.querySelector('thead tr');
          const CHAVE = 'pedemeia_tabela_' + chave;
          // trava pra impedir que soltar o mouse depois de redimensionar dispare ordenacao.
          // a alca se move junto com a coluna enquanto arrasta, entao no momento de soltar
          // o clique pode acabar caindo em outro elemento - por isso a trava e checada no
          // handler do th (o que realmente importa), nao so via stopPropagation na alca.
          let redimensionandoAgora = false;

          function estadoSalvo() {{
            try {{ return JSON.parse(localStorage.getItem(CHAVE) || '{{}}'); }} catch (e) {{ return {{}}; }}
          }}
          const estado = estadoSalvo();

          function colunasNaOrdemAtual() {{
            return [...thead.querySelectorAll('th[data-col]')].map(th => th.dataset.col);
          }}
          function salvarEstado() {{ localStorage.setItem(CHAVE, JSON.stringify(estado)); }}

          function aplicarLargura(col, px) {{
            const th = thead.querySelector('th[data-col="' + col + '"]');
            if (th) th.style.width = px + 'px';
            table.querySelectorAll('td[data-col="' + col + '"]').forEach(td => {{ td.style.width = px + 'px'; }});
          }}

          function reordenarLinhas() {{
            const ordem = colunasNaOrdemAtual();
            table.querySelectorAll('tbody tr').forEach(tr => {{
              const mapaTd = {{}};
              tr.querySelectorAll('td[data-col]').forEach(td => {{ mapaTd[td.dataset.col] = td; }});
              ordem.forEach(col => {{ if (mapaTd[col]) tr.appendChild(mapaTd[col]); }});
            }});
          }}

          // ordem salva
          if (estado.ordem && estado.ordem.length) {{
            const mapaTh = {{}};
            thead.querySelectorAll('th[data-col]').forEach(th => {{ mapaTh[th.dataset.col] = th; }});
            estado.ordem.forEach(col => {{ if (mapaTh[col]) thead.appendChild(mapaTh[col]); }});
            reordenarLinhas();
          }}

          // sem rolagem: a soma das larguras tem que bater exatamente com a largura
          // da tabela (100% do container) - normaliza tanto a largura salva quanto a
          // padrao proporcionalmente, senao o navegador estoura a tabela pra caber
          // a soma das colunas (e volta a rolagem que queremos evitar).
          function normalizarParaCaber(larguras) {{
            const soma = Object.values(larguras).reduce((a, b) => a + b, 0);
            // NUNCA medir table.getBoundingClientRect() aqui: se a soma das larguras
            // declaradas passar do espaco disponivel, a tabela already fica mais larga
            // que o container (table-layout:fixed estoura pra caber as colunas) e a
            // "largura da tabela" mediria esse valor ja errado, sem nunca corrigir.
            // O pai (.wrap) nao estoura, entao ele sim reflete o espaco real disponivel.
            const alvo = table.parentElement.clientWidth;
            if (soma <= 0 || alvo <= 0) return larguras;
            const fator = alvo / soma;
            const normalizado = {{}};
            Object.keys(larguras).forEach(c => {{ normalizado[c] = Math.max(40, larguras[c] * fator); }});
            return normalizado;
          }}
          const larguraBase = {{}};
          thead.querySelectorAll('th[data-col]').forEach(th => {{
            larguraBase[th.dataset.col] = (estado.larguras && estado.larguras[th.dataset.col]) || th.getBoundingClientRect().width;
          }});
          const larguraFinal = normalizarParaCaber(larguraBase);
          Object.keys(larguraFinal).forEach(col => aplicarLargura(col, larguraFinal[col]));

          // redimensionar: alca na borda direita de cada th - arrastar tira/da espaco
          // da coluna vizinha (a soma nunca muda, entao a tabela nunca estoura)
          thead.querySelectorAll('th[data-col]').forEach(th => {{
            const alca = document.createElement('span');
            alca.className = 'col-resize-handle';
            alca.draggable = false;
            th.appendChild(alca);
            // sem isso, o clique de soltar o mouse depois de redimensionar tambem
            alca.addEventListener('click', function (e) {{ e.stopPropagation(); }});
            alca.addEventListener('mousedown', function (e) {{
              e.preventDefault();
              e.stopPropagation();
              const thVizinho = th.nextElementSibling;
              if (!thVizinho || !thVizinho.dataset.col) return;
              redimensionandoAgora = true;
              const startX = e.clientX;
              const startWidth = th.getBoundingClientRect().width;
              const startWidthVizinho = thVizinho.getBoundingClientRect().width;
              function mover(e2) {{
                const delta = e2.clientX - startX;
                const novaAtual = startWidth + delta;
                const novaVizinho = startWidthVizinho - delta;
                if (novaAtual < 40 || novaVizinho < 40) return;
                aplicarLargura(th.dataset.col, novaAtual);
                aplicarLargura(thVizinho.dataset.col, novaVizinho);
              }}
              function soltar() {{
                document.removeEventListener('mousemove', mover);
                document.removeEventListener('mouseup', soltar);
                estado.larguras = estado.larguras || {{}};
                estado.larguras[th.dataset.col] = th.getBoundingClientRect().width;
                estado.larguras[thVizinho.dataset.col] = thVizinho.getBoundingClientRect().width;
                salvarEstado();
                // a alca se move junto com a coluna durante o arraste, entao o "click"
                // que o navegador gera ao soltar o mouse pode cair fora dela (no proprio
                // th) - so o stopPropagation na alca nao e suficiente. A trava e resetada
                // so depois desse click (se houver) ja ter passado pelo handler do th.
                setTimeout(function () {{ redimensionandoAgora = false; }}, 0);
              }}
              document.addEventListener('mousemove', mover);
              document.addEventListener('mouseup', soltar);
            }});
          }});

          // reordenar: arrastar o cabecalho pra esquerda/direita
          let arrastando = null;
          thead.querySelectorAll('th[data-col]').forEach(th => {{
            th.draggable = true;
            th.addEventListener('dragstart', function () {{
              arrastando = th;
              th.classList.add('arrastando');
            }});
            th.addEventListener('dragend', function () {{
              th.classList.remove('arrastando');
              thead.querySelectorAll('th[data-col]').forEach(t => t.classList.remove('arrastar-sobre'));
            }});
            th.addEventListener('dragover', function (e) {{
              e.preventDefault();
              if (th !== arrastando) th.classList.add('arrastar-sobre');
            }});
            th.addEventListener('dragleave', function () {{ th.classList.remove('arrastar-sobre'); }});
            th.addEventListener('drop', function (e) {{
              e.preventDefault();
              th.classList.remove('arrastar-sobre');
              if (!arrastando || arrastando === th) return;
              const rect = th.getBoundingClientRect();
              const antes = (e.clientX - rect.left) < rect.width / 2;
              th.parentNode.insertBefore(arrastando, antes ? th : th.nextSibling);
              reordenarLinhas();
              estado.ordem = colunasNaOrdemAtual();
              salvarEstado();
            }});
          }});

          // ordenar: clicar no titulo da coluna (nao na alca de redimensionar)
          function valorOrdenavel(td) {{
            if (!td) return '';
            if (td.dataset.sort !== undefined && td.dataset.sort !== '') return parseFloat(td.dataset.sort);
            const sel = td.querySelector('select');
            if (sel) return (sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '').toLowerCase();
            const inp = td.querySelector('input[type=text]');
            if (inp) return inp.value.toLowerCase();
            return td.textContent.trim().toLowerCase();
          }}
          function ordenarLinhas(col, dir) {{
            const tbody = table.querySelector('tbody');
            const linhas = [...tbody.querySelectorAll('tr')];
            linhas.sort(function (a, b) {{
              const va = valorOrdenavel(a.querySelector('td[data-col="' + col + '"]'));
              const vb = valorOrdenavel(b.querySelector('td[data-col="' + col + '"]'));
              const cmp = (typeof va === 'number' && typeof vb === 'number') ? va - vb : String(va).localeCompare(String(vb));
              return dir === 'asc' ? cmp : -cmp;
            }});
            linhas.forEach(tr => tbody.appendChild(tr));
          }}
          function atualizarIndicadores() {{
            thead.querySelectorAll('th[data-col]').forEach(th => {{
              th.classList.remove('sort-asc', 'sort-desc');
              if (estado.sort && estado.sort.col === th.dataset.col) {{
                th.classList.add(estado.sort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
              }}
            }});
          }}
          thead.querySelectorAll('th[data-col]').forEach(th => {{
            th.addEventListener('click', function (e) {{
              if (redimensionandoAgora) return;
              if (e.target.classList.contains('col-resize-handle')) return;
              const col = th.dataset.col;
              const dir = (estado.sort && estado.sort.col === col && estado.sort.dir === 'asc') ? 'desc' : 'asc';
              estado.sort = {{ col: col, dir: dir }};
              salvarEstado();
              ordenarLinhas(col, dir);
              atualizarIndicadores();
            }});
          }});

          // ordenacao salva de uma sessao anterior
          if (estado.sort) {{
            ordenarLinhas(estado.sort.col, estado.sort.dir);
          }}
          atualizarIndicadores();
        }}
        ativarTabelaAjustavel(document.getElementById('tabela-lancamentos'), 'lancamentos');
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


@app.route("/cartoes", methods=["GET", "POST"])
@requer("cadastros")
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
        f'<tr><td>{esc(c["prefixo"])}</td><td>final {esc(c["final4"])}</td>'
        f'<td style="white-space:nowrap">'
        f'<a href="/cartoes?editar={c["final4"]}" class="ver-btn" style="text-decoration:none;margin-right:6px">Editar</a>'
        f'<form method="post" style="display:inline" onsubmit="return confirm(\'Excluir este cartao?\')">'
        f'<input type="hidden" name="acao" value="excluir"><input type="hidden" name="final4" value="{esc(c["final4"])}">'
        f'<button type="submit" class="ver-btn">Excluir</button></form></td></tr>'
        for c in cartoes_cadastrados
    ) or '<tr><td colspan="3" style="text-align:center;color:#888;padding:16px">Nenhum cartao cadastrado.</td></tr>'

    erro_html = f'<p class="err">{erro}</p>' if erro else ''
    cancelar_html = '<a href="/cartoes" style="margin-left:6px;font-size:13px">cancelar edicao</a>' if editando else ''

    return f"""
    <html><head><title>Gerenciar Cartoes · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Gerenciar Cartões', 'cartoes')}
      <div class="wrap">
        <div class="cat-breakdown">
          <h3>{titulo_form}{cancelar_html}</h3>
          <form method="post" style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
            <input type="hidden" name="acao" value="salvar">
            <input type="hidden" name="final4_original" value="{esc(form_final4)}">
            <div>
              <label style="font-size:13px;color:#555;display:block">Ultimos 4 digitos</label>
              <input name="final4" maxlength="4" placeholder="Ex: 9938" value="{esc(form_final4)}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px">
            </div>
            <div>
              <label style="font-size:13px;color:#555;display:block">Nome / prefixo (ex: Andrea - digital)</label>
              <input name="prefixo" placeholder="Ex: Andrea - digital" value="{esc(form_prefixo)}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:260px">
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

    def linha_valor(v):
        gasto = gasto_por_valor.get(v["id"], {})
        gasto_mes = float(gasto.get("gasto_mes") or 0)
        gasto_ano = float(gasto.get("gasto_ano") or 0)
        teto_mensal = float(v["teto_mensal"]) if v["teto_mensal"] is not None else None
        teto_anual = float(v["teto_anual"]) if v["teto_anual"] is not None else None
        barra_mensal = _barra_html(gasto_mes, teto_mensal)
        barra_anual = _barra_html(gasto_ano, teto_anual)
        progresso = (
            f'<div style="font-size:11.5px;color:var(--ink-faint)">'
            f'{_fmt_moeda(gasto_mes)} este mês{" de " + _fmt_moeda(teto_mensal) if teto_mensal else ""}</div>{barra_mensal}'
            if teto_mensal else
            (f'<div style="font-size:11.5px;color:var(--ink-faint)">{_fmt_moeda(gasto_mes)} este mês</div>' if gasto_mes else "")
        )
        progresso_ano = (
            f'<div style="font-size:11.5px;color:var(--ink-faint);margin-top:4px">'
            f'{_fmt_moeda(gasto_ano)} este ano de {_fmt_moeda(teto_anual)}</div>{barra_anual}'
            if teto_anual else ""
        )
        return (
            f'<tr><td style="padding-left:24px">'
            f'<form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
            f'<input type="hidden" name="acao" value="editar_valor"><input type="hidden" name="valor_id" value="{v["id"]}">'
            f'<input name="nome" value="{esc(v["nome"])}" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:200px">'
            f'<span style="font-size:12px;color:#888">teto mensal</span>'
            f'<input name="teto_mensal" value="{"" if teto_mensal is None else f"{teto_mensal:g}"}" placeholder="opcional" '
            f'style="width:100px;padding:6px 8px;border:1px solid #ccc;border-radius:6px">'
            f'<span style="font-size:12px;color:#888">teto anual</span>'
            f'<input name="teto_anual" value="{"" if teto_anual is None else f"{teto_anual:g}"}" placeholder="opcional" '
            f'style="width:100px;padding:6px 8px;border:1px solid #ccc;border-radius:6px">'
            f'<button type="submit" class="ver-btn">Salvar</button>'
            f'</form>{progresso}{progresso_ano}'
            f'</td>'
            f'<td style="vertical-align:top"><form method="post" onsubmit="return confirm(\'Excluir este valor?\')">'
            f'<input type="hidden" name="acao" value="excluir_valor"><input type="hidden" name="valor_id" value="{v["id"]}">'
            f'<button type="submit" class="ver-btn">Excluir</button></form></td></tr>'
        )

    blocos = []
    for d in dims:
        valores = valores_por_dim.get(d["id"], [])
        valores_rows = "".join(
            linha_valor(v)
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
              <input name="nome" value="{esc(d["nome"])}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:220px">
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
    <html><head><title>Gerenciar Dimensoes · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Gerenciar Dimensões', 'dimensoes')}
      <div class="wrap">
        <div style="font-size:13px;color:#666;margin-bottom:16px">
          Dimensoes sao classificacoes independentes do Centro de Custo, aplicadas a cada lancamento
          (ex: <strong>Responsavel</strong> - quem gastou, <strong>Projeto/Evento</strong> - a qual viagem ou evento pertence).
          Dimensoes marcadas como obrigatorias impedem confirmar (marcar como conferida) um lancamento sem esse vinculo preenchido.
          Cada valor pode ter um <strong>teto de gasto</strong> mensal e/ou anual (ex: "Ronaldo: R$3.000/mes") -
          o progresso do mes/ano corrente aparece embaixo do valor assim que houver um teto e algum gasto vinculado.
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
        key=lambda c: chave_alfa(cat_pt(c)),
    )
    categorias_por_subgrupo = {}
    for c in todas_categorias:
        sid = mapa_categoria.get(c)
        if sid:
            categorias_por_subgrupo.setdefault(sid, []).append(c)
    categorias_sem_vinculo = [c for c in todas_categorias if not mapa_categoria.get(c)]

    def chip_categoria(c):
        """Categoria ja vinculada a este subgrupo - clicar no x desvincula (some para 'sem centro de custo')."""
        return (
            f'<form method="post" style="display:inline-flex;align-items:center;gap:3px;background:var(--bg);'
            f'border:1px solid var(--line);border-radius:999px;padding:3px 3px 3px 10px;font-size:12px;margin:2px">'
            f'<input type="hidden" name="acao" value="mapear_categoria">'
            f'<input type="hidden" name="categoria" value="{esc(c)}">'
            f'<input type="hidden" name="subgrupo_id" value="">'
            f'{cat_pt(c)}'
            f'<button type="submit" title="Desvincular" style="border:none;background:none;cursor:pointer;'
            f'color:var(--ink-faint);font-weight:700;padding:2px 6px;font-size:13px">×</button>'
            f'</form>'
        )

    def select_adicionar_categoria(subgrupo_id):
        """Dropdown para vincular mais uma categoria a este subgrupo (move de onde estiver, se estiver em outro)."""
        opts = ['<option value="">+ vincular categoria…</option>']
        for c in todas_categorias:
            if mapa_categoria.get(c) == subgrupo_id:
                continue
            opts.append(f'<option value="{esc(c)}">{cat_pt(c)}</option>')
        return (
            f'<form method="post" style="display:inline-block">'
            f'<input type="hidden" name="acao" value="mapear_categoria">'
            f'<input type="hidden" name="subgrupo_id" value="{subgrupo_id}">'
            f'<select name="categoria" onchange="this.form.submit()" '
            f'style="padding:4px 6px;border:1px solid #ccc;border-radius:6px;font-size:12px;color:var(--ink-faint)">'
            f'{"".join(opts)}</select></form>'
        )

    linhas_html = []
    for g in grupos_db:
        subs = subgrupos_por_grupo.get(g["id"], [])
        linhas_html.append(f"""
        <tr style="background:var(--bg)">
          <td colspan="2" style="padding-top:18px">
            <form method="post" style="display:flex;gap:8px;align-items:center">
              <input type="hidden" name="acao" value="editar_grupo"><input type="hidden" name="grupo_id" value="{g["id"]}">
              <input name="nome" value="{esc(g["nome"])}" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;font-weight:700;font-size:14px;width:260px">
              <button type="submit" class="ver-btn">Salvar</button>
            </form>
          </td>
          <td style="padding-top:18px">
            <form method="post" onsubmit="return confirm('Excluir centro de custo e seus subgrupos?')">
              <input type="hidden" name="acao" value="excluir_grupo"><input type="hidden" name="grupo_id" value="{g["id"]}">
              <button type="submit" class="ver-btn">Excluir</button>
            </form>
          </td>
        </tr>
        """)
        for s in subs:
            chips = "".join(chip_categoria(c) for c in categorias_por_subgrupo.get(s["id"], []))
            linhas_html.append(f"""
            <tr>
              <td style="padding-left:22px;border-left:2px solid var(--line);position:relative">
                <span style="position:absolute;left:6px;color:var(--ink-faint);font-size:13px">└</span>
                <form method="post" style="display:flex;gap:6px;align-items:center">
                  <input type="hidden" name="acao" value="editar_subgrupo"><input type="hidden" name="subgrupo_id" value="{s["id"]}">
                  <input name="nome" value="{esc(s["nome"])}" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:200px">
                  <button type="submit" class="ver-btn">Salvar</button>
                </form>
              </td>
              <td style="max-width:340px">
                {chips}{select_adicionar_categoria(s["id"])}
              </td>
              <td>
                <form method="post" onsubmit="return confirm('Excluir subgrupo? As categorias vinculadas ficam sem centro de custo.')">
                  <input type="hidden" name="acao" value="excluir_subgrupo"><input type="hidden" name="subgrupo_id" value="{s["id"]}">
                  <button type="submit" class="ver-btn">Excluir</button>
                </form>
              </td>
            </tr>
            """)
        linhas_html.append(f"""
        <tr>
          <td colspan="3" style="padding-left:22px;border-left:2px solid var(--line);padding-bottom:16px">
            <form method="post" style="display:flex;gap:8px;align-items:center">
              <input type="hidden" name="acao" value="criar_subgrupo"><input type="hidden" name="grupo_id" value="{g["id"]}">
              <input name="nome" placeholder="Novo subgrupo" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:200px">
              <button type="submit" class="ver-btn">+ Adicionar subgrupo</button>
            </form>
          </td>
        </tr>
        """)

    sem_vinculo_html = "".join(
        f'<div class="cat-row"><span>{cat_pt(c)}</span>'
        f'<span><form method="post" style="display:inline">'
        f'<input type="hidden" name="acao" value="mapear_categoria"><input type="hidden" name="categoria" value="{esc(c)}">'
        f'<select name="subgrupo_id" onchange="this.form.submit()" style="padding:5px 7px;border:1px solid #ccc;border-radius:6px;font-size:12px">'
        f'<option value="">vincular a…</option>'
        + "".join(
            f'<optgroup label="{esc(g["nome"])}">' +
            "".join(f'<option value="{s["id"]}">{esc(s["nome"])}</option>' for s in subgrupos_por_grupo.get(g["id"], []))
            + '</optgroup>'
            for g in grupos_db if subgrupos_por_grupo.get(g["id"])
        ) +
        f'</select></form></span></div>'
        for c in categorias_sem_vinculo
    ) or '<div class="cat-row"><span>Todas as categorias têm um centro de custo definido.</span></div>'

    erro_html = f'<div class="aviso-erro">{erro}</div>' if erro else ""

    return f"""
    <html><head><title>Centro de Custos · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Centro de Custos', 'grupos')}
      <div class="wrap">
        {erro_html}
        <details class="cat-breakdown">
          <summary style="cursor:pointer;font-weight:600;font-size:13px;color:var(--ink-soft)">Como isso se relaciona com Categorias?</summary>
          <div style="font-size:13px;color:var(--ink-soft);line-height:1.7;margin-top:10px">
            <strong>Categoria</strong> vem do banco/Pluggy (ex: "Mercado", "Restaurantes") — é o que classifica cada
            lançamento individualmente, em <a href="/categorias">Gerenciar categorias</a>.
            <strong>Centro de Custo</strong> é uma camada acima, criada por você, pra agrupar várias categorias
            parecidas (ex: o centro de custo "Alimentação" pode juntar as categorias "Mercado" e "Restaurantes").
            Cada categoria pode estar vinculada a no máximo um subgrupo — é esse vínculo que você edita abaixo,
            na coluna "Categorias vinculadas". Um <strong>centro de custo</strong> tem um ou mais
            <strong>subgrupos</strong> (a árvore abaixo mostra isso: cada subgrupo aparece recuado, ligado por
            uma linha ao centro de custo dele). Para classificar por pessoa, projeto/evento ou outra dimensão
            independente da categoria — inclusive definir um <strong>teto de gasto</strong> por pessoa ou projeto —
            use <a href="/dimensoes">Gerenciar dimensões</a> em vez disso.
          </div>
        </details>
        <div class="cat-breakdown">
          <h3>Novo centro de custo</h3>
          <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="hidden" name="acao" value="criar_grupo">
            <input name="nome" placeholder="Nome do centro de custo" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:260px">
            <button type="submit" style="background:#1d2b3a;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer">Criar</button>
          </form>
        </div>

        <div class="cat-breakdown">
          <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:12px">
            Cada centro de custo (linha em destaque) tem um ou mais subgrupos (recuados, ligados por uma linha),
            e cada subgrupo reúne as categorias vinculadas a ele — clique no × pra desvincular, ou use o seletor
            pra vincular mais uma.
          </div>
          <div class="tabela-scroll">
          <table class="compacta">
            <thead><tr>
              <th>Centro de custo / Subgrupo</th><th>Categorias vinculadas</th><th>Remover</th>
            </tr></thead>
            <tbody>{"".join(linhas_html)}</tbody>
          </table>
          </div>
        </div>

        <div class="cat-breakdown">
          <h3>Categorias sem centro de custo</h3>
          <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:10px">
            Essas categorias ainda não entram em nenhum centro de custo — não aparecem nos totais por grupo do DRE.
          </div>
          {sem_vinculo_html}
        </div>
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

    origem_options = "".join(
        f'<option value="{val}">{completo}</option>' for val, _curto, completo in origem_opcoes
    )

    return f"""
    <html><head><title>Importar extrato / fatura · Pé de Meia</title>{BASE_CSS}</head>
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
            <div class="tabela-scroll">
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
          <table class="compacta">
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
        </div>

        {bloco_hist}
      </div>
    </body></html>
    """


@app.route("/contas", methods=["GET", "POST"])
@requer("cadastros")
def contas_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aviso = erro = None

    if request.method == "POST":
        item_id = request.form.get("item_id")
        titular = (request.form.get("titular") or "").strip()
        try:
            if not titular:
                cur.execute("DELETE FROM cartao.item_titular WHERE item_id = %s;", (item_id,))
                aviso = "Titular removido dessa conexão."
            else:
                cur.execute(
                    "INSERT INTO cartao.item_titular (item_id, titular) VALUES (%s,%s) "
                    "ON CONFLICT (item_id) DO UPDATE SET titular = EXCLUDED.titular;",
                    (item_id, titular),
                )
                aviso = f'Titular salvo: "{esc(titular)}".'
            conn.commit()
        except Exception as e:
            conn.rollback()
            erro = str(e)

    cur.execute(
        "SELECT c.item_id, c.account_id, c.tipo, c.nome, c.numero_final, p.connector_name, it.titular "
        "FROM cartao.conta c JOIN cartao.pluggy_item p ON p.item_id = c.item_id "
        "LEFT JOIN cartao.item_titular it ON it.item_id = c.item_id "
        "ORDER BY p.connector_name, c.tipo;"
    )
    linhas = cur.fetchall()
    cur.close()
    conn.close()

    conexoes = {}
    for r in linhas:
        item_id = str(r["item_id"])
        banco = detectar_banco(r["nome"], r["connector_name"])
        info = conexoes.setdefault(item_id, {"banco": banco, "titular": r["titular"], "contas": []})
        tipo_pt = {"CREDIT": "Cartão de crédito", "BANK": "Conta corrente", "MANUAL": "Dinheiro (manual)"}.get(r["tipo"], r["tipo"])
        detalhe = tipo_pt
        if r["numero_final"]:
            detalhe += f" · final {r['numero_final']}"
        info["contas"].append(detalhe)

    sugestoes = ["Ronaldo", "Andrea", "Ronaldo e Andrea", "Compartilhado"]
    datalist_html = "".join(f'<option value="{s}">' for s in sugestoes)

    def linha(item_id, info):
        selo = selo_banco_html(info["banco"])
        contas_txt = ", ".join(info["contas"])
        titular_atual = info["titular"] or ""
        return f"""
        <div class="cat-breakdown" style="padding:16px 18px">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
            <div style="display:flex;align-items:center;gap:9px">
              {selo}
              <div>
                <div style="font-weight:600;font-size:14px">{info["banco"]}</div>
                <div style="font-size:12px;color:var(--ink-faint)">{contas_txt}</div>
              </div>
            </div>
            <form method="post" style="display:flex;gap:8px;align-items:center">
              <input type="hidden" name="item_id" value="{item_id}">
              <input name="titular" list="sugestoes-titular" value="{esc(titular_atual)}" placeholder="De quem é essa conta?"
                     style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:220px">
              <button type="submit" class="ver-btn">Salvar</button>
            </form>
          </div>
        </div>
        """

    blocos = "".join(linha(item_id, info) for item_id, info in conexoes.items())
    aviso_html = f'<div class="aviso-ok">{aviso}</div>' if aviso else ""
    erro_html = f'<div class="aviso-erro">{erro}</div>' if erro else ""

    return f"""
    <html><head><title>Gerenciar contas · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Gerenciar contas', 'contas')}
      <div class="wrap">
        {aviso_html}{erro_html}
        <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:16px">
          Diga de quem é cada conta/conexão importada do banco. Isso aparece junto do nome do banco
          nos lançamentos, relatórios e em qualquer lugar que mostre a origem do dinheiro.
        </div>
        <datalist id="sugestoes-titular">{datalist_html}</datalist>
        {blocos}
      </div>
    </body></html>
    """


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
                    aviso = f'Categoria "{esc(nome)}" criada.'

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
                    aviso = f'Categoria renomeada para "{esc(novo_nome)}".'

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
                    aviso = f'{qtd} lançamento(s) movido(s) de "{cat_pt(origem)}" para "{cat_pt(destino)}".'

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
                    aviso = f'Categoria "{cat_pt(categoria)}" removida.'
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
        key=lambda c: chave_alfa(cat_pt(c)),
    )

    def opcoes_destino(atual):
        return "".join(
            f'<option value="{esc(c)}">{cat_pt(c)}</option>'
            for c in todas if c != atual
        )

    def linha(c):
        info = usadas.get(c)
        qtd = info["qtd"] if info else 0
        total = float(info["total"] or 0) if info else 0.0
        pode_excluir = qtd == 0
        btn_excluir = (
            f'<form method="post" onsubmit="return confirm(\'Remover a categoria {cat_pt(c)}?\')">'
            f'<input type="hidden" name="acao" value="excluir"><input type="hidden" name="categoria" value="{esc(c)}">'
            f'<button type="submit" class="ver-btn">Remover</button></form>'
            if pode_excluir else
            f'<button type="button" data-categoria="{esc(c)}" onclick="verLancamentosCategoria(this)" '
            f'data-tip="Existem lançamentos nessa categoria. Clique para ver quais são." '
            f'style="font-size:11px;color:var(--ink-faint);background:none;border:none;padding:0;'
            f'text-decoration:underline;cursor:pointer">{qtd} lanç. — protegida</button>'
        )
        nat = naturezas_atuais.get(c, NATUREZA_PADRAO)
        opts_natureza = "".join(
            f'<option value="{k}" {"selected" if k == nat else ""}>{v}</option>'
            for k, v in NATUREZAS.items()
        )
        aviso_nat = "" if nat == "despesa" else '<span style="color:var(--ink-faint);font-size:11px"> fora do resultado</span>' if nat in NATUREZAS_NEUTRAS else ""
        return f"""
        <tr>
          <td>
            <form method="post" style="display:flex;gap:6px;align-items:center">
              <input type="hidden" name="acao" value="renomear"><input type="hidden" name="categoria" value="{esc(c)}">
              <input name="novo_nome" value="{cat_pt(c)}" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:220px">
              <button type="submit" class="ver-btn">Salvar</button>
            </form>
            <div style="font-size:11px;color:var(--ink-faint);margin-top:2px">{esc(c)}</div>
          </td>
          <td class="valor">{qtd or "-"}</td>
          <td class="valor">{_fmt_moeda(total) if qtd else "-"}</td>
          <td>
            <form method="post" style="display:flex;gap:6px;align-items:center">
              <input type="hidden" name="acao" value="natureza"><input type="hidden" name="categoria" value="{esc(c)}">
              <select name="natureza" onchange="this.form.submit()" style="padding:5px 7px;font-size:12px">{opts_natureza}</select>
              {aviso_nat}
            </form>
          </td>
          <td>
            <form method="post" style="display:flex;gap:6px;align-items:center">
              <input type="hidden" name="acao" value="mover"><input type="hidden" name="origem" value="{esc(c)}">
              <select name="destino" style="padding:6px 8px;border:1px solid #ccc;border-radius:6px;max-width:180px">
                <option value="">mover lançamentos para…</option>
                {opcoes_destino(c)}
              </select>
              <button type="submit" class="ver-btn"{" disabled" if not qtd else ""}>Mover</button>
            </form>
          </td>
          <td>{btn_excluir}</td>
        </tr>
        """

    aviso_html = f'<div class="aviso-ok">{aviso}</div>' if aviso else ""
    erro_html = f'<div class="aviso-erro">{erro}</div>' if erro else ""

    return f"""
    <html><head><title>Gerenciar categorias · Pé de Meia</title>{BASE_CSS}</head>
    <body>
      {topbar_html('Gerenciar categorias', 'categorias')}
      <div class="wrap">
        {aviso_html}{erro_html}
        <div class="cat-breakdown">
          <h3>Nova categoria</h3>
          <form method="post" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="hidden" name="acao" value="criar">
            <input name="nome" placeholder="Nome da categoria" style="padding:7px 9px;border:1px solid #ccc;border-radius:6px;width:240px">
            <button type="submit" style="background:#1d2b3a;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer">Criar categoria</button>
          </form>
        </div>
        <details class="cat-breakdown">
          <summary style="cursor:pointer;font-weight:600;font-size:13px;color:var(--ink-soft)">O que é cada natureza?</summary>
          <div style="font-size:13px;color:var(--ink-soft);line-height:1.7;margin-top:10px">
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
        </details>
        <div class="cat-breakdown">
          <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:12px">
            Renomeie, defina a natureza contábil, mova lançamentos entre categorias ou remova categorias vazias.
            Uma categoria só pode ser removida quando não tiver nenhum lançamento — mova os lançamentos para outra
            categoria primeiro, usando a coluna "Mover".
          </div>
          <div class="tabela-scroll">
          <table class="compacta">
            <thead><tr>
              <th>Categoria</th><th style="text-align:right">Lanç.</th>
              <th style="text-align:right">Total</th><th>Natureza</th><th>Mover lançamentos</th><th>Remover</th>
            </tr></thead>
            <tbody>{"".join(linha(c) for c in todas)}</tbody>
          </table>
          </div>
        </div>
      </div>

      <div class="modal-bg" id="modalLancBg" onclick="if(event.target===this) fecharModalLanc()">
        <div class="modal" style="width:520px">
          <span class="close" onclick="fecharModalLanc()">&times;</span>
          <h3 id="modalLancTitulo">Lançamentos</h3>
          <div id="modalLancBody" style="max-height:60vh;overflow-y:auto"></div>
        </div>
      </div>
      <script>
        function escHtml(s) {{
          return String(s ?? '').replace(/[&<>"']/g, c => ({{
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
          }})[c]);
        }}
        function fecharModalLanc() {{
          document.getElementById('modalLancBg').classList.remove('show');
        }}
        function verLancamentosCategoria(btn) {{
          const categoria = btn.dataset.categoria;
          const corpo = document.getElementById('modalLancBody');
          document.getElementById('modalLancTitulo').textContent = 'Lançamentos — ' + btn.closest('tr').querySelector('input[name=novo_nome]').value;
          corpo.innerHTML = '<div style="padding:12px 0;color:var(--ink-faint);font-size:13px">Carregando…</div>';
          document.getElementById('modalLancBg').classList.add('show');
          fetch('/api/categoria-lancamentos?categoria=' + encodeURIComponent(categoria))
            .then(r => r.json())
            .then(lista => {{
              if (!lista.length) {{
                corpo.innerHTML = '<div style="padding:12px 0;color:var(--ink-faint);font-size:13px">Nenhum lançamento encontrado.</div>';
                return;
              }}
              corpo.innerHTML = lista.map(l =>
                '<div class="row"><span>' + escHtml(l.data) + ' — ' + escHtml(l.descricao) + '</span>' +
                '<span>R$ ' + l.valor.toLocaleString('pt-BR', {{minimumFractionDigits:2, maximumFractionDigits:2}}) + '</span></div>'
              ).join('');
            }})
            .catch(() => {{
              corpo.innerHTML = '<div style="padding:12px 0;color:var(--bad)">Erro ao carregar os lançamentos.</div>';
            }});
        }}
      </script>
    </body></html>
    """


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
