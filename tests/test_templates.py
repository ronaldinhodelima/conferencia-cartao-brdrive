"""Renderiza os templates com o formato REAL dos dados que a view entrega.

Existe por causa de um bug que passou despercebido: /importar desempacotava 3
valores de uma tupla que carregar_origens() devolve com 4 desde o commit fcedcf1.
A tela ficou dando 500 e ninguem viu, porque ela nao esta mais no menu.

Testar com dado inventado nao pega esse tipo de erro - o formato aqui tem que
espelhar o que a funcao realmente devolve.
"""
import pytest

import app


@pytest.fixture
def ctx():
    with app.app.test_request_context("/"):
        yield


# (account_id, html com selo, label completo, label curto) - ver carregar_origens()
ORIGEM_OPCOES = [
    ("acc-1", '<span class="selo">UN</span>Unicred CC', "Unicred · Conta corrente · Ronaldo", "Unicred CC"),
    ("acc-2", '<span class="selo">NU</span>Nubank', "Nubank · Cartão de crédito · Andrea", "Nubank"),
]


class TestImportar:
    def test_renderiza_com_a_tupla_real_de_4_elementos(self, ctx):
        html = app.render_template(
            "importar.html", titulo="Importar", topbar="", origem_opcoes=ORIGEM_OPCOES
        )
        assert html.count("<option") == 2
        assert "Unicred · Conta corrente · Ronaldo" in html

    def test_option_usa_texto_puro_e_nao_o_html_do_selo(self, ctx):
        # o <option> nao renderiza tag; se usar o campo com selo, o usuario ve
        # a marcacao crua no dropdown
        html = app.render_template(
            "importar.html", titulo="Importar", topbar="", origem_opcoes=ORIGEM_OPCOES
        )
        assert 'class="selo"' not in html

    def test_sem_origem_nenhuma_nao_quebra(self, ctx):
        html = app.render_template("importar.html", titulo="Importar", topbar="", origem_opcoes=[])
        assert "<option" not in html
        assert "Ver prévia" in html


class TestInvestimentos:
    def test_estado_nao_sincronizado(self, ctx):
        html = app.render_template(
            "investimentos.html", titulo="Investimentos", topbar="", sincronizado=False
        )
        assert "Ainda não sincronizado" in html

    def test_nome_da_aplicacao_e_escapado(self, ctx):
        # o nome vem do Pluggy - conteudo de terceiro
        ativos = [{
            "nome": "<img src=x onerror=alert(1)>", "detalhe": "CDB", "aplicado": 100.0,
            "bruto": 110.0, "rend": 10.0, "pct": 10.0, "impostos": 1.5,
            "saldo": 108.5, "vencimento": "-",
        }]
        html = app.render_template(
            "investimentos.html", titulo="Investimentos", topbar="", sincronizado=True,
            ativos=ativos, encerrados=0, saldo_total=108.5, aplicado_total=100.0,
            rendimento_bruto=10.0, rend_pct=10.0, ir_total=1.5, historico=[],
        )
        assert "<img src=x" not in html
        assert "&lt;img" in html


class TestRegras:
    BASE = dict(
        titulo="Regras", topbar="", erro=None, categorias=[{"chave": "Fuel", "nome": "Combustível"}],
        dimensoes=[{"id": 1, "nome": "Responsável", "obrigatoria": True}],
        valores_por_dim={1: [{"id": 10, "dimensao_id": 1, "nome": "Ronaldo"}]},
        total_aplicadas=0, editar_id=None, regras=[],
    )

    def test_padrao_da_regra_e_escapado(self, ctx):
        regra = {
            "id": 1, "padrao": "<script>alert(1)</script>", "categoria": "Fuel",
            "categoria_nome": "Combustível", "dims_txt": "-", "dims_selecionadas": {},
        }
        html = app.render_template("regras.html", **{**self.BASE, "regras": [regra]})
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_modo_edicao_marca_so_a_regra_escolhida(self, ctx):
        regras = [
            {"id": 1, "padrao": "A", "categoria": "Fuel", "categoria_nome": "Combustível",
             "dims_txt": "-", "dims_selecionadas": {1: 10}},
            {"id": 2, "padrao": "B", "categoria": "Fuel", "categoria_nome": "Combustível",
             "dims_txt": "-", "dims_selecionadas": {}},
        ]
        html = app.render_template("regras.html", **{**self.BASE, "regras": regras, "editar_id": 1})
        assert html.count('value="editar_regra"') == 1
        assert '<option value="10" selected>' in html

    def test_sem_regras_mostra_aviso(self, ctx):
        html = app.render_template("regras.html", **self.BASE)
        assert "Nenhuma regra cadastrada ainda." in html
