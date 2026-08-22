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
      // cada linha abre os detalhes do lancamento; o id vai em data-attribute e o
      // clique e tratado por delegacao, sem handler inline
      corpo.innerHTML = lista.map(l =>
        '<div class="row linha-tx" data-tx="' + escHtml(l.transacao_id) + '" style="cursor:pointer">' +
        '<span>' + escHtml(l.data) + ' — ' + escHtml(l.descricao) + '</span>' +
        '<span>R$ ' + l.valor.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</span></div>'
      ).join('');
    })
    .catch(() => {
      corpo.innerHTML = '<div style="padding:12px 0;color:var(--bad)">Erro ao carregar os lançamentos.</div>';
    });
}


// ---- detalhes de um lancamento (segundo nivel do modal) ----
let txAtual = null;

document.addEventListener('click', function (e) {
  const linha = e.target.closest('.linha-tx');
  if (linha) verTransacao(linha.dataset.tx);
});

function fecharModalTx() {
  document.getElementById('modalTxBg').classList.remove('show');
  document.getElementById('modalTxStatus').textContent = '';
  txAtual = null;
}

// o ESC (tratado no tabelas.js) fecha os dois modais de uma vez; aqui so limpamos
// o estado desta tela
window.aoFecharModal = function () { txAtual = null; };

function verTransacao(id) {
  txAtual = id;
  const corpo = document.getElementById('modalTxBody');
  corpo.innerHTML = '<div style="padding:12px 0;color:var(--ink-faint);font-size:13px">Carregando…</div>';
  document.getElementById('modalTxStatus').textContent = '';
  document.getElementById('modalTxBg').classList.add('show');

  fetch('/api/transacao/' + encodeURIComponent(id))
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        corpo.innerHTML = '<div style="padding:12px 0;color:var(--bad)">' +
                          escHtml(d.erro || 'Não foi possível carregar.') + '</div>';
        return;
      }
      const labels = {
        data: 'Data', descricao: 'Descrição', valor: 'Valor (R$)',
        valor_original: 'Valor original', status: 'Status', tipo: 'Tipo',
        origem: 'Origem', parcela: 'Parcela', conferida: 'Conferida',
        conferida_por: 'Conferida por', observacao: 'Observação',
        natureza_efetiva: 'Natureza no DRE'
      };
      corpo.innerHTML = Object.keys(labels).map(k =>
        '<div class="row"><span>' + labels[k] + '</span><span>' + escHtml(d[k]) + '</span></div>'
      ).join('');
      document.getElementById('modalTxCategoria').value = d.categoria || '';
    })
    .catch(() => {
      corpo.innerHTML = '<div style="padding:12px 0;color:var(--bad)">Erro ao carregar o lançamento.</div>';
    });
}

function salvarCategoriaTx() {
  if (!txAtual) return;
  const status = document.getElementById('modalTxStatus');
  status.style.color = 'var(--ink-faint)';
  status.textContent = 'Salvando…';
  // manda so a categoria: o endpoint preserva o resto do lancamento
  fetch('/api/transacao/' + encodeURIComponent(txAtual), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ categoria: document.getElementById('modalTxCategoria').value })
  })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { status.style.color = 'var(--bad)'; status.textContent = 'Falha ao salvar.'; return; }
      status.style.color = 'var(--good)';
      status.textContent = 'Categoria alterada. Recarregando a tela…';
      // a contagem por categoria muda, entao a tela precisa ser refeita
      setTimeout(() => window.location.reload(), 800);
    })
    .catch(() => { status.style.color = 'var(--bad)'; status.textContent = 'Falha ao salvar.'; });
}
