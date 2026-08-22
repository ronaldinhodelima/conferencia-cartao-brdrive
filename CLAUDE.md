# Pé de Meia — contexto do projeto

Sistema financeiro pessoal/familiar da família Ronaldo. Sincroniza lançamentos de cartão de
crédito e conta corrente via Open Finance (Pluggy) do Unicred e Nubank (duas contas Nubank:
Ronaldo e Andrea). Substitui o antigo nome "Conferência de Cartão".

Este arquivo existe para que qualquer sessão do Claude (Code, Cowork, etc.) retome o projeto
sem precisar redescobrir decisões já tomadas. Leia isto inteiro antes de mexer em qualquer coisa.

## Regra de ouro (instrução permanente do usuário)

> "Sempre que formos falar em financeiro, preciso que traga as considerações do DRE, do conceito
> de DRE financeiro, não podemos ter dados mascarados ou informações que inflem os lançamentos.
> Os números precisam ser reais."

Na prática:
- Resultado = Receitas − Despesas. Só isso é "resultado".
- Investimento, compra de bem (terreno, veículo, imóvel), pagamento de fatura de cartão e
  transferência entre contas próprias **não são despesa** — só trocam a forma do patrimônio.
- Juros e tarifas **são despesa de verdade** (o dinheiro sai e não volta).
- Terreno não deprecia. Só entraria depreciação de bens que perdem valor com o tempo (não
  implementamos depreciação ainda — hoje bens só ficam fora do resultado, não geram despesa).
- Toda vez que mexer em relatório/DRE/natureza de categoria, explicar o raciocínio contábil,
  nunca só aplicar sem justificar.

## Preferências de estilo do usuário (Ronaldo)

- Respostas diretas, sem enrolação, com tópicos quando fizer sentido.
- **Nunca inventar números ou informação** — se não souber, perguntar.
- Interface do sistema em português.

## Stack e arquitetura

- **App principal**, separado em quatro camadas:
  - `app.py` (~45 linhas) — cria o Flask app, registra os filtros Jinja e os blueprints.
  - `core.py` — constantes, acesso ao banco, permissões, helpers de HTML. **Não importa
    nada de `views/` nem de `app.py`.**
  - `views/` — as 22 rotas em 6 blueprints: `auth`, `sistema`, `lancamentos`, `relatorios`,
    `cadastros`, `usuarios`. Cada módulo importa do `core` só os nomes que usa, listados
    explicitamente.
  - `templates/` (Jinja2) e `static/` (CSS, JS, logos). **Nenhuma tela monta HTML por
    f-string no Python** — se aparecer uma, é regressão.
  Sem framework front-end — JS puro. A view faz SQL e regra de negócio, o template só exibe.
  **O escaping do Jinja é automático**: nada de `esc()` dentro de valor que vai para template
  (gera `&quot;` visível na tela). Quando o valor já é HTML confiável montado no Python
  (selo do banco, topbar, aviso de pendências), marque `|safe` explicitamente — assim fica
  claro na revisão que foi decisão, não esquecimento. Para valor que entra em **string
  JavaScript** (dentro de `onclick`/`onsubmit`), `|safe` e escape de HTML não bastam: use
  `|tojson`.
- **Banco**: PostgreSQL, schema `cartao.*` (rodando dentro do Coolify, host interno de rede
  Docker — não acessível de fora).
- **Worker de sincronização**: `bussola/app.py` (serviço separado no Coolify) — busca dados do
  Pluggy e grava no mesmo Postgres.
- **Deploy**: Coolify (PaaS auto-hospedado da BRDrive), build via Dockerfile a partir de um
  repositório git no GitHub.

## Repositório e deploy

- **GitHub**: `ronaldinhodelima/pe-de-meia` (branch `main`).
  - `app.py`, `core.py`, `views/`, `templates/`, `static/` → app principal (Flask).
  - `tests/` → suíte pytest.
  - `bussola/app.py` → worker de sincronização Pluggy.
  - `Dockerfile` → build do app principal. **Ao criar pasta nova, adicione o `COPY`** —
    o container sobe sem ela e quebra só na hora de servir a tela.
- **Coolify**: `https://coolify.brdrive.net`, projeto **Ronaldinho**.
  - App principal: nome `conferencia-cartao-app`, uuid `nvbnzjhig1og7s0gn5nrbxjo`.
    Domínio: **https://pedemeia.brdrive.net** (+ domínio padrão do Coolify como backup:
    `https://nvbnzjhig1og7s0gn5nrbxjo.coolify.brdrive.net`).
  - Worker de sync: nome `bussola-financeira-app-v2`, uuid `hdgffcvh3ljqe61dczztaycz`.
    Domínio interno: `https://hdgffcvh3ljqe61dczztaycz.coolify.brdrive.net`.
    **Atenção**: esse domínio já mudou uma vez sem avisar (Coolify reatribuiu o subdomínio
    em algum redeploy) e quebrou o botão "Atualizar agora" porque a URL estava hardcoded em
    `BUSSOLA_SYNC_URL` no app principal. Se o sync voltar a dar erro 404/502, o primeiro
    passo é conferir se o domínio do worker mudou de novo.
- **Deploy = git push + trigger via API do Coolify** (a UI de clicar "Deploy" no painel
  se mostrou pouco confiável nesta sessão — os cliques às vezes não disparavam o build).
  Fluxo usado:
  ```bash
  git clone https://github.com/ronaldinhodelima/pe-de-meia.git
  cp app.py <repo>/app.py   # depois de editar
  cd <repo> && git add app.py && git commit -m "..." && git push
  curl -X POST -H "Authorization: Bearer <COOLIFY_TOKEN>" \
    "https://coolify.brdrive.net/api/v1/deploy?uuid=nvbnzjhig1og7s0gn5nrbxjo"
  ```
  O token do Coolify e as credenciais de banco ficam nas variáveis de ambiente do próprio
  Coolify (aba Environment de cada app) — nunca no código nem no repositório git.

## Banco de dados — tabelas principais (schema `cartao`)

- `pluggy_item` — cada conexão bancária (1 linha por item Pluggy).
- `conta` — contas dentro de cada item (conta corrente, cartão de crédito, "manual"/dinheiro).
- `transacao` — lançamentos (chave: `transacao_id` do Pluggy, evita duplicidade).
- `sync_log` — auditoria das rodadas de sincronização.
- `categoria_natureza` — natureza contábil de cada categoria (despesa/receita/investimento/
  bem/transferência/fluxo) — base do DRE.
- `categoria` — overrides de nome (renomeações feitas pelo usuário em `/categorias`).
- `categoria_oculta` — categorias removidas pelo usuário (ficam escondidas nos dropdowns).
- `grupo_custo` / `subgrupo_custo` / `categoria_subgrupo` — centro de custo (grupos de gasto).
- `usuario` — login/senha (PBKDF2-HMAC-SHA256, 200k iterações) + perfil + permissões.
- `item_titular` — de quem é cada conexão bancária (Ronaldo / Andrea / Ronaldo e Andrea).
- `investimento` / `investimento_saldo` — posições de investimento e histórico diário de saldo.
- `cartao_nome` — apelido de cada cartão pelos 4 últimos dígitos. Um cartão de crédito (uma
  linha em `conta`) pode ter vários cartões físicos/virtuais/adicionais; a `conta` guarda só o
  final do principal, e os adicionais só aparecem em `transacao.numero_cartao_final` — é dali
  que a tela `/contas` descobre quais cartões existem.
- `regra_classificacao` / `regra_dimensao_valor` — regras automáticas de categorização.
- `dimensao` / `dimensao_valor` / `transacao_dimensao` — dimensões livres (ex: Responsável:
  Ronaldo/Andrea/Amanda/Compartilhado, Projeto, etc.) além da categoria.
  `dimensao_valor` também guarda `teto_mensal` / `teto_anual` (teto de gasto por valor).
- `schema_version` — controle de migração (ver `migrate()`). Cada bloco `if versao_atual < N`
  roda uma vez só; **não reescrever migração já aplicada** — criaria divergência de schema.

## Modelo de natureza (6 naturezas, base do DRE)

`despesa`, `receita`, `investimento`, `bem`, `transferencia` e `fluxo` — este último é o
padrão: a direção do lançamento decide se é receita ou despesa (usado pra PIX/TED/dinheiro).
As três neutras (`investimento`, `bem`, `transferencia`) ficam fora do resultado.

## Sistema de permissões

Perfis: `admin` (tudo), `operador` (lançamentos + relatórios + sincronizar, sem
cadastros/usuários), `leitura` (só ver lançamentos e relatórios). As 8 permissões granulares:
`lancamentos_ver`, `lancamentos_editar`, `lancamentos_conferir`, `lancamentos_manual`,
`relatorios`, `cadastros`, `sincronizar`, `usuarios`. Decorator `@requer(permissao)`
protege cada rota; `pode(permissao)` controla o que aparece na interface.

Usuários atuais: `ronaldo` (admin), `andrea` (admin, herdado do sistema antigo), `amanda`
(operador, criada nesta sessão).

## Identidade visual

- Nome: **Pé de Meia**. Logo oficial fornecida pelo usuário (meia de tricô com dinheiro),
  em **fundo claro sólido** no topbar e na tela de login.
  Os PNGs foram embutidos como base64 direto no `app.py` (`LOGO_TOPBAR_B64`, `LOGO_HERO_B64`)
  — não dependem de arquivo externo.
- **Favicon**: meia sólida em marrom (`#9f7251`) com **fundo transparente**, diferente da logo
  do topbar. Cuidado: o favicon que vale está **hardcoded dentro de `BASE_CSS_HEAD`**
  (`<link rel="icon" ...>`); a variável `LOGO_FAVICON_B64` existe mas **não é usada por
  ninguém** — mexer nela não muda nada na tela.
- Bancos identificados por "selo" colorido em CSS puro (cor da marca + sigla de 2 letras),
  não por logo de imagem — Pluggy não fornece logo utilizável.
- Tooltips customizados (120ms, mais rápido que o `title` nativo do navegador).

## Funcionalidades já construídas (sessão 1 — Cowork)

1. Sync completo do Pluggy (corrigido bug de paginação — campo `next`, não `cursor.after`).
2. Relatórios em ordem cronológica (gráfico) com lista "Totais agrupados" mais recente primeiro.
3. Modelo de natureza contábil (5 naturezas) aplicado em Relatórios e DRE.
4. DRE com Receitas/Despesas/Resultado/Margem por mês + grupos de custo.
5. Tela `/naturezas` para reclassificar a natureza de qualquer categoria.
6. Grupo "Despesas Financeiras" (juros e tarifas) no centro de custo.
7. Conexão Nubank (duas contas — Ronaldo e Andrea) além da Unicred, sync multi-conexão com
   auto-descoberta de conexões já sincronizadas (mas **não** de conexões novas nunca vistas —
   essas precisam ser adicionadas manualmente na env var `PLUGGY_ITEM_ID` do worker).
8. Selos coloridos de banco + tooltips rápidos.
9. Renomeação do app (Conferência de Cartão → Meu Dinheiro → **Pé de Meia**), com DNS próprio
   `pedemeia.brdrive.net`.
10. Remoção do serviço Metabase (não era mais usado).
11. Sistema completo de usuários e permissões (`/usuarios`).
12. Logo oficial aplicada (favicon, topbar, tela de login).
13. Grupos/categorias sempre em ordem alfabética (ignorando acento/maiúscula — função
    `chave_alfa()`); `<details>` de `/grupos` lembram se estavam abertos ao salvar
    (via `localStorage`).
14. Item "Importar extrato/fatura" removido do menu (funcionalidade continua existindo,
    só não aparece mais na navegação).
15. `/categorias`: criar, renomear, mover lançamentos entre categorias, excluir (só permite
    excluir categoria vazia — com lançamentos, fica "protegida").
16. `/contas`: identifica o titular de cada conexão bancária (Unicred = Ronaldo e Andrea,
    Nubank 1 = Ronaldo, Nubank 2 = Andrea) — aparece em Lançamentos, Relatórios e em qualquer
    lugar que mostre a origem do dinheiro.
17. Correção do bug do botão "Atualizar agora" (URL do worker de sync desatualizada).

## Funcionalidades e correções (sessão 2 — Claude Code, 20–21/08/2026)

Segurança:
1. Cookie de sessão com `Secure`, `HttpOnly` e `SameSite=Lax`.
2. Senhas de fallback hardcoded (`changeme1/2`) removidas — sem env, a conta de emergência
   simplesmente não existe.
3. **XSS corrigido em todo o app** (`esc()` e `json_script()`): nome de categoria, dimensão,
   grupo, cartão, titular, descrição/observação de lançamento e mensagens de aviso. O caso mais
   grave era `json.dumps()` dentro de `<script>` — não escapa `</`, então uma descrição contendo
   `</script>` executava JS para qualquer um que abrisse Lançamentos. Validado com payload real.
4. `/sync` do worker exige `X-Sync-Secret` (env `SYNC_SECRET`, mesma nos dois serviços).
5. `/` do worker parou de expor a lista de tabelas do banco e o resumo do sync sem a chave.

Processo:
6. Migração versionada (`cartao.schema_version`) — antes rodava ~30 DDL a cada boot.
7. `tests/` com 46 testes (ver seção "Testes automatizados").
8. `.gitignore` e identidade do git configurada.

Produto:
9. Login: mostrar senha + `autocomplete` para o navegador salvar credencial.
10. Favicon novo (meia sólida, fundo transparente). **Atenção**: o favicon real fica hardcoded
    dentro de `BASE_CSS_HEAD`, não na variável `LOGO_FAVICON_B64` (que não é usada).
11. `/categorias`: clicar em "X lanç. — protegida" lista os lançamentos que bloqueiam a remoção.
12. `/grupos` virou **Centro de Custos**: tabela única, hierarquia visual grupo → subgrupo,
    vínculo de categoria por chips removíveis.
13. Teto de gasto saiu do centro de custo e virou **teto por valor de dimensão** (ex: "Ronaldo:
    R$3.000/mês"), com barra de progresso do gasto real do mês/ano em `/dimensoes`.
14. **`/pendencias`** — painel de pendências de classificação (ver seção própria).
15. **Colunas ajustáveis em todas as 7 tabelas**: redimensionar (estilo planilha, a vizinha
    compensa), reordenar arrastando o cabeçalho, ordenar clicando no título, botão "Redefinir
    colunas". Preferências no `localStorage` por tabela. Ver seção própria.
16. Fatura por cartão: `fechamento_fatura`/`vencimento_fatura` vêm do Pluggy por conta — antes
    o fechamento era uma constante única (`FATURA_DIA_FECHAMENTO`), que só servia para um cartão.
17. `/cartoes` foi **fundido** em `/contas`, agora **"Configurações de Contas / Cartão"**:
    titular da conexão + nome de cada cartão (físico, virtual, adicional) + datas da fatura
    (somente leitura).
18. Navegação de mês (`‹ ›`) em Lançamentos e logo do topbar como link para o início.

## Refatoração e limpeza (sessão 3 — 21/08/2026)

`app.py` saiu de **5.612 para ~3.260 linhas**. Todo o HTML foi para `templates/` (Jinja) e o
JS/CSS/logos para `static/`. As 11 telas foram migradas uma a uma, cada uma validada em
produção antes da seguinte.

**Cinco XSS latentes** apareceram na migração, todos em conteúdo de terceiro que era
interpolado cru — o escaping automático do Jinja resolveu quatro; o do preview de importação
era JS e foi corrigido com `escHtml`:
descrição de arquivo importado, nome de aplicação (Pluggy), padrão de regra, nome de dimensão
e valor de dimensão, e o apelido do cartão em `origem_curta()`.

**Removido por não estar mais em uso:** a tela `/importar` inteira (3 rotas, 7 auxiliares, a
permissão `importar`) — que aliás estava dando 500 desde `fcedcf1`, sem ninguém notar, porque
tinha saído do menu. E os redirects legados `/cartoes` e `/naturezas`.

### Regra de dependência entre os módulos

`app.py` → `views/` → `core.py`, sempre nessa direção. Nada em `core.py` pode importar de
`views/`, senão volta o import circular.

**Armadilha do estado compartilhado:** `CATEGORIA_PT_DB` e `CATEGORIAS_OCULTAS` são
recarregados em runtime (quando o usuário renomeia ou oculta categoria). Como os blueprints
fazem `from core import CATEGORIA_PT_DB`, `recarregar_categorias_db()` **altera os dicionários
no lugar** (`clear()` + `update()`). Se algum dia alguém trocar isso por reatribuição, os
nomes de categoria congelam na versão do boot — sem erro nenhum, só nome errado na tela.
`tests/test_core_estado.py` trava esse comportamento checando a identidade do objeto.

### Como cortar código sem quebrar nada

Duas vezes nesta sessão um corte por busca de texto apagou código vizinho por engano — uma
delas levou a rota `/dre` inteira, outra levou `_montar_filtro_relatorio` e derrubou
`/relatorios` em produção. O que funciona:

1. Delimitar a função pelo **AST** (`node.end_lineno`), nunca por "até o próximo `@app.route`"
   — auxiliares sem decorator moram entre as rotas e são engolidos.
2. Depois do corte, **comparar as definições de topo antes/depois**. Contar rotas não basta,
   justamente porque o que se perde costuma ser função sem decorator.
3. **302 não é prova de que a tela funciona** — é o redirect de login. Validação real só
   logado, olhando o conteúdo.
4. Em tela com número (DRE, relatórios), **anotar os valores em produção antes do deploy** e
   comparar depois. Migração de tela não pode mexer em número.

## Pendências conhecidas

### Ação do usuário (nada disso o Claude pode fazer sozinho)

- **Rotar o token do Coolify.** O token foi colado no chat durante a sessão de 21/08/2026 e
  deve ser considerado comprometido. Gerar novo em Coolify → Keys & Tokens e revogar o antigo.
- **Revisar `/pendencias`.** Há categorias sem natureza definida (Aeroporto, Água/Gás,
  Automotive, Bicycle, Cinema…). Sem natureza, o app assume `despesa` por padrão — ou seja,
  elas **entram no resultado do DRE como gasto** sem ninguém ter decidido isso. É o tipo de
  coisa que infla despesa, contra a regra de ouro do projeto.

### Técnicas

**Escape em JS montado no cliente:** três telas montam HTML no navegador com `innerHTML`
a partir de dados que chegam por AJAX (`lancamentos.js`, `relatorios.js`, `categorias.js`).
Aí o Jinja não protege — quem escapa é o `escHtml()` do `tabelas.js`, no ponto onde o
`innerHTML` é montado. A regra combinada: **o servidor manda texto puro** (`cat_pt_puro`,
sem `esc()`) **e o JS escapa**. Não escapar no servidor também: gera escape duplo, e em
rótulo de gráfico (Chart.js desenha em canvas) apareceria `&amp;` literal na tela.

**Gunicorn com `--preload` (não remova o preload):** o app roda em
`gunicorn --preload -w 2 --timeout 120`. O `--preload` não é detalhe de performance —
`core.py` chama `migrate()` no import, então sem ele **cada worker roda a migração ao mesmo
tempo no boot** e as DDL competem entre si (medido: 3 workers = 3 imports; com preload = 1).
É seguro porque nenhuma conexão de banco fica aberta em variável de módulo. Se algum dia
alguém criar um pool global, o preload passa a compartilhar socket entre os processos filhos
e vira bug. O `--timeout 120` existe porque "Atualizar agora" chama o worker de sync com
timeout de 60s — o padrão do gunicorn (30s) mataria o processo antes.

**Sem ambiente de staging:** todo push na `main` vai direto para o app que a família usa.
Mitigado hoje pelos testes e pela validação pós-deploy, mas o risco existe.

**Operacional:** toda vez que uma nova conexão bancária for adicionada no Pluggy, o `item_id`
precisa entrar manualmente na env `PLUGGY_ITEM_ID` do worker (`hdgffcvh3ljqe61dczztaycz`) —
a auto-descoberta só funciona para conexões que já sincronizaram alguma vez.

**Colunas órfãs:** `conta.dia_fechamento` e `conta.dia_vencimento` existem no banco (migração v3)
mas não são lidas nem gravadas por ninguém — foram uma tentativa de sobrescrita manual das datas
de fatura, descontinuada. Ficaram porque reescrever migração já aplicada criaria divergência de
schema entre bancos.

**Decisão sobre o worker (21/08/2026):** o worker fica **acessível publicamente**, sem restrição
de IP. Tentamos aplicar uma `ipallowlist` no Traefik e ela **quebrou o sync** (403): o app chama
o worker pela URL pública e o Traefik enxerga um IP interno do Docker, não o IP público do
servidor (45.163.12.5). Foi revertido. A proteção real hoje é por chave (`SYNC_SECRET`), não por
rede. Se algum dia quiser fechar de verdade: fazer o app chamar o worker pela rede interna
(`http://<container>:8000/sync`) e remover o domínio público dele.

## Como trabalhar neste projeto (fluxo que deu certo)

Nenhuma skill "empacotada" foi usada — é engenharia direta (Python/Flask/SQL/Coolify API).
As skills de BRDrive disponíveis no ambiente (vendas, propostas) são de outro contexto e não
têm relação com este projeto.

O ciclo usado na sessão 2, que vale repetir:
1. **Ler o código antes de mudar** — várias vezes a causa raiz era diferente da aparente
   (ex: coluna que "não reduzia" era um `max-width:0` conflitante; divisor "sumido" entre
   Origem e Categoria era `display:flex` vazando de `.cel-origem` para o `<th>`).
2. `python3 -m py_compile app.py bussola/app.py` e `pytest tests/ -q` antes de commitar.
3. Commit + push (o webhook faz o deploy sozinho), **e então validar em produção** —
   status no Coolify, `/health`, logs (procurar traceback e `Aviso: falha ao rodar migracao`)
   e teste real da tela pelo navegador.
4. **Testar de verdade, não só ler o código.** O teste com payload real de XSS encontrou 3
   pontos que a varredura por grep não pegou. Testar em produção também já mostrou que o
   `localStorage` precisa ser limpo **antes** de recarregar, senão o estado antigo em memória
   falseia o resultado.
5. Limpar dados de teste depois (categoria de teste, teto de teste, `localStorage`).

## Como continuar no Claude Code

1. Instalar: `npm install -g @anthropic-ai/claude-code` (requer Node.js).
2. Clonar o repositório localmente:
   ```bash
   git clone https://github.com/ronaldinhodelima/pe-de-meia.git
   cd pe-de-meia
   ```
3. Este arquivo (`CLAUDE.md`) deve ficar na raiz do repositório — o Claude Code lê
   automaticamente ao iniciar uma sessão nessa pasta.
4. Autenticação git: configurar um Personal Access Token do GitHub (ou SSH key) uma única vez
   no `git credential helper` local, para não precisar colar o token a cada push.
5. Para deploys, seguir o fluxo descrito em "Repositório e deploy" acima — o token do Coolify
   precisa ser configurado como variável de ambiente local (não commitado).

## Deploy automático

Webhook do GitHub -> Coolify configurado em 20/08/2026. Todo push na `main` dispara build/deploy sozinho, sem precisar chamar a API do Coolify manualmente.

## Classificação: natureza e centro de custo (decisão de 21/08/2026)

A tela **`/pendencias`** ("Pendências de classificação", no menu Configurações) materializa
tudo isto: lista categoria sem natureza e categoria de despesa sem centro de custo, com ação
direta em cada linha, e conta os lançamentos que ainda usam natureza manual. Uma faixa de
alerta aparece no DRE quando há pendência que distorce número — natureza manual sozinha
**não** dispara alerta, porque não é erro.

Como garantir que os números do DRE são reais, sem despesa inflada:

- **A natureza vem da categoria, não do lançamento.** Para classificar uma operação fora do
  padrão (ex: um PIX de R$ 98 mil que foi a compra de um terreno), o caminho correto é **mover
  o lançamento para uma categoria com a natureza certa** ("Imóveis / Terrenos" → `bem`). Assim
  não importa se o meio foi PIX, cartão ou dinheiro — a categoria carrega a natureza.
- O campo "natureza" no modal do lançamento (`transacao.natureza`) ainda existe e continua
  sobrepondo a da categoria, mas é a via **antiga**: fica invisível para quem olha a categoria
  depois. A tela `/pendencias` conta quantos lançamentos ainda usam isso.
- **Categoria sem natureza é o problema mais grave**: o app assume `despesa` por padrão
  (`NATUREZA_PADRAO`), então uma categoria nova inventada pelo Pluggy entra como despesa
  *silenciosamente*. Não dá para bloquear o Pluggy de criar categorias — a solução é alertar
  (`/pendencias` + faixa no DRE) para o usuário decidir: definir natureza, renomear ou ocultar.
- **Centro de custo só se aplica a categorias de despesa.** Vincular receita ou transferência a
  um centro de custo não faz sentido contábil — centro de custo é análise de gasto. Por isso
  `/pendencias` só cobra vínculo das categorias com natureza `despesa`.

## Colunas ajustáveis nas tabelas

Utilitário compartilhado no `<script>` do `BASE_CSS` (roda em todas as telas). Para ligar numa
tabela nova, basta: `<table class="compacta ajustavel" data-tabela="chave-unica">`. O resto é
automático — atribui `data-col` por índice quando o HTML não traz, injeta o botão "Redefinir
colunas" e se ativa no `DOMContentLoaded`.

- `data-sem-ordenar` / `data-sem-reordenar` desligam recursos individualmente. Usado no
  **Centro de Custos**, que é hierárquico (linhas de grupo usam `colspan`): ordenar embaralharia
  a hierarquia e reordenar colunas quebraria essas linhas.
- Preferências (ordem, largura, ordenação) ficam no `localStorage`, chave `pedemeia_tabela_<chave>`.
- Redimensionar é **estilo planilha**: a coluna vizinha compensa, então a soma nunca muda e a
  tabela nunca estoura a largura da tela (a de Lançamentos não tem scroll horizontal).
- Ordenação numérica entende `R$ 1,234.56` (formato do `:,.2f` do Python, que é o usado no app)
  **e** `R$ 1.234,56` — o separador decimal é o último `.` ou `,` do texto.

Duas armadilhas já resolvidas, que voltam a morder se alguém mexer:
- Quando um filtro recarrega a tabela por AJAX (`aplicarFiltros` faz `replaceWith`), **é preciso
  chamar `ativarTabelaAjustavel()` de novo** — o elemento antigo vai embora levando os listeners.
- CSS de célula não pode vazar para o `<th>`: `.cel-origem { display:flex }` (pensado para o selo
  do banco na célula) tirava o cabeçalho do grid da tabela e fazia a coluna seguinte desenhar por
  cima. Por isso a regra é `td.cel-origem` e há um `display:table-cell !important` defensivo nos
  `th[data-col]`.

## Testes automatizados

`tests/` cobre a "regra de ouro" do DRE (o que é despesa de verdade) e as funções auxiliares
puras (`cat_pt`, `esc`, `json_script`, `chave_alfa`, `_fmt_moeda`, `_barra_html`, hash de senha,
permissões por perfil). `app.py` dá pra importar sem banco — `migrate()` e
`recarregar_categorias_db()` engolem qualquer erro de conexão e seguem com estado padrão — por
isso os testes importam o `app.py` de verdade em vez de duplicar a lógica.

Não cobre: as queries SQL em si (exigem Postgres real, fora do escopo hoje) — `TestNaturezaEfetiva`
em `tests/test_dre_logic.py` é um espelho em Python do `CASE` de `NATUREZA_SQL`; se a SQL mudar,
essa função e os testes têm que mudar junto (não há checagem automática de que os dois batem).

Rodar localmente:
```bash
pip install pytest flask psycopg2-binary
pytest tests/ -v
```
