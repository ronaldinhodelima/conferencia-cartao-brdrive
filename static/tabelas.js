// Escapa texto antes de jogar em innerHTML. Fica aqui por ser compartilhado:
// varias telas montam HTML no cliente a partir de dado vindo do banco.
function escHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

// ---------- colunas ajustaveis (compartilhado por todas as telas) ----------
// Marque a tabela com class="ajustavel" e data-tabela="chave-unica" que o resto
// e automatico: as colunas ganham data-col por indice (se ainda nao tiverem),
// o botao "Redefinir colunas" e injetado acima da tabela e as preferencias de
// ordem/largura/ordenacao ficam no localStorage por chave.
// Opcional: data-sem-ordenar / data-sem-reordenar para tabelas hierarquicas
// (linhas com colspan), onde ordenar ou trocar colunas de lugar nao faz sentido.
function redefinirColunas(chave) {
  localStorage.removeItem('pedemeia_tabela_' + chave);
  guardarPosicaoAtual();
  window.location.reload();
}

function ativarTabelaAjustavel(table, chave, opcoes) {
  if (!table) return;
  opcoes = opcoes || {};
  const podeOrdenar = !opcoes.semOrdenar && !table.hasAttribute('data-sem-ordenar');
  const podeReordenar = !opcoes.semReordenar && !table.hasAttribute('data-sem-reordenar');
  const thead = table.querySelector('thead tr');
  if (!thead) return;
  const CHAVE = 'pedemeia_tabela_' + chave;

  // 1) garante data-col em todo th/td (por indice, quando o HTML nao trouxe).
  // Roda antes de aplicar a ordem salva, entao o DOM ainda esta na ordem do servidor.
  const thsOriginais = [...thead.children];
  thsOriginais.forEach((th, i) => { if (!th.dataset.col) th.dataset.col = 'c' + i; });
  const ordemOriginal = thsOriginais.map(th => th.dataset.col);
  table.querySelectorAll('tbody tr').forEach(tr => {
    const tds = [...tr.children];
    // linha com colspan (ex: cabecalho de grupo) tem contagem diferente - fica de fora
    if (tds.length !== thsOriginais.length) return;
    tds.forEach((td, i) => { if (!td.dataset.col) td.dataset.col = ordemOriginal[i]; });
  });

  let estado;
  try { estado = JSON.parse(localStorage.getItem(CHAVE) || '{}'); } catch (e) { estado = {}; }
  function salvarEstado() { localStorage.setItem(CHAVE, JSON.stringify(estado)); }
  function colunasNaOrdemAtual() {
    return [...thead.querySelectorAll('th[data-col]')].map(th => th.dataset.col);
  }
  function aplicarLargura(col, px) {
    const th = thead.querySelector('th[data-col="' + col + '"]');
    if (th) th.style.width = px + 'px';
    table.querySelectorAll('td[data-col="' + col + '"]').forEach(td => { td.style.width = px + 'px'; });
  }
  function reordenarLinhas() {
    const ordem = colunasNaOrdemAtual();
    table.querySelectorAll('tbody tr').forEach(tr => {
      const mapaTd = {};
      tr.querySelectorAll('td[data-col]').forEach(td => { mapaTd[td.dataset.col] = td; });
      ordem.forEach(col => { if (mapaTd[col]) tr.appendChild(mapaTd[col]); });
    });
  }

  // 2) barra acima da tabela: campo de filtro na esquerda, "Redefinir colunas" na
  //    direita. Injetada automaticamente, uma vez por tabela.
  if (!table.previousElementSibling || !table.previousElementSibling.classList.contains('barra-colunas')) {
    const barra = document.createElement('div');
    barra.className = 'barra-colunas';
    barra.style.cssText = 'display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:6px';

    const esq = document.createElement('div');
    esq.style.cssText = 'display:flex;align-items:center;gap:8px';
    const busca = document.createElement('input');
    busca.type = 'search';
    busca.className = 'filtro-tabela';
    busca.placeholder = 'Filtrar';
    busca.setAttribute('aria-label', 'Filtrar');
    busca.style.cssText = 'padding:6px 9px;border:1px solid var(--line);border-radius:6px;font-size:13px;width:200px';
    const contador = document.createElement('span');
    contador.style.cssText = 'font-size:12px;color:var(--ink-faint)';
    esq.appendChild(busca);
    esq.appendChild(contador);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ver-btn';
    btn.title = 'Volta a ordem, largura e ordenação das colunas ao padrão';
    btn.textContent = '↺ Redefinir colunas';
    btn.addEventListener('click', function () { redefinirColunas(chave); });

    barra.appendChild(esq);
    barra.appendChild(btn);
    table.parentNode.insertBefore(barra, table);
    ativarFiltroTabela(table, busca, contador);
  }

  // 3) ordem salva
  if (podeReordenar && estado.ordem && estado.ordem.length) {
    const mapaTh = {};
    thead.querySelectorAll('th[data-col]').forEach(th => { mapaTh[th.dataset.col] = th; });
    estado.ordem.forEach(col => { if (mapaTh[col]) thead.appendChild(mapaTh[col]); });
    reordenarLinhas();
  }

  // 4) larguras normalizadas para caber exatamente no container (sem rolagem).
  // Medir a propria tabela nao serve: com table-layout:fixed ela ja estoura pra
  // caber a soma das colunas, entao o alvo sairia errado. O pai nao estoura.
  const larguraBase = {};
  thead.querySelectorAll('th[data-col]').forEach(th => {
    larguraBase[th.dataset.col] = (estado.larguras && estado.larguras[th.dataset.col]) || th.getBoundingClientRect().width;
  });
  const soma = Object.values(larguraBase).reduce((a, b) => a + b, 0);
  const alvo = table.parentElement.clientWidth;
  if (soma > 0 && alvo > 0) {
    const fator = alvo / soma;
    Object.keys(larguraBase).forEach(col => aplicarLargura(col, Math.max(40, larguraBase[col] * fator)));
  }

  // 5) redimensionar: arrastar tira/da espaco da coluna vizinha (soma constante)
  let redimensionandoAgora = false;
  thead.querySelectorAll('th[data-col]').forEach(th => {
    if (th.querySelector('.col-resize-handle')) return;
    const alca = document.createElement('span');
    alca.className = 'col-resize-handle';
    alca.draggable = false;
    th.appendChild(alca);
    alca.addEventListener('click', function (e) { e.stopPropagation(); });
    alca.addEventListener('mousedown', function (e) {
      e.preventDefault();
      e.stopPropagation();
      const thVizinho = th.nextElementSibling;
      if (!thVizinho || !thVizinho.dataset.col) return;
      redimensionandoAgora = true;
      const startX = e.clientX;
      const larguraInicial = th.getBoundingClientRect().width;
      const larguraInicialVizinho = thVizinho.getBoundingClientRect().width;
      function mover(e2) {
        const delta = e2.clientX - startX;
        const nova = larguraInicial + delta;
        const novaVizinho = larguraInicialVizinho - delta;
        if (nova < 40 || novaVizinho < 40) return;
        aplicarLargura(th.dataset.col, nova);
        aplicarLargura(thVizinho.dataset.col, novaVizinho);
      }
      function soltar() {
        document.removeEventListener('mousemove', mover);
        document.removeEventListener('mouseup', soltar);
        estado.larguras = estado.larguras || {};
        estado.larguras[th.dataset.col] = th.getBoundingClientRect().width;
        estado.larguras[thVizinho.dataset.col] = thVizinho.getBoundingClientRect().width;
        salvarEstado();
        // rede de seguranca: normalmente a trava e consumida pelo handler de click
        // do th (a alca se move junto com a coluna, entao o click pode cair fora dela)
        setTimeout(function () { redimensionandoAgora = false; }, 300);
      }
      document.addEventListener('mousemove', mover);
      document.addEventListener('mouseup', soltar);
    });
  });

  // 6) reordenar arrastando o cabecalho
  if (podeReordenar) {
    let arrastando = null;
    thead.querySelectorAll('th[data-col]').forEach(th => {
      th.draggable = true;
      th.addEventListener('dragstart', function () { arrastando = th; th.classList.add('arrastando'); });
      th.addEventListener('dragend', function () {
        th.classList.remove('arrastando');
        thead.querySelectorAll('th[data-col]').forEach(t => t.classList.remove('arrastar-sobre'));
      });
      th.addEventListener('dragover', function (e) {
        e.preventDefault();
        if (th !== arrastando) th.classList.add('arrastar-sobre');
      });
      th.addEventListener('dragleave', function () { th.classList.remove('arrastar-sobre'); });
      th.addEventListener('drop', function (e) {
        e.preventDefault();
        th.classList.remove('arrastar-sobre');
        if (!arrastando || arrastando === th) return;
        const rect = th.getBoundingClientRect();
        const antes = (e.clientX - rect.left) < rect.width / 2;
        th.parentNode.insertBefore(arrastando, antes ? th : th.nextSibling);
        reordenarLinhas();
        estado.ordem = colunasNaOrdemAtual();
        salvarEstado();
      });
    });
  }

  // 7) ordenar clicando no titulo
  if (podeOrdenar) {
    function valorOrdenavel(td) {
      if (!td) return '';
      if (td.dataset.sort !== undefined && td.dataset.sort !== '') return parseFloat(td.dataset.sort);
      const sel = td.querySelector('select');
      if (sel) return (sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '').toLowerCase();
      const inp = td.querySelector('input[type=text]');
      if (inp) return inp.value.toLowerCase();
      const txt = td.textContent.trim();
      // valor monetario/percentual ordena como numero. O separador decimal e o
      // ULTIMO '.' ou ',' que aparecer - assim funciona tanto no formato que o
      // app usa hoje (R$ 1,234.56, do :,.2f do Python) quanto no brasileiro
      // (R$ 1.234,56), sem depender de qual esta em uso.
      const limpo = txt.replace(/[R$\s%]/g, '');
      const ultVirgula = limpo.lastIndexOf(',');
      const ultPonto = limpo.lastIndexOf('.');
      const numerico = ultVirgula > ultPonto
        ? limpo.replace(/\./g, '').replace(',', '.')   // decimal e virgula
        : limpo.replace(/,/g, '');                     // decimal e ponto (ou sem decimal)
      if (numerico !== '' && numerico !== '-' && !isNaN(Number(numerico))) return Number(numerico);
      return txt.toLowerCase();
    }
    function ordenarLinhas(col, dir) {
      const tbody = table.querySelector('tbody');
      if (!tbody) return;
      const linhas = [...tbody.querySelectorAll('tr')];
      linhas.sort(function (a, b) {
        const va = valorOrdenavel(a.querySelector('td[data-col="' + col + '"]'));
        const vb = valorOrdenavel(b.querySelector('td[data-col="' + col + '"]'));
        const cmp = (typeof va === 'number' && typeof vb === 'number') ? va - vb : String(va).localeCompare(String(vb));
        return dir === 'asc' ? cmp : -cmp;
      });
      linhas.forEach(tr => tbody.appendChild(tr));
    }
    function atualizarIndicadores() {
      thead.querySelectorAll('th[data-col]').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (estado.sort && estado.sort.col === th.dataset.col) {
          th.classList.add(estado.sort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
        }
      });
    }
    thead.querySelectorAll('th[data-col]').forEach(th => {
      th.addEventListener('click', function (e) {
        if (redimensionandoAgora) { redimensionandoAgora = false; return; }
        if (e.target.classList.contains('col-resize-handle')) return;
        const col = th.dataset.col;
        const dir = (estado.sort && estado.sort.col === col && estado.sort.dir === 'asc') ? 'desc' : 'asc';
        estado.sort = { col: col, dir: dir };
        salvarEstado();
        ordenarLinhas(col, dir);
        atualizarIndicadores();
      });
    });
    if (estado.sort) ordenarLinhas(estado.sort.col, estado.sort.dir);
    atualizarIndicadores();
  }
}

// ativa sozinho toda tabela marcada com class="ajustavel" e data-tabela="chave"
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('table.ajustavel[data-tabela]').forEach(function (t) {
    ativarTabelaAjustavel(t, t.dataset.tabela);
  });
});

// ---- ESC fecha qualquer modal aberto (compartilhado por todas as telas) ----
// Todas as telas usam a mesma marcacao .modal-bg + classe .show, entao um unico
// handler cobre os detalhes do lancamento, a lista de lancamentos da categoria e
// qualquer modal que venha depois. Cada tela limpa o proprio estado em
// window.aoFecharModal (ex: o id do lancamento que estava aberto).
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;
  const abertos = document.querySelectorAll('.modal-bg.show');
  if (!abertos.length) return;
  abertos.forEach(function (m) { m.classList.remove('show'); });
  if (typeof window.aoFecharModal === 'function') window.aoFecharModal();
});

// ---- manter a posicao da pagina ao salvar ----
// Quase toda tela de cadastro reenvia o formulario e a view devolve a pagina
// inteira (nao ha redirect). Para o navegador e um documento novo, e documento
// novo abre no topo - quem estava editando um item la embaixo perde o lugar a
// cada alteracao.
//
// Guardamos a posicao no sessionStorage e voltamos para ela quando a pagina nova
// chega. Ativa sozinho em todas as telas (ver o final do arquivo), entao tela
// nova ja nasce com o comportamento certo.
const POS_CHAVE = 'pedemeia_pos_' + location.pathname;
const POS_VALIDADE_MS = 15000;

// Chame antes de um window.location.reload() feito por JS: recarregar por codigo
// nao dispara o evento submit, entao a posicao nao seria guardada sozinha.
function guardarPosicaoAtual() {
  try {
    sessionStorage.setItem(POS_CHAVE, JSON.stringify({
      y: window.scrollY,
      // o <details> de ajuda fica no topo de varias telas: se voltasse fechado,
      // tudo abaixo subiria e a rolagem cairia no lugar errado
      abertos: Array.from(document.querySelectorAll('details')).map(function (d) { return d.open; }),
      em: Date.now(),
    }));
  } catch (e) { /* sessionStorage indisponivel: so perde a posicao */ }
}

function manterPosicaoAoSalvar() {
  document.addEventListener('submit', guardarPosicaoAtual);

  let estado = null;
  try {
    estado = JSON.parse(sessionStorage.getItem(POS_CHAVE) || 'null');
    sessionStorage.removeItem(POS_CHAVE);
  } catch (e) { return; }
  if (!estado) return;
  // envio cancelado (confirm recusado) deixa a posicao guardada sem navegacao
  // nenhuma - sem esta checagem, a proxima visita a tela daria um pulo sozinho
  if (Date.now() - (estado.em || 0) > POS_VALIDADE_MS) return;

  document.querySelectorAll('details').forEach(function (d, i) {
    if ((estado.abertos || [])[i]) d.open = true;
  });
  // espera o layout assentar (larguras de coluna sao aplicadas por JS) antes de rolar
  requestAnimationFrame(function () { window.scrollTo(0, estado.y || 0); });
}

document.addEventListener('DOMContentLoaded', manterPosicaoAoSalvar);


// ---- filtro de conteudo da tabela ----
// Filtra no cliente: esconde as linhas que nao casam, sem ida ao servidor. So
// enxerga o que ja esta carregado na tela - em Lancamentos, por exemplo, filtra
// dentro do mes aberto, nao no historico inteiro.
function normalizarBusca(texto) {
  return String(texto == null ? '' : texto)
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
}

// Texto que representa a linha para efeito de busca. Nao da para usar o
// textContent puro: as celulas trazem <select> cujas opcoes listam TODAS as
// categorias, entao qualquer busca casaria com todas as linhas. Aqui as opcoes
// sao descartadas e entram, no lugar, o valor dos campos e a opcao escolhida.
function textoFiltravelDaLinha(tr) {
  if (tr.__textoFiltro != null) return tr.__textoFiltro;
  const partes = [];
  tr.querySelectorAll('td').forEach(function (td) {
    const clone = td.cloneNode(true);
    clone.querySelectorAll('select').forEach(function (s) { s.remove(); });
    partes.push(clone.textContent);
  });
  tr.querySelectorAll('input').forEach(function (i) {
    if (['checkbox', 'radio', 'hidden'].indexOf(i.type) === -1) partes.push(i.value);
  });
  tr.querySelectorAll('select').forEach(function (s) {
    const opt = s.options[s.selectedIndex];
    if (opt) partes.push(opt.textContent);
  });
  tr.__textoFiltro = normalizarBusca(partes.join(' '));
  return tr.__textoFiltro;
}

function ativarFiltroTabela(table, campo, contador) {
  const corpo = table.tBodies[0];
  if (!corpo) return;
  // tabela hierarquica (Centro de Custos): a linha com colspan abre um bloco e as
  // seguintes sao filhas dela. Esconder linha solta quebraria a arvore - um
  // subgrupo apareceria sem o centro de custo dele.
  const hierarquica = !!corpo.querySelector('tr > td[colspan]');

  // valor editado invalida o texto guardado daquela linha
  table.addEventListener('input', function (e) {
    const tr = e.target.closest('tr');
    if (tr) tr.__textoFiltro = null;
  });
  table.addEventListener('change', function (e) {
    const tr = e.target.closest('tr');
    if (tr) tr.__textoFiltro = null;
  });

  function aplicar() {
    const q = normalizarBusca(campo.value).trim();
    const dados = Array.from(corpo.rows);

    if (!q) {
      dados.forEach(function (tr) { tr.style.display = ''; });
      contador.textContent = '';
      return;
    }

    const casa = dados.map(function (tr) { return textoFiltravelDaLinha(tr).indexOf(q) !== -1; });
    const visivel = casa.slice();

    if (hierarquica) {
      let inicioBloco = -1;
      dados.forEach(function (tr, i) {
        const ehCabecalho = !!tr.querySelector(':scope > td[colspan]');
        if (ehCabecalho) inicioBloco = i;
        // filho que casa traz o cabecalho do bloco junto
        if (!ehCabecalho && casa[i] && inicioBloco >= 0) visivel[inicioBloco] = true;
      });
      // cabecalho que casa mostra o bloco inteiro
      let atual = -1;
      dados.forEach(function (tr, i) {
        const ehCabecalho = !!tr.querySelector(':scope > td[colspan]');
        if (ehCabecalho) atual = casa[i] ? i : -1;
        else if (atual >= 0) visivel[i] = true;
      });
    }

    let n = 0;
    dados.forEach(function (tr, i) {
      tr.style.display = visivel[i] ? '' : 'none';
      if (visivel[i]) n++;
    });
    contador.textContent = n + ' de ' + dados.length;
  }

  campo.addEventListener('input', aplicar);
  campo.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { campo.value = ''; aplicar(); e.stopPropagation(); }
  });
}
