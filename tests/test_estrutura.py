"""Checagens estruturais do projeto, feitas em cima do AST.

Existem porque py_compile e os testes de template nao pegam esta classe de erro:
um nome usado mas nao importado so estoura em tempo de execucao, na hora em que
aquela linha roda - que pode ser meses depois, na producao. Aconteceu nesta
sessao duas vezes (um helper apagado junto com um corte, e um import removido
achando que estava sem uso).
"""
import ast
import builtins
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parent.parent
MODULOS = [RAIZ / "app.py", RAIZ / "core.py", *sorted((RAIZ / "views").glob("*.py"))]

# dunders que o Python injeta no modulo e nao aparecem como atribuicao
INJETADOS = {"__file__", "__name__", "__doc__", "__package__"}


def nomes_definidos(arvore):
    achados = set(dir(builtins)) | INJETADOS
    for n in ast.walk(arvore):
        if isinstance(n, ast.Import):
            for a in n.names:
                achados.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                achados.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            achados.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            achados.add(n.id)
        elif isinstance(n, ast.arg):
            achados.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            achados.add(n.name)
        elif isinstance(n, ast.Global):
            achados.update(n.names)
    return achados


def test_nenhum_nome_usado_sem_estar_definido_ou_importado():
    problemas = {}
    for caminho in MODULOS:
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        definidos = nomes_definidos(arvore)
        faltando = sorted({
            n.id for n in ast.walk(arvore)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in definidos
        })
        if faltando:
            problemas[caminho.name] = faltando
    assert not problemas, f"nome sem import/definicao: {problemas}"


def test_core_nao_importa_das_views_nem_do_app():
    """A dependencia so pode correr app.py -> views/ -> core.py."""
    arvore = ast.parse((RAIZ / "core.py").read_text(encoding="utf-8"))
    for n in ast.walk(arvore):
        modulo = None
        if isinstance(n, ast.ImportFrom):
            modulo = n.module or ""
        elif isinstance(n, ast.Import):
            modulo = n.names[0].name
        if modulo and (modulo.startswith("views") or modulo == "app"):
            raise AssertionError(f"core.py importa {modulo} - volta o import circular")


def test_views_nao_importam_umas_das_outras():
    for caminho in (RAIZ / "views").glob("*.py"):
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        for n in ast.walk(arvore):
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("views"):
                raise AssertionError(f"{caminho.name} importa de outra view: {n.module}")


def test_nenhuma_tela_monta_html_por_fstring():
    """Todo HTML tem que morar em templates/. Se voltar f-string, e regressao."""
    culpados = []
    for caminho in MODULOS:
        texto = caminho.read_text(encoding="utf-8")
        if "<html>" in texto or "<body>" in texto:
            culpados.append(caminho.name)
    assert not culpados, f"HTML em Python: {culpados}"


def test_todas_as_rotas_continuam_registradas():
    import app

    rotas = {str(r) for r in app.app.url_map.iter_rules() if r.endpoint != "static"}
    esperadas = {
        "/", "/login", "/logout", "/health",
        "/api/sync-status", "/api/sync-agora", "/api/transacao/<transacao_id>",
        "/api/lancamento-manual", "/api/lancamento-manual/<transacao_id>",
        "/api/categoria-lancamentos",
        "/relatorios", "/relatorios/dados", "/relatorios/lancamentos",
        "/dre", "/investimentos",
        "/categorias", "/grupos", "/dimensoes", "/regras", "/contas", "/pendencias",
        "/usuarios",
    }
    assert rotas == esperadas


def test_tojson_nunca_dentro_de_atributo_html():
    """|tojson e o escape certo para dentro de <script>, e errado dentro de atributo.

    O filtro do Flask nao escapa aspas duplas, entao {{ x|tojson }} num atributo
    delimitado por aspas duplas FECHA o atributo antes da hora:

        onclick="f(event, {{ id|tojson }})"  ->  onclick="f(event, "abc")"

    O navegador le onclick como 'f(event, ' - erro de sintaxe, o handler nunca
    roda. Foi assim que a tela de Lancamentos parou de abrir os detalhes e de
    salvar as edicoes, sem erro nenhum aparecer. Para passar dado ao JS: use
    data-attribute + delegacao, ou um bloco <script type="application/json">.
    """
    import re

    padrao = re.compile(r'=\s*"[^"\n]*\{\{[^}]*\|\s*tojson')
    culpados = []
    for caminho in sorted((RAIZ / "templates").glob("*.html")):
        for numero, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), 1):
            if padrao.search(linha):
                culpados.append(f"{caminho.name}:{numero}")
    assert not culpados, f"|tojson dentro de atributo HTML: {culpados}"


def test_nenhum_handler_inline_recebe_id_interpolado():
    """Handlers inline com dado interpolado sao a origem do bug acima.

    Os eventos da tabela de Lancamentos passaram a ser tratados por delegacao,
    lendo o id do data-id da linha. Isto trava a volta do padrao antigo.
    """
    import re

    html = (RAIZ / "templates" / "index.html").read_text(encoding="utf-8")
    suspeitos = re.findall(r'on\w+="[^"]*\{\{[^}]*\br\.id\b[^}]*\}\}[^"]*"', html)
    assert not suspeitos, f"handler inline com o id da linha: {suspeitos}"


def test_posicao_da_pagina_e_mantida_em_todas_as_telas():
    """Salvar reenvia o form e a view devolve a pagina inteira, entao o navegador
    voltaria ao topo a cada alteracao.

    A ativacao e automatica no tabelas.js (carregado por todas as telas via
    base.html) em vez de uma chamada por template - assim tela nova ja nasce com
    o comportamento certo, sem depender de alguem lembrar.
    """
    tabelas = (RAIZ / "static" / "tabelas.js").read_text(encoding="utf-8")
    assert "function manterPosicaoAoSalvar" in tabelas
    assert "addEventListener('DOMContentLoaded', manterPosicaoAoSalvar)" in tabelas
    assert '<script src="/static/tabelas.js">' in (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")


def test_todo_reload_por_js_guarda_a_posicao_antes():
    """window.location.reload() nao dispara submit, entao a posicao nao seria
    guardada sozinha - cada reload por codigo precisa chamar guardarPosicaoAtual()
    antes."""
    import re

    problemas = []
    for caminho in sorted((RAIZ / "static").glob("*.js")):
        if caminho.name == "chart.umd.min.js":
            continue
        # comentario que apenas menciona reload nao conta
        texto = "\n".join(
            "" if l.lstrip().startswith("//") else l
            for l in caminho.read_text(encoding="utf-8").splitlines()
        )
        for m in re.finditer(r"location\.reload\(\)", texto):
            antes = texto[max(0, m.start() - 220):m.start()]
            if "guardarPosicaoAtual()" not in antes:
                linha = texto[:m.start()].count("\n") + 1
                problemas.append(f"{caminho.name}:{linha}")
    assert not problemas, f"reload sem guardar a posicao antes: {problemas}"


def test_selo_do_banco_nao_e_escapado_no_filtro_de_origem():
    """O filtro de Origem mostra o selo colorido do banco antes do nome da conta.

    O selo e HTML montado por selo_banco_html(). Quando a varredura de XSS passou
    a escapar o texto da opcao, o selo vinha concatenado nesse texto e o usuario
    passou a ver a marcacao crua ('<SPAN CLASS="SELO"...') dentro do dropdown.
    Por isso o selo viaja num campo separado da tupla de opcoes.
    """
    import app  # noqa: F401
    import core

    with app.app.test_request_context("/"):
        html = core.chip_filter_html(
            "origem", "Origem",
            [("a1", 'Conta <b>X</b>', "titulo", "curto", '<span class="selo">Nu</span>')],
            [],
        )
    assert '<span class="selo">Nu</span>' in html, "o selo tem que renderizar como HTML"
    assert "&lt;span" not in html, "o selo nao pode aparecer escapado"
    assert "&lt;b&gt;" in html, "o nome da conta continua escapado"


def test_filtro_de_tabela_existe_e_e_automatico():
    """O campo de filtro e injetado pelo tabelas.js junto com a barra de colunas,
    entao vale para toda tabela marcada como ajustavel - inclusive as que vierem
    depois, sem precisar mexer no template."""
    js = (RAIZ / "static" / "tabelas.js").read_text(encoding="utf-8")
    assert "function ativarFiltroTabela" in js
    assert "ativarFiltroTabela(table, busca, contador)" in js
    assert "placeholder = 'Filtrar'" in js

    # o texto da linha nao pode sair do textContent puro: as celulas trazem
    # <select> cujas opcoes listam todas as categorias, e aí qualquer busca
    # casaria com todas as linhas
    assert "clone.querySelectorAll('select').forEach" in js
