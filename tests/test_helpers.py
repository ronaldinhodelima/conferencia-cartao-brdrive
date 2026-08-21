"""Testes das funcoes auxiliares puras usadas pelo DRE e pelas telas."""
import app  # noqa: F401
import core


class TestCatPt:
    def test_categoria_conhecida_traduz(self):
        assert core.cat_pt("Groceries") == "Mercado"

    def test_categoria_desconhecida_mantem_o_nome_original(self):
        assert core.cat_pt("Categoria Que Nao Existe") == "Categoria Que Nao Existe"

    def test_categoria_vazia_ou_none_vira_travessao(self):
        assert core.cat_pt(None) == "-"
        assert core.cat_pt("") == "-"

    def test_saida_e_sempre_escapada(self):
        # cat_pt() e chamado direto em HTML em varias telas - precisa
        # devolver texto seguro mesmo pra categoria com nome malicioso
        # (ver correcao de XSS desta sessao).
        assert "<script>" not in core.cat_pt("<script>alert(1)</script>")


class TestChaveAlfa:
    def test_ignora_acento_e_maiuscula_na_ordenacao(self):
        # "agua" e "água" tem que virar a MESMA chave (acento nao deve
        # separar as duas na ordenacao), e "Água" ordena antes de "Banco".
        assert core.chave_alfa("água") == core.chave_alfa("agua")
        assert core.chave_alfa("Água") < core.chave_alfa("Banco")
        assert core.chave_alfa("ZEBRA") == core.chave_alfa("zebra")

    def test_ordena_lista_ignorando_acento_e_caixa(self):
        palavras = ["Zebra", "água", "Ábaco", "banco"]
        ordenado = sorted(palavras, key=core.chave_alfa)
        assert ordenado == ["Ábaco", "água", "banco", "Zebra"]


class TestEsc:
    def test_escapa_tags_html(self):
        assert core.esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_escapa_aspas(self):
        resultado = core.esc('nome" onmouseover="alert(1)')
        assert '"' not in resultado

    def test_none_vira_string_vazia(self):
        assert core.esc(None) == ""

    def test_numero_vira_string(self):
        assert core.esc(42) == "42"


class TestJsonScript:
    def test_escapa_fechamento_de_script_tag(self):
        # descricao de lancamento contendo literalmente "</script>" nao pode
        # quebrar a tag e injetar HTML/JS (ver correcao de XSS desta sessao).
        payload = {"descricao": "</script><script>alert(1)</script>"}
        saida = core.json_script(payload)
        assert "</script>" not in saida
        assert "<\\/script>" in saida

    def test_json_valido_continua_parseavel(self):
        import json
        payload = {"a": 1, "b": "texto normal"}
        assert json.loads(core.json_script(payload)) == payload


class TestFmtMoeda:
    def test_formata_com_duas_casas_e_separador_de_milhar(self):
        assert core._fmt_moeda(1234.5) == "R$ 1,234.50"

    def test_valor_negativo(self):
        assert core._fmt_moeda(-50) == "R$ -50.00"


class TestBarraHtml:
    def test_sem_teto_nao_gera_barra(self):
        assert core._barra_html(100, None) == ""
        assert core._barra_html(100, 0) == ""

    def test_com_teto_gera_barra_e_percentual(self):
        html = core._barra_html(50, 100)
        assert "50% do teto" in html

    def test_estourar_o_teto_nao_passa_de_100_por_cento_de_largura(self):
        # a barra visual nao pode passar do tamanho do container mesmo que
        # o gasto seja 3x o teto - so o texto mostra o percentual real.
        html = core._barra_html(300, 100)
        assert "width:100%" in html
        assert "300% do teto" in html


class TestSenha:
    def test_hash_e_verificacao_roundtrip(self):
        h = core.hash_senha("minhasenha123")
        assert core.senha_confere("minhasenha123", h)

    def test_senha_errada_nao_confere(self):
        h = core.hash_senha("minhasenha123")
        assert not core.senha_confere("outrasenha", h)

    def test_hash_guardado_invalido_nao_quebra(self):
        assert not core.senha_confere("qualquer", "lixo-sem-formato")
        assert not core.senha_confere("qualquer", None)


class TestPermissoesDoPerfil:
    def test_admin_tem_todas_as_permissoes(self):
        assert set(core.permissoes_do_perfil("admin")) == set(core.PERMISSOES.keys())

    def test_leitura_so_ve_lancamentos_e_relatorios(self):
        perms = set(core.permissoes_do_perfil("leitura"))
        assert perms == {"lancamentos_ver", "relatorios"}

    def test_perfil_desconhecido_cai_em_leitura(self):
        assert set(core.permissoes_do_perfil("perfil-que-nao-existe")) == {"lancamentos_ver", "relatorios"}


class TestAvisoPendencias:
    """aviso_pendencias_html e a faixa de alerta mostrada no DRE - so pode aparecer
    quando ha algo que realmente distorce numero, senao vira ruido na tela."""

    def test_sem_pendencia_nao_mostra_nada(self):
        pend = {"sem_natureza": [], "despesa_sem_centro": [], "natureza_manual": 0, "total": 0}
        assert core.aviso_pendencias_html(pend) == ""

    def test_natureza_manual_sozinha_nao_dispara_alerta(self):
        # natureza manual e informativo (funciona, so nao e o caminho mais limpo) -
        # nao pode disparar alerta vermelho como se fosse erro
        pend = {"sem_natureza": [], "despesa_sem_centro": [], "natureza_manual": 12, "total": 0}
        assert core.aviso_pendencias_html(pend) == ""

    def test_categoria_sem_natureza_dispara_alerta(self):
        pend = {"sem_natureza": ["Groceries"], "despesa_sem_centro": [], "natureza_manual": 0, "total": 1}
        html = core.aviso_pendencias_html(pend)
        assert "sem natureza definida" in html
        assert "/pendencias" in html

    def test_singular_e_plural(self):
        um = core.aviso_pendencias_html(
            {"sem_natureza": ["A"], "despesa_sem_centro": [], "natureza_manual": 0, "total": 1}
        )
        assert "1</strong> categoria sem natureza" in um
        dois = core.aviso_pendencias_html(
            {"sem_natureza": ["A", "B"], "despesa_sem_centro": [], "natureza_manual": 0, "total": 2}
        )
        assert "2</strong> categorias sem natureza" in dois

    def test_mostra_os_dois_tipos_juntos(self):
        pend = {"sem_natureza": ["A"], "despesa_sem_centro": ["B", "C"], "natureza_manual": 0, "total": 3}
        html = core.aviso_pendencias_html(pend)
        assert "sem natureza definida" in html
        assert "sem centro de custo" in html
