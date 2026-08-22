// Tela de Relatórios: filtros em chip, gráfico (Chart.js) e totais agrupados.
// Todo o conteúdo vem por AJAX de /relatorios/dados e /relatorios/lancamentos,
// então este arquivo nao depende de nada interpolado pelo Python.
// ---- chip filters: dropdown com busca, checkbox toggle e navegacao por teclado ----
function cfToggle(btn) {
  const panel = btn.nextElementSibling;
  const abrir = !panel.classList.contains('show');
  document.querySelectorAll('.chip-panel.show').forEach(p => { if (p !== panel) p.classList.remove('show'); });
  if (abrir) {
    panel.classList.add('show');
    const search = panel.querySelector('.chip-search');
    if (search) { search.value = ''; cfFiltrar(search); search.focus(); }
  } else {
    panel.classList.remove('show');
  }
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('.chipfilter')) {
    document.querySelectorAll('.chip-panel.show').forEach(p => p.classList.remove('show'));
  }
});
function cfClear(e, btn) {
  e.stopPropagation();
  const panel = btn.closest('.chipfilter').querySelector('.chip-panel');
  panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
  aplicarFiltros();
}
function cfFiltrar(input) {
  const panel = input.closest('.chip-panel');
  const q = input.value.toLowerCase();
  panel.querySelectorAll('.chip-opt').forEach(opt => {
    // sem o contador: buscar "13" nao pode casar com a conta que tem 13 lancamentos
    opt.style.display = textoDaOpcao(opt).toLowerCase().includes(q) ? 'flex' : 'none';
  });
  panel.querySelectorAll('.chip-hover').forEach(o => o.classList.remove('chip-hover'));
}
function cfKeydown(e, input) {
  const panel = input.closest('.chip-panel');
  const visiveis = Array.from(panel.querySelectorAll('.chip-opt')).filter(o => o.style.display !== 'none');
  let idx = visiveis.findIndex(o => o.classList.contains('chip-hover'));
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (idx >= 0) visiveis[idx].classList.remove('chip-hover');
    idx = Math.min(idx + 1, visiveis.length - 1);
    if (visiveis[idx]) visiveis[idx].classList.add('chip-hover');
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (idx >= 0) visiveis[idx].classList.remove('chip-hover');
    idx = Math.max(idx - 1, 0);
    if (visiveis[idx]) visiveis[idx].classList.add('chip-hover');
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (idx >= 0) {
      const cb = visiveis[idx].querySelector('input[type=checkbox]');
      cb.checked = !cb.checked;
      aplicarFiltros();
    }
  } else if (e.key === 'Escape') {
    panel.classList.remove('show');
  }
}

// ---- filtros aplicados em tempo real via AJAX (o dropdown nao fecha) ----
function fmtMoeda(v) {
  return 'R$ ' + Number(v).toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function atualizarChipLabels() {
  document.querySelectorAll('.chipfilter').forEach(cf => {
    const btn = cf.querySelector('.chip-btn');
    if (!btn) return;   // defensivo: so trata caixa de filtro de verdade
    const label = btn.dataset.label;
    const n = cf.querySelectorAll('input[type=checkbox]:checked').length;
    btn.classList.toggle('ativo', n > 0);
    // sem o '+': o botao abre um filtro, nao adiciona nada
    btn.innerHTML = label + (n ? ' (' + n + ')' : '') +
      (n ? '<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' : '');
  });
  // chips pequenos mostrando tudo que esta selecionado
  const cont = document.getElementById('chipsSel');
  if (cont) {
    // [name] exigido: checkbox sem nome (ex: o do menu de colunas) nao e filtro
    const marcados = Array.from(document.querySelectorAll('.chipfilter input[type=checkbox][name]:checked'));
    // curto/completo saem do DOM via textContent, que DECODIFICA o que o Jinja
    // escapou - voltar isso cru para innerHTML reabriria o XSS. Ex: um valor de
    // dimensao chamado "<img src=x onerror=...>" criado em /dimensoes.
    cont.innerHTML = marcados.map(cb => {
      const lbl = cb.closest('.chip-opt');
      const curto = lbl.dataset.curto || textoDaOpcao(lbl);
      const completo = lbl.getAttribute('data-tip') || curto;
      // nome/valor vao em data-attribute e o clique e tratado por delegacao - ver
      // a nota equivalente em lancamentos.js
      return '<span class="chip-tag" title="' + escHtml(completo) + '"><span>' + escHtml(curto) + '</span>' +
             '<b class="chip-x" data-nome="' + escHtml(cb.name) + '" data-valor="' + escHtml(cb.value) + '">&times;</b></span>';
    }).join('');
  }
}
document.addEventListener('click', function (e) {
  const x = e.target.closest('.chip-x');
  if (x) desmarcarFiltro(x.dataset.nome, x.dataset.valor);
});
function desmarcarFiltro(nome, valor) {
  // comparacao em JS em vez de seletor CSS: valor com aspas quebraria o seletor
  const cb = Array.from(document.querySelectorAll('.chipfilter input[type=checkbox][name]'))
                  .find(c => c.name === nome && c.value === valor);
  if (cb) { cb.checked = false; aplicarFiltros(); }
}
function coletarQuery() {
  const params = new URLSearchParams();
  params.set('visao', document.getElementById('selVisao').value);
  params.set('agrupar', document.getElementById('selAgrupar').value);
  document.querySelectorAll('.chip-opt input[type=checkbox]:checked').forEach(cb => params.append(cb.name, cb.value));
  const di = document.getElementById('inputDataIni').value;
  const df = document.getElementById('inputDataFim').value;
  if (di) params.set('data_ini', di);
  if (df) params.set('data_fim', df);
  return params;
}
function aplicarFiltros() {
  atualizarChipLabels();
  const params = coletarQuery();
  history.replaceState(null, '', '/relatorios?' + params.toString());
  carregarDados(params);
}
function carregarDados(params) {
  fetch('/relatorios/dados?' + params.toString()).then(r => r.json()).then(renderResultado);
}
const LABEL_VISAO = { despesa: 'Total de despesas', receita: 'Total de receitas',
                      investimento: 'Investido / adquirido', tudo: 'Fluxo de caixa (líquido)' };
function renderResultado(data) {
  document.getElementById('totalGeral').textContent = fmtMoeda(data.total_geral);
  document.getElementById('labelTotal').textContent = LABEL_VISAO[data.visao] || 'Total no filtro';
  document.getElementById('qtdGeral').textContent = data.qtd_geral;
  const ehPeriodo = data.agrupar === 'mes';
  document.getElementById('graficoTitulo').textContent =
    ehPeriodo ? 'Evolução mês a mês' : 'Gráfico (' + data.agrupar_label + ')';
  renderGrupos(data.grupos, ehPeriodo);
  renderChart(data.grupos, ehPeriodo);
}

// ---- lista de totais agrupados, clicavel para ver os lancamentos de cada grupo ----
window.__grupos = [];
function renderGrupos(grupos, ehPeriodo) {
  // o grafico fica na ordem cronologica (linha do tempo); ja a lista abaixo
  // mostra o mes mais recente no topo, que e o que se quer olhar primeiro
  const lista = ehPeriodo ? grupos.slice().reverse() : grupos;
  window.__grupos = lista;
  const cont = document.getElementById('gruposCont');
  if (!lista.length) {
    cont.innerHTML = '<div style="color:#888;padding:10px 0">Nenhum lancamento encontrado com esses filtros.</div>';
    return;
  }
  // na linha do tempo a barra fica proporcional ao maior mes (fica legivel),
  // e mostramos a variacao em relacao ao mes anterior
  const maxTotal = Math.max.apply(null, lista.map(g => Math.abs(g.total)).concat([1]));
  cont.innerHTML = lista.map((g, i) => {
    const larguraBarra = ehPeriodo ? (Math.abs(g.total) / maxTotal * 100) : Math.max(g.pct, 0);
    let direita = '<strong>' + fmtMoeda(g.total) + '</strong> <span style="color:#aaa">' + g.pct + '%</span>';
    // lista invertida: o mes anterior e o de baixo (i + 1)
    if (ehPeriodo && i < lista.length - 1) {
      const ant = lista[i + 1].total;
      if (ant) {
        const varPct = (g.total - ant) / Math.abs(ant) * 100;
        const cor = varPct > 0 ? 'var(--bad)' : 'var(--good)';
        const sinal = varPct > 0 ? '▲' : '▼';
        direita = '<strong>' + fmtMoeda(g.total) + '</strong> ' +
                  '<span style="color:' + cor + ';font-size:12px" title="variação em relação ao mês anterior">' +
                  sinal + ' ' + Math.abs(varPct).toFixed(1) + '%</span>';
      }
    }
    return '<div>' +
      '<div class="rel-grupo-row" style="cursor:pointer" onclick="toggleGrupoDetalhe(' + i + ')">' +
        '<div style="flex:1">' +
          '<div style="display:flex;justify-content:space-between">' +
            '<span>' + (g.selo || '') + escHtml(g.nome) + ' <span style="color:#aaa">(' + escHtml(g.qtd) + ')</span></span>' +
            '<span>' + direita + '</span>' +
          '</div>' +
          '<div class="barra"><div style="width:' + larguraBarra + '%"></div></div>' +
        '</div>' +
      '</div>' +
      '<div class="rel-grupo-detalhe" id="grupoDetalhe' + i + '" style="display:none"></div>' +
    '</div>';
  }).join('');
}
function toggleGrupoDetalhe(i) {
  const el = document.getElementById('grupoDetalhe' + i);
  const abrir = el.style.display === 'none';
  document.querySelectorAll('.rel-grupo-detalhe').forEach(d => { if (d !== el) d.style.display = 'none'; });
  if (!abrir) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  if (el.dataset.loaded === '1') return;
  el.innerHTML = '<div style="padding:10px;color:#888;font-size:13px">Carregando...</div>';
  const g = window.__grupos[i];
  const params = coletarQuery();
  if (g.valor === null || g.valor === undefined) { params.set('valor_none', '1'); }
  else { params.set('valor', g.valor); }
  fetch('/relatorios/lancamentos?' + params.toString())
    .then(r => r.json())
    .then(data => {
      el.dataset.loaded = '1';
      if (!data.lancamentos.length) {
        el.innerHTML = '<div style="padding:10px;color:#888;font-size:13px">Nenhum lancamento.</div>';
        return;
      }
      // descricao, origem e categoria sao texto do banco (Pluggy ou digitado) e
      // entram em innerHTML - tem que passar por escHtml. So o selo e HTML do app.
      el.innerHTML = '<table class="rel-mini-table"><thead><tr><th>Data</th><th>Descrição</th><th>Origem</th><th>Categoria</th><th>Valor</th></tr></thead><tbody>' +
        data.lancamentos.map(l => (
          '<tr><td>' + escHtml(l.data) + '</td><td>' + escHtml(l.descricao) + '</td>' +
          '<td data-tip="' + escHtml(l.origem_completa || '') + '">' +
            (l.origem_selo || '') + escHtml(l.origem) + '</td>' +
          '<td>' + escHtml(l.categoria) + '</td>' +
          '<td class="valor">' + fmtMoeda(l.valor) + '</td></tr>'
        )).join('') +
        '</tbody></table>' +
        (data.total >= 300 ? '<div style="padding:8px 10px;color:#999;font-size:12px">Mostrando os 300 lancamentos mais recentes deste grupo.</div>' : '');
    });
}

// ---- grafico dinamico conforme os filtros aplicados ----
let chartInstance = null;
let chartTipoAtual = null;
function renderChart(grupos, ehPeriodo) {
  if (!window.Chart) return;
  const labels = grupos.map(g => g.nome);
  const valores = grupos.map(g => g.total);
  // linha do tempo (mes a mes) fica melhor como linha; os demais, como barras
  const tipo = ehPeriodo ? 'line' : 'bar';
  const dataset = ehPeriodo
    ? { label: 'Total (R$)', data: valores, borderColor: '#2e6fd6', backgroundColor: 'rgba(46,111,214,.12)',
         fill: true, tension: .3, pointRadius: 4, pointHoverRadius: 6, pointBackgroundColor: '#2e6fd6', borderWidth: 2 }
    : { label: 'Total (R$)', data: valores, backgroundColor: '#2e6fd6', borderRadius: 4, maxBarThickness: 46 };

  if (chartInstance && chartTipoAtual === tipo) {
    chartInstance.data.labels = labels;
    chartInstance.data.datasets[0] = dataset;
    chartInstance.update();
    return;
  }
  if (chartInstance) chartInstance.destroy();
  chartTipoAtual = tipo;
  chartInstance = new Chart(document.getElementById('chartGrupos'), {
    type: tipo,
    data: { labels: labels, datasets: [dataset] },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => fmtMoeda(c.parsed.y) } }
      },
      scales: {
        y: { beginAtZero: true, ticks: { callback: v => 'R$ ' + Number(v).toLocaleString('pt-BR') } }
      }
    }
  });
}

document.addEventListener('DOMContentLoaded', function() {
  carregarDados(new URLSearchParams(window.location.search));
});
