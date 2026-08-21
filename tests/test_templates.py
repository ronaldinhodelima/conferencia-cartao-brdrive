"""Renderiza os templates com o formato REAL dos dados que a view entrega.

Existe por causa de um bug que passou despercebido: a tela /importar desempacotava
3 valores de uma tupla que carregar_origens() devolve com 4 desde o commit fcedcf1,
e ficou dando 500 sem ninguem ver (ela nao estava mais no menu). A tela foi removida
depois, mas a licao fica: testar com dado inventado nao pega esse tipo de erro - o
formato usado aqui tem que espelhar o que a view realmente entrega.
"""
import pytest

import app


@pytest.fixture
def ctx():
    with app.app.test_request_context("/"):
        yield


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
