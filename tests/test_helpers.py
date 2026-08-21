"""Testes das funcoes auxiliares puras usadas pelo DRE e pelas telas."""
import app


class TestCatPt:
    def test_categoria_conhecida_traduz(self):
        assert app.cat_pt("Groceries") == "Mercado"

    def test_categoria_desconhecida_mantem_o_nome_original(self):
        assert app.cat_pt("Categoria Que Nao Existe") == "Categoria Que Nao Existe"

    def test_categoria_vazia_ou_none_vira_travessao(self):
        assert app.cat_pt(None) == "-"
        assert app.cat_pt("") == "-"

    def test_saida_e_sempre_escapada(self):
        # cat_pt() e chamado direto em HTML em varias telas - precisa
        # devolver texto seguro mesmo pra categoria com nome malicioso
        # (ver correcao de XSS desta sessao).
        assert "<script>" not in app.cat_pt("<script>alert(1)</script>")


class TestChaveAlfa:
    def test_ignora_acento_e_maiuscula_na_ordenacao(self):
        # "agua" e "água" tem que virar a MESMA chave (acento nao deve
        # separar as duas na ordenacao), e "Água" ordena antes de "Banco".
        assert app.chave_alfa("água") == app.chave_alfa("agua")
        assert app.chave_alfa("Água") < app.chave_alfa("Banco")
        assert app.chave_alfa("ZEBRA") == app.chave_alfa("zebra")

    def test_ordena_lista_ignorando_acento_e_caixa(self):
        palavras = ["Zebra", "água", "Ábaco", "banco"]
        ordenado = sorted(palavras, key=app.chave_alfa)
        assert ordenado == ["Ábaco", "água", "banco", "Zebra"]


class TestEsc:
    def test_escapa_tags_html(self):
        assert app.esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_escapa_aspas(self):
        resultado = app.esc('nome" onmouseover="alert(1)')
        assert '"' not in resultado

    def test_none_vira_string_vazia(self):
        assert app.esc(None) == ""

    def test_numero_vira_string(self):
        assert app.esc(42) == "42"


class TestJsonScript:
    def test_escapa_fechamento_de_script_tag(self):
        # descricao de lancamento contendo literalmente "</script>" nao pode
        # quebrar a tag e injetar HTML/JS (ver correcao de XSS desta sessao).
        payload = {"descricao": "</script><script>alert(1)</script>"}
        saida = app.json_script(payload)
        assert "</script>" not in saida
        assert "<\\/script>" in saida

    def test_json_valido_continua_parseavel(self):
        import json
        payload = {"a": 1, "b": "texto normal"}
        assert json.loads(app.json_script(payload)) == payload


class TestFmtMoeda:
    def test_formata_com_duas_casas_e_separador_de_milhar(self):
        assert app._fmt_moeda(1234.5) == "R$ 1,234.50"

    def test_valor_negativo(self):
        assert app._fmt_moeda(-50) == "R$ -50.00"


class TestBarraHtml:
    def test_sem_teto_nao_gera_barra(self):
        assert app._barra_html(100, None) == ""
        assert app._barra_html(100, 0) == ""

    def test_com_teto_gera_barra_e_percentual(self):
        html = app._barra_html(50, 100)
        assert "50% do teto" in html

    def test_estourar_o_teto_nao_passa_de_100_por_cento_de_largura(self):
        # a barra visual nao pode passar do tamanho do container mesmo que
        # o gasto seja 3x o teto - so o texto mostra o percentual real.
        html = app._barra_html(300, 100)
        assert "width:100%" in html
        assert "300% do teto" in html


class TestSenha:
    def test_hash_e_verificacao_roundtrip(self):
        h = app.hash_senha("minhasenha123")
        assert app.senha_confere("minhasenha123", h)

    def test_senha_errada_nao_confere(self):
        h = app.hash_senha("minhasenha123")
        assert not app.senha_confere("outrasenha", h)

    def test_hash_guardado_invalido_nao_quebra(self):
        assert not app.senha_confere("qualquer", "lixo-sem-formato")
        assert not app.senha_confere("qualquer", None)


class TestPermissoesDoPerfil:
    def test_admin_tem_todas_as_permissoes(self):
        assert set(app.permissoes_do_perfil("admin")) == set(app.PERMISSOES.keys())

    def test_leitura_so_ve_lancamentos_e_relatorios(self):
        perms = set(app.permissoes_do_perfil("leitura"))
        assert perms == {"lancamentos_ver", "relatorios"}

    def test_perfil_desconhecido_cai_em_leitura(self):
        assert set(app.permissoes_do_perfil("perfil-que-nao-existe")) == {"lancamentos_ver", "relatorios"}
