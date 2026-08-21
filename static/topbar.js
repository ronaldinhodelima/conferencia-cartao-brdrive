// ---- tooltip proprio: o balao nativo do navegador so aparece depois de ~1s ----
(function() {
  let el = null, timer = null;
  function criar() {
    if (!el) {
      el = document.createElement('div');
      el.id = 'tooltip';
      document.body.appendChild(el);
    }
    return el;
  }
  function posicionar(e, t) {
    const m = 14;
    let x = e.clientX + m, y = e.clientY + m;
    const r = t.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - m;
    if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - m;
    t.style.left = Math.max(6, x) + 'px';
    t.style.top = Math.max(6, y) + 'px';
  }
  document.addEventListener('mouseover', function(e) {
    const alvo = e.target.closest('[data-tip]');
    if (!alvo) return;
    const texto = alvo.getAttribute('data-tip');
    if (!texto) return;
    clearTimeout(timer);
    timer = setTimeout(function() {
      const t = criar();
      t.textContent = texto;
      t.classList.add('show');
      posicionar(e, t);
    }, 120);
  });
  document.addEventListener('mousemove', function(e) {
    if (el && el.classList.contains('show')) posicionar(e, el);
  });
  document.addEventListener('mouseout', function(e) {
    if (!e.target.closest('[data-tip]')) return;
    clearTimeout(timer);
    if (el) el.classList.remove('show');
  });
  document.addEventListener('click', function() {
    clearTimeout(timer);
    if (el) el.classList.remove('show');
  });
})();

// menu do topo: abre/fecha no clique e fecha ao clicar fora ou apertar Esc
function menuToggle(e, btn) {
  e.stopPropagation();
  const drop = btn.closest('.dropdown');
  const abrir = !drop.classList.contains('aberto');
  document.querySelectorAll('.dropdown.aberto').forEach(d => d.classList.remove('aberto'));
  if (abrir) drop.classList.add('aberto');
  btn.blur();
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('.dropdown')) {
    document.querySelectorAll('.dropdown.aberto').forEach(d => d.classList.remove('aberto'));
  }
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') document.querySelectorAll('.dropdown.aberto').forEach(d => d.classList.remove('aberto'));
});

function syncEhSucesso(status) {
  if (!status) return false;
  const s = String(status).toLowerCase();
  return s === 'ok' || s === 'success' || s === 'sucesso';
}
function syncFormatarTexto(d) {
  if (!d.executado_em) return 'Sem sincronização registrada';
  let txt = 'Atualizado em ' + d.executado_em;
  if (d.status && !syncEhSucesso(d.status)) txt += ' (erro)';
  return txt;
}
async function syncCarregarStatus() {
  // o widget so existe para quem tem permissao de sincronizar
  if (!document.getElementById('syncTexto')) return;
  try {
    const r = await fetch('/api/sync-status');
    const d = await r.json();
    document.getElementById('syncTexto').textContent = syncFormatarTexto(d);
    const dot = document.getElementById('syncDot');
    dot.className = 'sync-dot ' + (syncEhSucesso(d.status) ? 'ok' : (d.status ? 'erro' : ''));
  } catch (e) {
    document.getElementById('syncTexto').textContent = 'Status indisponível';
  }
}
async function dispararSync() {
  const btn = document.getElementById('syncBtn');
  const dot = document.getElementById('syncDot');
  btn.disabled = true;
  btn.textContent = 'Atualizando...';
  dot.className = 'sync-dot rodando';
  document.getElementById('syncTexto').textContent = 'Sincronizando com o Pluggy...';
  try {
    const r = await fetch('/api/sync-agora', { method: 'POST' });
    const d = await r.json();
    document.getElementById('syncTexto').textContent = syncFormatarTexto(d);
    dot.className = 'sync-dot ' + (syncEhSucesso(d.status) ? 'ok' : 'erro');
  } catch (e) {
    document.getElementById('syncTexto').textContent = 'Falha ao atualizar';
    dot.className = 'sync-dot erro';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Atualizar agora';
  }
}
syncCarregarStatus();
