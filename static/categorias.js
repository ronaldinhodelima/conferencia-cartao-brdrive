// Modal que lista os lancamentos de uma categoria "protegida" (com lancamentos,
// por isso nao pode ser removida). Usa escHtml() de tabelas.js.
function fecharModalLanc() {
  document.getElementById('modalLancBg').classList.remove('show');
}

function verLancamentosCategoria(btn) {
  const categoria = btn.dataset.categoria;
  const corpo = document.getElementById('modalLancBody');
  document.getElementById('modalLancTitulo').textContent =
    'Lançamentos — ' + btn.closest('tr').querySelector('input[name=novo_nome]').value;
  corpo.innerHTML = '<div style="padding:12px 0;color:var(--ink-faint);font-size:13px">Carregando…</div>';
  document.getElementById('modalLancBg').classList.add('show');
  fetch('/api/categoria-lancamentos?categoria=' + encodeURIComponent(categoria))
    .then(r => r.json())
    .then(lista => {
      if (!lista.length) {
        corpo.innerHTML = '<div style="padding:12px 0;color:var(--ink-faint);font-size:13px">Nenhum lançamento encontrado.</div>';
        return;
      }
      corpo.innerHTML = lista.map(l =>
        '<div class="row"><span>' + escHtml(l.data) + ' — ' + escHtml(l.descricao) + '</span>' +
        '<span>R$ ' + l.valor.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</span></div>'
      ).join('');
    })
    .catch(() => {
      corpo.innerHTML = '<div style="padding:12px 0;color:var(--bad)">Erro ao carregar os lançamentos.</div>';
    });
}
