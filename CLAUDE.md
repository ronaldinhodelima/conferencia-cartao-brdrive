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

- **App principal**: `app.py` (Flask monolítico, um arquivo só, HTML/CSS/JS embutido via
  f-strings — sem templates separados, sem framework front-end).
- **Banco**: PostgreSQL, schema `cartao.*` (rodando dentro do Coolify, host interno de rede
  Docker — não acessível de fora).
- **Worker de sincronização**: `bussola/app.py` (serviço separado no Coolify) — busca dados do
  Pluggy e grava no mesmo Postgres.
- **Deploy**: Coolify (PaaS auto-hospedado da BRDrive), build via Dockerfile a partir de um
  repositório git no GitHub.

## Repositório e deploy

- **GitHub**: `ronaldinhodelima/conferencia-cartao-brdrive` (branch `main`).
  - `app.py` → app principal (Flask).
  - `bussola/app.py` → worker de sincronização Pluggy.
  - `Dockerfile` → build do app principal.
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
  git clone https://github.com/ronaldinhodelima/conferencia-cartao-brdrive.git
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
- `cartao_nome` — apelido de cada cartão pelos 4 últimos dígitos.
- `regra_classificacao` / `regra_dimensao_valor` — regras automáticas de categorização.
- `dimensao` / `dimensao_valor` / `transacao_dimensao` — dimensões livres (ex: Responsável:
  Ronaldo/Andrea/Amanda/Compartilhado, Projeto, etc.) além da categoria.

## Modelo de natureza (5 categorias, base do DRE)

`despesa`, `receita`, `investimento`, `bem`, `transferencia`, e `fluxo` (default: direção do
lançamento decide se é receita ou despesa — usado pra PIX/TED/dinheiro).

## Sistema de permissões

Perfis: `admin` (tudo), `operador` (lançamentos + relatórios + importar + sincronizar, sem
cadastros/usuários), `leitura` (só ver lançamentos e relatórios). Permissões granulares:
`lancamentos_ver`, `lancamentos_editar`, `lancamentos_conferir`, `lancamentos_manual`,
`importar`, `relatorios`, `cadastros`, `sincronizar`, `usuarios`. Decorator `@requer(permissao)`
protege cada rota; `pode(permissao)` controla o que aparece na interface.

Usuários atuais: `ronaldo` (admin), `andrea` (admin, herdado do sistema antigo), `amanda`
(operador, criada nesta sessão).

## Identidade visual

- Nome: **Pé de Meia**. Logo oficial fornecida pelo usuário (meia de tricô com dinheiro),
  sempre usando a variação de **fundo claro sólido** (nunca a transparente nem a escura).
  Os PNGs já cortados/otimizados foram embutidos como base64 direto no `app.py`
  (`LOGO_FAVICON_B64`, `LOGO_TOPBAR_B64`, `LOGO_HERO_B64`) — não dependem de arquivo externo.
- Bancos identificados por "selo" colorido em CSS puro (cor da marca + sigla de 2 letras),
  não por logo de imagem — Pluggy não fornece logo utilizável.
- Tooltips customizados (120ms, mais rápido que o `title` nativo do navegador).

## Funcionalidades já construídas (nesta sessão, em ordem)

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

## Pendências conhecidas

- Nenhuma pendência técnica aberta no momento. Toda vez que uma nova conexão bancária for
  adicionada no Pluggy, lembrar que o `item_id` precisa ser adicionado manualmente na env var
  `PLUGGY_ITEM_ID` do worker de sync (`hdgffcvh3ljqe61dczztaycz`) — a auto-descoberta só
  funciona para conexões que já sincronizaram alguma vez.
- Considerar migrar de `git push` manual + Coolify API para um **webhook automático** do
  GitHub → Coolify (deploy automático a cada push na `main`), o que eliminaria a etapa manual
  de disparar o deploy.

## Skills / ferramentas usadas nesta sessão

Nenhuma skill "empacotada" do Cowork foi usada para construir o Pé de Meia — foi trabalho de
engenharia direta (Python/Flask/SQL/Coolify API) via Bash, Edit, Read, Write. As skills de
BRDrive disponíveis no ambiente (vendas, propostas comerciais, etc.) são de outro contexto
(comercial) e não têm relação com este projeto financeiro pessoal.

## Como continuar no Claude Code

1. Instalar: `npm install -g @anthropic-ai/claude-code` (requer Node.js).
2. Clonar o repositório localmente:
   ```bash
   git clone https://github.com/ronaldinhodelima/conferencia-cartao-brdrive.git
   cd conferencia-cartao-brdrive
   ```
3. Este arquivo (`CLAUDE.md`) deve ficar na raiz do repositório — o Claude Code lê
   automaticamente ao iniciar uma sessão nessa pasta.
4. Autenticação git: configurar um Personal Access Token do GitHub (ou SSH key) uma única vez
   no `git credential helper` local, para não precisar colar o token a cada push.
5. Para deploys, seguir o fluxo descrito em "Repositório e deploy" acima — o token do Coolify
   precisa ser configurado como variável de ambiente local (não commitado).
