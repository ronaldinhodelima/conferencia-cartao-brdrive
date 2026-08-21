"""Garante que o estado compartilhado do core continua visivel para os blueprints.

Cada modulo de views faz `from core import CATEGORIA_PT_DB`, o que guarda uma
referencia ao dicionario. Se recarregar_categorias_db() REATRIBUISSE a variavel
(como fazia antes dos blueprints), os modulos seguiriam apontando para o
dicionario velho e os nomes de categoria congelariam na versao do boot - sem
erro nenhum, so nome errado na tela.

Por isso o teste olha a identidade do objeto, nao so o conteudo.
"""
import core


class FakeCursor:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.atual = []

    def execute(self, sql, *a):
        self.atual = self.respostas.pop(0)

    def fetchall(self):
        return self.atual

    def close(self):
        pass


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **k):
        return self._cur

    def close(self):
        pass


def test_recarregar_altera_no_lugar_sem_trocar_o_objeto(monkeypatch):
    ref_nomes = core.CATEGORIA_PT_DB
    ref_ocultas = core.CATEGORIAS_OCULTAS

    cur = FakeCursor([[("Groceries", "Mercado")], [("Fuel",)]])
    monkeypatch.setattr(core, "get_conn", lambda: FakeConn(cur))
    core.recarregar_categorias_db()

    # mesmo objeto -> quem importou por `from core import ...` enxerga a mudanca
    assert core.CATEGORIA_PT_DB is ref_nomes
    assert core.CATEGORIAS_OCULTAS is ref_ocultas
    assert ref_nomes["Groceries"] == "Mercado"
    assert "Fuel" in ref_ocultas


def test_recarregar_limpa_o_que_saiu_do_banco(monkeypatch):
    core.CATEGORIA_PT_DB["Removida"] = "Some Depois"
    cur = FakeCursor([[("Groceries", "Mercado")], []])
    monkeypatch.setattr(core, "get_conn", lambda: FakeConn(cur))
    core.recarregar_categorias_db()
    assert "Removida" not in core.CATEGORIA_PT_DB


def test_falha_de_banco_nao_zera_o_que_ja_estava_carregado(monkeypatch):
    core.CATEGORIA_PT_DB.clear()
    core.CATEGORIA_PT_DB["Groceries"] = "Mercado"

    def explode():
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(core, "get_conn", explode)
    core.recarregar_categorias_db()
    # se o banco cai, a tela continua mostrando os nomes que ja tinha
    assert core.CATEGORIA_PT_DB["Groceries"] == "Mercado"
