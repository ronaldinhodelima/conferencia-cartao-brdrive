// Tela de importacao de extrato/fatura (OFX/CSV).
// escHtml() vem de tabelas.js, carregado antes deste arquivo.

let itensPreview = [];

function fmtMoeda(v) {
  return 'R$ ' + Number(v).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function enviarPreview(e) {
  e.preventDefault();
  const arq = document.getElementById('arquivo').files[0];
  if (!arq) return false;
  const msg = document.getElementById('msg');
  msg.textContent = 'Lendo arquivo...';
  msg.style.color = 'var(--ink-soft)';
  const fd = new FormData();
  fd.append('arquivo', arq);
  fd.append('origem', document.getElementById('origem').value);
  fd.append('inverter', document.getElementById('inverter').checked ? '1' : '0');
  fetch('/api/importar/preview', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { msg.textContent = d.erro; msg.style.color = 'var(--bad)'; return; }
      msg.textContent = d.total + ' lançamentos lidos (' + d.periodo + ').';
      msg.style.color = 'var(--good)';
      itensPreview = d.itens;
      renderPreview(d);
    })
    .catch(() => { msg.textContent = 'Falha ao ler o arquivo.'; msg.style.color = 'var(--bad)'; });
  return false;
}

function renderPreview(d) {
  document.getElementById('resumoPreview').innerHTML =
    '<div class="card"><div class="label">Lidos</div><div class="val">' + escHtml(d.total) + '</div></div>' +
    '<div class="card"><div class="label">Novos</div><div class="val" style="color:var(--good)">' + escHtml(d.novos) + '</div></div>' +
    '<div class="card"><div class="label">Já existem</div><div class="val" style="color:var(--ink-faint)">' + escHtml(d.duplicados) + '</div></div>' +
    '<div class="card"><div class="label">Período</div><div class="val" style="font-size:15px">' + escHtml(d.periodo) + '</div></div>';

  // a descricao vem de um arquivo enviado pelo usuario (OFX/CSV) - conteudo
  // de terceiro, tem que ser escapado antes de virar innerHTML
  document.getElementById('corpoPreview').innerHTML = d.itens.map((it, i) => (
    '<tr' + (it.duplicado ? ' style="color:var(--ink-faint)"' : '') + '>' +
      '<td class="cel-check"><input type="checkbox" data-i="' + i + '"' + (it.duplicado ? '' : ' checked') + '></td>' +
      '<td class="cel-data">' + escHtml(it.data_fmt) + '</td>' +
      '<td class="cel-desc" title="' + escHtml(it.descricao) + '">' + escHtml(it.descricao) + '</td>' +
      '<td class="valor cel-valor" style="' + (it.valor < 0 ? 'color:var(--good)' : '') + '">' + escHtml(fmtMoeda(it.valor)) + '</td>' +
      '<td class="cel-origem">' + (it.duplicado ? 'já existe' : 'novo') + '</td>' +
    '</tr>'
  )).join('');

  document.getElementById('blocoPreview').style.display = 'block';
  // o corpo desta tabela so existe depois do preview chegar - reativa as
  // colunas ajustaveis agora que as linhas estao no DOM
  ativarTabelaAjustavel(document.querySelector('table[data-tabela="importar-preview"]'), 'importar-preview');
}

function marcarTodos(v) {
  document.querySelectorAll('#corpoPreview input[type=checkbox]').forEach(cb => cb.checked = v);
}

function marcarSomenteNovos() {
  document.querySelectorAll('#corpoPreview input[type=checkbox]').forEach(cb => {
    cb.checked = !itensPreview[cb.dataset.i].duplicado;
  });
}

function confirmarImport() {
  const sel = Array.from(document.querySelectorAll('#corpoPreview input[type=checkbox]:checked'))
                   .map(cb => itensPreview[cb.dataset.i]);
  if (!sel.length) { alert('Nenhuma linha selecionada.'); return; }
  if (!confirm('Importar ' + sel.length + ' lançamento(s)?')) return;
  const btn = document.getElementById('btnImportar');
  btn.disabled = true; btn.textContent = 'Importando...';
  fetch('/api/importar/confirmar', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ origem: document.getElementById('origem').value, itens: sel })
  }).then(r => r.json()).then(d => {
    btn.disabled = false; btn.textContent = 'Importar selecionados';
    if (!d.ok) { alert(d.erro || 'Falha ao importar.'); return; }
    const msg = document.getElementById('msg');
    msg.style.color = 'var(--good)';
    msg.textContent = d.inseridos + ' lançamento(s) importado(s).' +
      (d.ignorados ? ' ' + d.ignorados + ' já existiam e foram ignorados.' : '');
    document.getElementById('blocoPreview').style.display = 'none';
  }).catch(() => {
    btn.disabled = false; btn.textContent = 'Importar selecionados';
    alert('Falha ao importar.');
  });
}
