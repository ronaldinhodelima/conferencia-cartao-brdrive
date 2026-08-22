// Tela de Lançamentos. Depende de tabelas.js (escHtml, ativarTabelaAjustavel),
// carregado antes deste arquivo.
//
// Os dados que vinham interpolados pelo Python agora sao lidos de tags
// <script type="application/json"> no HTML - foi o que permitiu este arquivo
// virar estatico. lerJson() e a porta de entrada desses dados.
function lerJson(seletor, padrao) {
  const el = document.querySelector(seletor);
  if (!el) return padrao;
  try { return JSON.parse(el.textContent); } catch (e) { return padrao; }
}

// ---- chip filter (origem): filtra sem fechar o painel ----
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
  if (!e.target.closest('.chipfilter') && !e.target.closest('.chip-tag')) {
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

function atualizarChipLabels() {
  document.querySelectorAll('.chipfilter').forEach(cf => {
    const btn = cf.querySelector('.chip-btn');
    if (!btn) return;   // defensivo: so trata caixa de filtro de verdade
    const label = btn.dataset.label;
    const n = cf.querySelectorAll('input[type=checkbox][name]:checked').length;
    btn.classList.toggle('ativo', n > 0);
    // sem o '+': o botao abre um filtro, nao adiciona nada
    btn.innerHTML = label + (n ? ' (' + n + ')' : '') +
      (n ? '<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' : '');
  });
  // chips pequenos ao lado mostrando o que esta selecionado
  const cont = document.getElementById('chipsSel');
  const marcados = Array.from(document.querySelectorAll('.chipfilter input[type=checkbox][name]:checked'));
  // curto/completo saem do DOM via textContent, que DECODIFICA o que o Jinja
  // escapou - voltar isso cru para innerHTML reabriria o XSS. Ex: um valor de
  // dimensao chamado "<img src=x onerror=...>" criado em /dimensoes.
  cont.innerHTML = marcados.map(cb => {
    const lbl = cb.closest('.chip-opt');
    const curto = lbl.dataset.curto || textoDaOpcao(lbl);
    const completo = lbl.getAttribute('data-tip') || curto;
    // o valor vai num data-attribute e o clique e tratado por delegacao. Nao da
    // pra montar onclick="f('...')" aqui: o navegador decodifica a entidade ANTES
    // do JS rodar, entao um valor com aspas escaparia da string mesmo escapado.
    return '<span class="chip-tag" title="' + escHtml(completo) + '"><span>' + escHtml(curto) + '</span>' +
           '<b class="chip-x" data-valor="' + escHtml(cb.value) + '">&times;</b></span>';
  }).join('');
}
document.addEventListener('click', function (e) {
  const x = e.target.closest('.chip-x');
  if (x) desmarcarOrigem(x.dataset.valor);
});
function desmarcarOrigem(valor) {
  // comparacao em JS em vez de seletor CSS: valor com aspas quebraria o seletor
  const cb = Array.from(document.querySelectorAll('.chipfilter input[type=checkbox][name]'))
                  .find(c => c.value === valor);
  if (cb) { cb.checked = false; aplicarFiltros(); }
}

// ---- aplica filtros via AJAX: o dropdown continua aberto ----
function coletarQuery() {
  const params = new URLSearchParams();
  params.set('mes', document.getElementById('mesInput').value);
  params.set('status', document.getElementById('statusInput').value);
  // [name] exigido: checkbox sem nome (ex: o do menu de colunas) nao e filtro
  document.querySelectorAll('.chipfilter input[type=checkbox][name]:checked').forEach(cb => params.append(cb.name, cb.value));
  return params;
}
// avanca/retrocede um mes no filtro. Usa Date pra virar o ano sozinho
// (dezembro -> janeiro do ano seguinte) em vez de somar no numero do mes.
function mudarMes(delta) {
  const campo = document.getElementById('mesInput');
  const partes = (campo.value || '').split('-').map(Number);
  if (partes.length !== 2 || !partes[0] || !partes[1]) return;
  const d = new Date(partes[0], partes[1] - 1 + delta, 1);
  campo.value = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  aplicarFiltros();
}
function aplicarFiltros() {
  atualizarChipLabels();
  const params = coletarQuery();
  history.replaceState(null, '', '/?' + params.toString());
  fetch('/?' + params.toString(), { headers: { 'X-Parcial': '1' } })
    .then(r => r.text())
    .then(html => {
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const novaTabela = doc.querySelector('table.compacta');
      const novosCards = doc.querySelector('.cards');
      const novaCat = doc.querySelector('details.cat-breakdown');
      if (novaTabela) {
        document.querySelector('table.compacta').replaceWith(novaTabela);
        // a tabela nova veio do servidor sem os listeners nem as alcas de
        // redimensionar (sao criados por JS) - replaceWith descarta o elemento
        // antigo junto com tudo que estava anexado nele, entao precisa reativar
        ativarTabelaAjustavel(novaTabela, 'lancamentos');
      }
      if (novosCards) document.querySelector('.cards').replaceWith(novosCards);
      const catAtual = document.querySelector('details.cat-breakdown');
      if (novaCat && catAtual) {
        // preserva o estado aberto/fechado escolhido pelo usuario
        novaCat.open = catAtual.open;
        catAtual.replaceWith(novaCat);
      }
      const scriptNovo = doc.querySelector('script[data-detalhes]');
      if (scriptNovo) {
        try { window.detalhes = JSON.parse(scriptNovo.textContent); } catch (e) {}
      }
    });
}

window.detalhes = lerJson('script[data-detalhes]', {});
let idAtualModal = null;

function verDetalhes(id) {
  const d = window.detalhes[id];
  if (!d) return;
  idAtualModal = id;
  // 'categoria' fora da lista: virou o seletor em linha, logo abaixo do corpo
  const labels = {
    data: 'Data', descricao: 'Descrição', valor: 'Valor (R$)',
    valor_original: 'Valor original', status: 'Status', tipo: 'Tipo', origem: 'Origem',
    parcela: 'Parcela', conferida: 'Conferida', conferida_por: 'Conferida por', observacao: 'Observação'
  };
  // campos que tem tratamento proprio fora desta lista e nao podem ser repetidos
  // aqui: 'categoria' virou o seletor logo abaixo do corpo do modal
  const tratadosAparte = ['categoria'];
  let html = '';
  for (const k in labels) {
    html += '<div class="row"><span>' + labels[k] + '</span><span>' + escHtml(d[k]) + '</span></div>';
  }
  // o resto sao as dimensoes (Responsável, Projeto, ...), que o servidor manda
  // com o nome da dimensao como chave
  for (const k in d) {
    if (!(k in labels) && k.charAt(0) !== '_' && tratadosAparte.indexOf(k) === -1) {
      html += '<div class="row"><span>' + escHtml(k) + '</span><span>' + escHtml(d[k]) + '</span></div>';
    }
  }
  document.getElementById('modalBody').innerHTML = html;
  document.getElementById('modalAcoes').style.display = d._manual ? 'block' : 'none';
  // reflete o estado atual de "duplicada" da linha correspondente
  const trAtual = document.querySelector('tr[data-id="' + id + '"]');
  const dupAtual = trAtual ? trAtual.querySelector('.dup-check') : null;
  document.getElementById('modalDup').checked = dupAtual ? dupAtual.checked : false;
  // espelha a categoria da linha; mudar aqui muda la e salva
  const selCat = document.getElementById('modalCategoria');
  const catLinha = trAtual ? trAtual.querySelector('.cat-select') : null;
  if (selCat && catLinha) selCat.value = catLinha.value;
  document.getElementById('modalBg').classList.add('show');
}

function salvarCategoriaModal() {
  if (!idAtualModal) return;
  const tr = document.querySelector('tr[data-id="' + idAtualModal + '"]');
  if (!tr) return;
  const selLinha = tr.querySelector('.cat-select');
  selLinha.value = document.getElementById('modalCategoria').value;
  salvar(idAtualModal, selLinha);
  // a categoria carrega a natureza contabil, entao os totais do mes mudam
  guardarPosicaoAtual();
  setTimeout(() => window.location.reload(), 600);
}
function toggleDuplicadaModal() {
  if (!idAtualModal) return;
  const marcado = document.getElementById('modalDup').checked;
  const tr = document.querySelector('tr[data-id="' + idAtualModal + '"]');
  if (!tr) return;
  const dupCheck = tr.querySelector('.dup-check');
  dupCheck.checked = marcado;
  const obsInput = tr.querySelector('.obs-input');
  if (marcado && !obsInput.value.trim()) {
    obsInput.value = DUPLICADA_OBS_PADRAO;
  }
  salvar(idAtualModal, dupCheck);
}
function fecharModal() {
  document.getElementById('modalBg').classList.remove('show');
  idAtualModal = null;
}
// o ESC e tratado no tabelas.js, que so tira a classe .show - aqui a tela limpa
// o proprio estado
window.aoFecharModal = function () { idAtualModal = null; };
function excluirManual() {
  if (!idAtualModal) return;
  const d = window.detalhes[idAtualModal] || {};
  if (!confirm('Excluir definitivamente este lançamento manual?\n\n' + (d.descricao || '') + '  ' + (d.valor || ''))) return;
  fetch('/api/lancamento-manual/' + idAtualModal, { method: 'DELETE' })
    .then(r => r.json())
    .then(res => {
      if (res.ok) { fecharModal(); guardarPosicaoAtual(); window.location.reload(); }
      else alert(res.erro || 'Não foi possível excluir.');
    });
}
// ---- delegacao dos eventos da tabela ----
// Os handlers eram inline (onclick/onchange no HTML) com o id interpolado pelo
// Jinja. O |tojson gera aspas duplas literais, que FECHAM o atributo antes da
// hora: onclick="linhaClick(event, "abc-123")" vira onclick="linhaClick(event, "
// e o resto do id vira atributo solto. Resultado: nem o modal abria nem as
// edicoes salvavam. Com delegacao o id sai do data-id da linha e nada e
// interpolado dentro de atributo - alem de continuar valendo para a tabela que
// o filtro AJAX substitui.
function idDaLinha(el) {
  const tr = el.closest('tr[data-id]');
  return tr ? tr.dataset.id : null;
}

document.addEventListener('click', function (e) {
  const tr = e.target.closest('#tabela-lancamentos tbody tr[data-id]');
  if (!tr) return;
  if (['SELECT', 'INPUT', 'OPTION', 'BUTTON'].includes(e.target.tagName)) return;
  verDetalhes(tr.dataset.id);
});

document.addEventListener('change', function (e) {
  const el = e.target;
  if (!el.matches('#tabela-lancamentos .cat-select, #tabela-lancamentos .dim-select, #tabela-lancamentos .conf-check')) return;
  const id = idDaLinha(el);
  if (id) salvar(id, el);
});

// blur nao borbulha; focusout sim
document.addEventListener('focusout', function (e) {
  if (!e.target.matches('#tabela-lancamentos .obs-input')) return;
  const id = idDaLinha(e.target);
  if (id) salvar(id, e.target);
});
const DUPLICADA_OBS_PADRAO = lerJson('script[data-config]', {}).duplicada_obs || '';
const filaSalvar = {};
function salvar(id, el) {
  const tr = el.closest('tr');
  const dimensoes = {};
  tr.querySelectorAll('.dim-select').forEach(sel => {
    dimensoes[sel.dataset.dim] = sel.value || null;
  });
  const payload = {
    conferida: tr.querySelector('.conf-check').checked,
    duplicada: tr.querySelector('.dup-check').checked,
    observacao: tr.querySelector('.obs-input').value,
    categoria: tr.querySelector('.cat-select').value,
    natureza: (window.detalhes[id] || {})._natureza || '',
    dimensoes: dimensoes
  };
  const anterior = filaSalvar[id] || Promise.resolve();
  const atual = anterior.then(() => fetch('/api/transacao/' + id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  })).then(r => r.json()).then(d => {
    if (d.ok) {
      const confFinal = payload.conferida && !d.bloqueada;
      tr.querySelector('.conf-check').checked = confFinal;
      tr.classList.toggle('conferida', confFinal);
      tr.classList.toggle('duplicada', payload.duplicada);
      tr.querySelectorAll('.dim-select').forEach(sel => {
        sel.style.borderColor = '';
        sel.style.background = '';
      });
      if (d.bloqueada) {
        (d.faltando || []).forEach(dimId => {
          const sel = tr.querySelector('.dim-select[data-dim="' + dimId + '"]');
          if (sel) { sel.style.borderColor = '#c23c34'; sel.style.background = '#fbeceb'; }
        });
        alert('Nao foi possivel confirmar: preencha os campos obrigatorios destacados em vermelho.');
      }
      const s = document.getElementById('status-' + id);
      if (s) { s.classList.add('show'); setTimeout(() => s.classList.remove('show'), 1500); }
    }
  });
  filaSalvar[id] = atual;
}
function toggleFormManual() {
  const f = document.getElementById('formManual');
  f.style.display = f.style.display === 'none' ? 'block' : 'none';
}
function salvarManual(e) {
  e.preventDefault();
  const statusEl = document.getElementById('manualStatus');
  statusEl.textContent = 'Salvando...';
  const payload = {
    data: document.getElementById('manualData').value,
    descricao: document.getElementById('manualDescricao').value,
    direcao: document.getElementById('manualDirecao').value,
    valor: document.getElementById('manualValor').value,
    categoria: document.getElementById('manualCategoria').value
  };
  fetch('/api/lancamento-manual', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(r => r.json()).then(d => {
    if (d.ok) { guardarPosicaoAtual(); window.location.reload(); }
    else { statusEl.textContent = d.erro || 'Falha ao salvar'; }
  }).catch(() => { statusEl.textContent = 'Falha ao salvar'; });
  return false;
}
atualizarChipLabels();
