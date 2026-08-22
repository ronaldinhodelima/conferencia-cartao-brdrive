"""Nucleo compartilhado: constantes, acesso ao banco, permissoes e helpers de HTML.

Nao importa nada de views/ nem de app.py. A dependencia corre sempre na mesma
direcao - app.py -> views/ -> core.py - o que impede import circular.
"""
import os
import functools
import hashlib
import html
import json
import secrets
import unicodedata
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import request, redirect, session, jsonify, render_template


# URL do servico bussola-financeira-app que faz a sincronizacao com o Pluggy.
# Pode ser sobrescrita via env var caso o dominio mude.
BUSSOLA_SYNC_URL = os.environ.get(
    "BUSSOLA_SYNC_URL", "https://hdgffcvh3ljqe61dczztaycz.coolify.brdrive.net/sync"
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


PERMISSOES = {
    "lancamentos_ver": ("Ver lançamentos", "Abrir a tela de lançamentos e consultar o que foi gasto."),
    "lancamentos_editar": ("Editar lançamentos", "Mudar categoria, responsável, projeto, observação e marcar duplicadas."),
    "lancamentos_conferir": ("Conferir lançamentos", "Marcar um lançamento como conferido."),
    "lancamentos_manual": ("Lançar dinheiro manual", "Criar e excluir lançamentos em espécie."),
    "relatorios": ("Ver relatórios", "Relatórios, DRE e investimentos."),
    "cadastros": ("Gerenciar cadastros", "Grupos de custo, dimensões, regras automáticas, cartões e naturezas."),
    "sincronizar": ("Sincronizar com o banco", "Usar o botão Atualizar agora."),
    "usuarios": ("Gerenciar usuários", "Criar usuários, trocar senhas e definir permissões."),
}


PERFIS = {
    "admin": ("Administrador", list(PERMISSOES.keys())),
    "operador": ("Operador", [
        "lancamentos_ver", "lancamentos_editar", "lancamentos_conferir",
        "lancamentos_manual", "relatorios", "sincronizar",
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
                return render_template(
                    "sem_permissao.html",
                    titulo="Sem permissão",
                    topbar=topbar_html("Sem permissão"),
                    permissao=titulo,
                ), 403
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


CATEGORIAS_NAO_GASTO = ("Credit card payment", "Transfer - Internal")


CATEGORIAS_EXTRA = (
    "BRDrive", "Agua / Gas", "Natacao", "Academia", "Viagem",
    "Imóveis / Terrenos", "Veículos / Bens",
)


NATUREZAS = {
    "receita": "Receita",
    "despesa": "Despesa",
    "investimento": "Investimento",
    "bem": "Aquisição de bem",
    "transferencia": "Transferência",
    "fluxo": "Depende da direção",
}


NATUREZA_PADRAO = "despesa"


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


CATEGORIAS_NEUTRAS_PADRAO = {
    c for c, n in SEED_NATUREZAS.items() if n in NATUREZAS_NEUTRAS
}


CONTA_MANUAL_ID = "00000000-0000-0000-0000-000000000002"


APP_NOME = "Pé de Meia"


MESES_ABREV = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


JOIN_NATUREZA = (
    " JOIN cartao.conta c ON c.account_id = t.account_id "
    " LEFT JOIN cartao.categoria_natureza n ON n.categoria = t.categoria "
)


VAL_DESPESA = (
    "(CASE WHEN c.tipo = 'CREDIT' THEN COALESCE(t.valor_brl, t.valor_original) "
    "ELSE -COALESCE(t.valor_brl, t.valor_original) END)"
)


_NAT_BASE = "COALESCE(t.natureza, n.natureza, '" + NATUREZA_PADRAO + "')"


NATUREZA_SQL = (
    "(CASE WHEN " + _NAT_BASE + " = 'fluxo' "
    "THEN (CASE WHEN " + VAL_DESPESA + " > 0 THEN 'despesa' ELSE 'receita' END) "
    "ELSE " + _NAT_BASE + " END)"
)


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
        # (valor, texto puro, titulo do tooltip, texto curto, selo em HTML)
        # o selo vai separado porque e HTML do proprio app: junto com o texto ele
        # seria escapado e o usuario veria a marcacao crua no filtro
        opcoes.append((aid, curto, completo, curto, selo))
    return contas_by_id, opcoes


IMPORT_NAMESPACE = uuid.UUID("6f1c2a52-0000-4000-8000-000000000042")


def chip_filter_html(nome, label, opcoes, selecionados, onchange="aplicarFiltros()"):
    """Filtro em chip com dropdown, busca e multi-selecao.

    opcoes: (valor, texto) e, opcionalmente, mais (titulo, texto_curto, selo_html).

    O texto e sempre escapado - vem do banco. O selo, quando existe, e HTML
    montado por selo_banco_html() e entra cru; por isso vem num campo separado,
    e nao concatenado no texto.
    """
    n_sel = len(selecionados)
    partes = []
    for opt in opcoes:
        val, texto = opt[0], opt[1]
        titulo = opt[2] if len(opt) > 2 else texto
        curto = opt[3] if len(opt) > 3 else None
        selo = opt[4] if len(opt) > 4 else ""
        marcado = "checked" if str(val) in selecionados else ""
        attr_curto = f' data-curto="{esc(curto)}"' if curto else ""
        partes.append(
            f'<label class="chip-opt" data-tip="{esc(titulo)}"{attr_curto}>'
            f'<input type="checkbox" name="{nome}" value="{esc(val)}" {marcado} '
            f'onchange="{onchange}"> {selo}{esc(texto)}</label>'
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


CATEGORIA_PT_DB = {}


CATEGORIAS_OCULTAS = set()


def recarregar_categorias_db():
    """Atualiza os apelidos e as categorias ocultas a partir do banco.

    Altera os dois dicionarios NO LUGAR, em vez de reatribui-los. Isso importa
    porque outros modulos fazem `from core import CATEGORIA_PT_DB`: se aqui
    reatribuisse, esses modulos continuariam enxergando o dicionario antigo e
    os nomes de categoria congelariam na versao do boot.
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT categoria, nome_pt FROM cartao.categoria;")
        novos = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT categoria FROM cartao.categoria_oculta;")
        ocultas = {r[0] for r in cur.fetchall()}
        cur.close()
        conn.close()
    except Exception:
        return
    CATEGORIA_PT_DB.clear()
    CATEGORIA_PT_DB.update(novos)
    CATEGORIAS_OCULTAS.clear()
    CATEGORIAS_OCULTAS.update(ocultas)


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


def _montar_filtro_relatorio(dimensoes):
    """Le os filtros da querystring (request.args) e monta where/params/group_expr reutilizaveis
    tanto pela pagina quanto pelos endpoints de dados (AJAX)."""
    categorias_sel = request.args.getlist("categoria")
    cartoes_sel = request.args.getlist("cartao")
    origens_sel = request.args.getlist("origem")
    # a data vai como parametro para o Postgres, que rejeita texto invalido com
    # erro - e o endpoint AJAX virava 500, deixando a tela em "Carregando..."
    # para sempre. Data que nao for AAAA-MM-DD e tratada como filtro nao
    # preenchido, que e o comportamento util aqui.
    def _data_valida(valor):
        valor = (valor or "").strip()
        if not valor:
            return ""
        try:
            datetime.strptime(valor, "%Y-%m-%d")
        except ValueError:
            return ""
        return valor

    data_ini = _data_valida(request.args.get("data_ini"))
    data_fim = _data_valida(request.args.get("data_fim"))
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
    elif agrupar.startswith("dim_") and agrupar.split("_", 1)[1].isdigit():
        # o int() ja impede injecao, mas sozinho ele estoura ValueError (500) em
        # "agrupar=dim_abc", que qualquer um alcanca editando a URL. Com o isdigit
        # na condicao, entrada invalida cai no else e vira o agrupamento padrao.
        dim_id_grp = int(agrupar.split("_", 1)[1])
        join_extra = (
            f"LEFT JOIN cartao.transacao_dimensao tdg ON tdg.transacao_id = t.transacao_id::text "
            f"AND tdg.dimensao_id = {dim_id_grp} LEFT JOIN cartao.dimensao_valor dvg ON dvg.id = tdg.valor_id"
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
