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


class TestIndex:
    """A tela de Lançamentos monta linha por linha a partir de dado do banco e do
    Pluggy, e ainda entrega dois blocos JSON para o lancamentos.js ler."""

    DIMS = [{"id": 1, "nome": "Responsável", "obrigatoria": True}]
    VALS = {1: [{"id": 10, "dimensao_id": 1, "nome": "Ronaldo"}]}

    def linha(self, **kw):
        base = dict(
            id="tx1", classes="", data_dia="13/08/26", data_hora="00:00",
            data_full="13/08/2026 00:00", data_sort=1.0, descricao="COMPRA",
            origem_selo='<span class="selo">UN</span>', origem_texto="Unicred",
            origem_completa="Unicred · CC", categoria="Fuel", dims={1: 10},
            valor_fmt="- R$ 10.00", valor_sort=-10.0, cor_valor="", observacao="",
            conferida=False, duplicada=False,
        )
        base.update(kw)
        return base

    def render(self, ctx_linhas, **kw):
        base = dict(
            titulo="Lançamentos", topbar="", mes="2026-08", status="todas",
            hoje_iso="2026-08-21", origem_filtro_html="", pode_editar=True,
            pode_conferir=True, pode_manual=True,
            categorias=[{"chave": "Fuel", "nome": "Combustível"}],
            dimensoes=self.DIMS, valores_por_dim=self.VALS, naturezas=app.NATUREZAS,
            linhas=ctx_linhas, por_categoria=[], receita_mes=0.0, gasto_real=0.0,
            resultado_mes=0.0, conf=0, total=0,
            detalhes_json="{}", config_json="{}",
        )
        base.update(kw)
        return app.render_template("index.html", **base)

    def test_descricao_e_escapada(self, ctx):
        # descricao vem do banco (Pluggy ou lancamento manual digitado)
        html = self.render([self.linha(descricao="<img src=x onerror=alert(1)>")])
        assert "<img src=x" not in html
        assert "&lt;img" in html

    def test_apelido_do_cartao_e_escapado_mas_o_selo_nao(self, ctx):
        # o apelido e digitado pelo usuario em /contas; o selo e HTML do proprio app
        html = self.render([self.linha(origem_texto='"><script>alert(1)</script>')])
        assert "<script>alert(1)</script>" not in html
        assert '<span class="selo">UN</span>' in html

    def test_dimensao_obrigatoria_sem_valor_fica_destacada(self, ctx):
        html = self.render([self.linha(dims={1: None})])
        assert "#c23c34;background:#fbeceb" in html

    def test_dimensao_obrigatoria_preenchida_nao_destaca(self, ctx):
        html = self.render([self.linha(dims={1: 10})])
        assert "#c23c34;background:#fbeceb" not in html

    def test_sem_permissao_de_editar_trava_os_campos(self, ctx):
        html = self.render([self.linha()], pode_editar=False, pode_conferir=False)
        assert html.count("disabled") >= 3

    def test_sem_lancamentos_mostra_aviso_com_colspan_certo(self, ctx):
        html = self.render([])
        # 8 colunas fixas + 1 dimensao
        assert 'colspan="9"' in html
        assert "Nenhum lançamento neste filtro." in html

    def test_natureza_fluxo_nao_aparece_no_modal(self, ctx):
        # 'fluxo' e o padrao (direcao decide), nao faz sentido escolher na mao
        html = self.render([self.linha()])
        assert 'value="fluxo"' not in html
