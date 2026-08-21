"""Testes da 'regra de ouro' do DRE (ver CLAUDE.md):

    Resultado = Receitas - Despesas. Investimento, compra de bem, pagamento de
    fatura e transferencia entre contas proprias NAO sao despesa - so trocam a
    forma do patrimonio. Juros e tarifas SAO despesa de verdade.

A decisao de qual e a natureza efetiva de um lancamento vive em duas formas
paralelas dentro do app: a expressao SQL `NATUREZA_SQL` (usada nas queries) e
os dados de configuracao (`NATUREZAS`, `SEED_NATUREZAS`, `NATUREZAS_NEUTRAS`).
Nao da pra rodar SQL sem banco, entao aqui testamos:

  1. Os dados de configuracao batem com a regra de ouro (pega erro de digitar
     a natureza errada em SEED_NATUREZAS, por exemplo).
  2. Uma reproducao em Python puro do CASE do NATUREZA_SQL (`natureza_efetiva`
     abaixo), que espelha exatamente a logica SQL - se a SQL mudar, essa
     funcao (e os testes) tem que mudar junto.
"""
import app


def natureza_efetiva(natureza_transacao, natureza_categoria, valor_despesa):
    """Espelho em Python de app.NATUREZA_SQL. Mantenha em sincronia com a SQL.

    natureza_transacao: app.natureza do lancamento (pode ser None)
    natureza_categoria: app.categoria_natureza.natureza da categoria (pode ser None)
    valor_despesa: o valor no sentido de VAL_DESPESA (positivo = dinheiro saiu)
    """
    base = natureza_transacao or natureza_categoria or app.NATUREZA_PADRAO
    if base == "fluxo":
        return "despesa" if valor_despesa > 0 else "receita"
    return base


class TestRegraDeOuro:
    """Cada caso aqui e uma frase literal da regra de ouro do CLAUDE.md."""

    def test_investimento_nao_e_despesa(self):
        assert "investimento" in app.NATUREZAS_NEUTRAS
        assert app.SEED_NATUREZAS["Investments"] == "investimento"
        assert app.SEED_NATUREZAS["Automatic investment"] == "investimento"
        assert app.SEED_NATUREZAS["Pension"] == "investimento"

    def test_compra_de_bem_nao_e_despesa(self):
        assert "bem" in app.NATUREZAS_NEUTRAS
        assert app.SEED_NATUREZAS["Imóveis / Terrenos"] == "bem"
        assert app.SEED_NATUREZAS["Veículos / Bens"] == "bem"

    def test_pagamento_de_fatura_nao_e_despesa(self):
        # a despesa real ja foi contada na compra - pagar a fatura so move
        # dinheiro da conta para o cartao.
        assert app.SEED_NATUREZAS["Credit card payment"] == "transferencia"
        assert "transferencia" in app.NATUREZAS_NEUTRAS

    def test_transferencia_entre_contas_proprias_nao_e_despesa(self):
        for categoria in (
            "Transfer - Internal",
            "Same person transfer",
            "Same person transfer - PIX",
            "Same person transfer - TED",
        ):
            assert app.SEED_NATUREZAS[categoria] == "transferencia"

    def test_juros_e_tarifas_sao_despesa_de_verdade(self):
        # dinheiro sai e nao volta - isso e despesa real, diferente de
        # investimento/bem/transferencia.
        assert app.SEED_NATUREZAS["Interests charged"] == "despesa"
        assert app.SEED_NATUREZAS["Credit card fees"] == "despesa"
        assert app.SEED_NATUREZAS["Tax on financial operations"] == "despesa"

    def test_receitas_sao_receita(self):
        for categoria in ("Income", "Salary", "Government aid", "Interest income", "Dividends"):
            assert app.SEED_NATUREZAS[categoria] == "receita"

    def test_pix_ted_doc_dependem_da_direcao(self):
        # PIX/TED/dinheiro: o que entra e receita, o que sai e despesa -
        # nao da pra saber sem olhar a direcao de cada lancamento.
        for categoria in ("Transfer - PIX", "Transfer - TED", "Transfer - DOC", "Transfer - Cash"):
            assert app.SEED_NATUREZAS[categoria] == "fluxo"

    def test_natureza_padrao_e_despesa(self):
        # categoria sem natureza definida (categoria nova, ainda nao
        # classificada) tem que cair em despesa por seguranca - nunca some
        # do resultado por omissao.
        assert app.NATUREZA_PADRAO == "despesa"

    def test_todo_valor_de_seed_naturezas_e_uma_natureza_valida(self):
        # se alguem digitar errado uma natureza em SEED_NATUREZAS (typo),
        # isso pega antes de virar um bug silencioso em producao.
        for categoria, natureza in app.SEED_NATUREZAS.items():
            assert natureza in app.NATUREZAS, f"{categoria!r} aponta pra natureza invalida {natureza!r}"

    def test_categorias_neutras_padrao_deriva_certo_do_seed(self):
        esperado = {c for c, n in app.SEED_NATUREZAS.items() if n in app.NATUREZAS_NEUTRAS}
        assert app.CATEGORIAS_NEUTRAS_PADRAO == esperado

    def test_naturezas_neutras_sao_subconjunto_de_naturezas(self):
        assert set(app.NATUREZAS_NEUTRAS) <= set(app.NATUREZAS)
        # despesa e receita nunca podem ser "neutras" (fora do resultado) -
        # isso quebraria o proprio conceito de resultado.
        assert "despesa" not in app.NATUREZAS_NEUTRAS
        assert "receita" not in app.NATUREZAS_NEUTRAS


class TestNaturezaEfetiva:
    """Cobre a prioridade natureza-do-lancamento > natureza-da-categoria > padrao,
    e a resolucao de 'fluxo' pela direcao do dinheiro (espelho do NATUREZA_SQL)."""

    def test_natureza_do_lancamento_tem_prioridade_sobre_categoria(self):
        # PIX de R$98mil classificado como "Groceries" pelo Pluggy, mas o
        # usuario marcou manualmente como compra de bem no proprio lancamento.
        assert natureza_efetiva("bem", "despesa", 98000) == "bem"

    def test_sem_natureza_no_lancamento_usa_a_da_categoria(self):
        assert natureza_efetiva(None, "investimento", 500) == "investimento"

    def test_sem_nenhuma_natureza_definida_usa_o_padrao(self):
        assert natureza_efetiva(None, None, 100) == app.NATUREZA_PADRAO

    def test_fluxo_com_dinheiro_saindo_e_despesa(self):
        assert natureza_efetiva(None, "fluxo", 150.0) == "despesa"

    def test_fluxo_com_dinheiro_entrando_e_receita(self):
        assert natureza_efetiva(None, "fluxo", -150.0) == "receita"

    def test_fluxo_definido_no_proprio_lancamento_tambem_resolve_por_direcao(self):
        assert natureza_efetiva("fluxo", "despesa", -200.0) == "receita"


class TestValDespesaSinal:
    """VAL_DESPESA: positivo = dinheiro saiu. Cartao de credito vem positivo na
    compra; conta corrente vem negativo na saida - por isso o sinal se inverte
    conforme o tipo da conta. Aqui so confirmamos que a expressao existe e tem
    a forma esperada (o calculo em si so roda dentro do Postgres)."""

    def test_val_despesa_inverte_sinal_fora_do_credito(self):
        assert "CREDIT" in app.VAL_DESPESA
        assert "ELSE -COALESCE" in app.VAL_DESPESA
