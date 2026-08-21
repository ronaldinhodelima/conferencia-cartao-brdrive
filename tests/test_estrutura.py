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
