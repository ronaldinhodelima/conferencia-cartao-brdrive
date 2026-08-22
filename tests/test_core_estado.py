"""Garante que o estado compartilhado do core continua visivel para os blueprints.

Cada modulo de views faz `from core import CATEGORIA_PT_DB`, o que guarda uma
referencia ao dicionario. Se recarregar_categorias_db() REATRIBUISSE a variavel
(como fazia antes dos blueprints), os modulos seguiriam apontando para o
dicionario velho e os nomes de categoria congelariam na versao do boot - sem
erro nenhum, so nome errado na tela.

Por isso o teste olha a identidade do objeto, nao so o conteudo.
"""
import app
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


class TestFiltroRelatorio:
    """_montar_filtro_relatorio le direto da querystring, entao precisa aguentar
    qualquer coisa que chegue pela URL sem estourar."""

    def _cfg(self, qs):
        with app.app.test_request_context(qs):
            return core._montar_filtro_relatorio([])

    def test_agrupar_invalido_cai_no_padrao_em_vez_de_estourar(self):
        for ruim in ("dim_abc", "dim_", "dim_1;DROP TABLE x--", "xxx", ""):
            cfg = self._cfg(f"/relatorios/dados?agrupar={ruim}")
            assert cfg["agrupar"] == "categoria"
            assert cfg["group_expr"] == "t.categoria"

    def test_agrupar_por_dimensao_valida_continua_funcionando(self):
        cfg = self._cfg("/relatorios/dados?agrupar=dim_7")
        assert cfg["agrupar"] == "dim_7"
        assert "tdg.dimensao_id = 7" in cfg["join_extra"]

    def test_dimensao_nao_numerica_nunca_entra_na_sql(self):
        cfg = self._cfg("/relatorios/dados?agrupar=dim_1 OR 1=1")
        assert "OR 1=1" not in cfg["join_extra"]

    def test_data_invalida_vira_filtro_vazio_em_vez_de_500(self):
        # o Postgres rejeita texto que nao e data e o endpoint AJAX virava 500,
        # deixando a tela presa em "Carregando..."
        for ruim in ("abc", "2026-99-99", "2026-02-30", "'; DROP TABLE x--", "  "):
            cfg = self._cfg(f"/relatorios/dados?data_ini={ruim}&data_fim={ruim}")
            assert cfg["data_ini"] == ""
            assert cfg["data_fim"] == ""

    def test_data_valida_passa_inteira(self):
        cfg = self._cfg("/relatorios/dados?data_ini=2026-07-01&data_fim=2026-07-31")
        assert cfg["data_ini"] == "2026-07-01"
        assert cfg["data_fim"] == "2026-07-31"

    def test_data_sem_zero_a_esquerda_e_aceita(self):
        # "2026-7-1" e data valida para o strptime e para o Postgres; nao ha
        # motivo para recusar so porque o <input type=date> nunca gera assim
        cfg = self._cfg("/relatorios/dados?data_ini=2026-7-1")
        assert cfg["data_ini"] == "2026-7-1"


class TestNomeDeCategoriaUnico:
    """Renomear categoria so troca o apelido - a chave do Pluggy continua distinta.
    Sem checagem, duas categorias diferentes ficam com o mesmo nome na tela e o
    relatorio passa a mostrar linhas repetidas, cada uma com sua natureza."""

    def setup_method(self):
        self.antes = dict(core.CATEGORIA_PT_DB)
        core.CATEGORIA_PT_DB.clear()
        core.CATEGORIA_PT_DB.update({"Parking": "Estacionamento", "Shopping": "Compras"})

    def teardown_method(self):
        core.CATEGORIA_PT_DB.clear()
        core.CATEGORIA_PT_DB.update(self.antes)

    def test_acusa_nome_ja_usado_por_outra_categoria(self):
        assert core.categoria_com_nome("Estacionamento") == "Parking"

    def test_ignora_acento_e_caixa(self):
        # para quem le a tela, "Compras" e "COMPRAS" sao o mesmo nome
        assert core.categoria_com_nome("COMPRAS") == "Shopping"
        assert core.categoria_com_nome("compras") == "Shopping"

    def test_renomear_para_o_proprio_nome_nao_e_conflito(self):
        assert core.categoria_com_nome("Estacionamento", exceto="Parking") is None

    def test_nome_livre_passa(self):
        assert core.categoria_com_nome("Categoria Inedita 123") is None

    def test_nome_vazio_nao_acusa(self):
        assert core.categoria_com_nome("") is None
        assert core.categoria_com_nome("   ") is None

    def test_categoria_oculta_nao_bloqueia_o_nome(self):
        # categoria escondida nao aparece em lista nenhuma, entao o nome dela
        # esta livre para ser reaproveitado
        core.CATEGORIAS_OCULTAS.add("Parking")
        try:
            assert core.categoria_com_nome("Estacionamento") is None
        finally:
            core.CATEGORIAS_OCULTAS.discard("Parking")


def test_recarregar_nunca_deixa_o_dicionario_vazio(monkeypatch):
    """Com threads compartilhando o mesmo dicionario, um clear() seguido de
    update() abriria uma janela em que outra requisicao leria o dicionario vazio
    e mostraria a chave crua do Pluggy no lugar do nome da categoria.

    O teste espia o dicionario durante a recarga: em nenhum momento ele pode
    ficar sem os nomes que continuam existindo.
    """
    core.CATEGORIA_PT_DB.clear()
    core.CATEGORIA_PT_DB.update({"Parking": "Estacionamento", "Sai": "Vai Sumir"})

    vistos = []

    class CursorEspiao(FakeCursor):
        def fetchall(self):
            vistos.append(dict(core.CATEGORIA_PT_DB))
            return super().fetchall()

    cur = CursorEspiao([[("Parking", "Estacionamento")], []])
    monkeypatch.setattr(core, "get_conn", lambda: FakeConn(cur))
    core.recarregar_categorias_db()

    assert all("Parking" in v for v in vistos), "o nome sumiu durante a recarga"
    assert core.CATEGORIA_PT_DB == {"Parking": "Estacionamento"}
    assert "Sai" not in core.CATEGORIA_PT_DB, "o que saiu do banco tem que sair daqui"
