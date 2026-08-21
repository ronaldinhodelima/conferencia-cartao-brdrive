"""Relatorios, DRE e investimentos."""
import os
import csv
import functools
import hashlib
import html
import io
import json
import re
import secrets
import unicodedata
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, redirect, session, jsonify, render_template

from core import (
    BASE_CSS,
    CATEGORIAS_EXTRA,
    CATEGORIAS_OCULTAS,
    CATEGORIA_PT_DB,
    JOIN_NATUREZA,
    MESES_ABREV,
    NATUREZA_SQL,
    VAL_DESPESA,
    _montar_filtro_relatorio,
    aplicar_regras,
    aviso_pendencias_html,
    carregar_origens,
    cat_pt,
    cat_pt_puro,
    chave_alfa,
    chip_filter_html,
    esc,
    get_conn,
    levantar_pendencias,
    pode,
    requer,
    topbar_html,
)

bp = Blueprint("relatorios", __name__)


@bp.route("/dre")
@requer("relatorios")
def dre():
    ano = request.args.get("ano") or str(datetime.now().year)
    hoje = datetime.now()

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    base = f"FROM cartao.transacao t {JOIN_NATUREZA} WHERE COALESCE(t.duplicada, false) = false "

    cur.execute(
        f"SELECT t.categoria, SUM({VAL_DESPESA}) AS total {base} "
        f"AND to_char(t.data_transacao,'YYYY') = %s AND {NATUREZA_SQL} = 'despesa' "
        "AND t.categoria IS NOT NULL GROUP BY t.categoria;",
        (ano,),
    )
    anual_por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}

    # ---- DRE propriamente dito: receitas, despesas e resultado de cada mes do ano ----
    cur.execute(
        f"SELECT to_char(t.data_transacao,'YYYY-MM') AS mes, {NATUREZA_SQL} AS natureza, "
        f"SUM({VAL_DESPESA}) AS total {base} AND to_char(t.data_transacao,'YYYY') = %s "
        f"GROUP BY 1, 2 ORDER BY 1;",
        (ano,),
    )
    meses_dre = {}
    for r in cur.fetchall():
        m = meses_dre.setdefault(r["mes"], {"receita": 0.0, "despesa": 0.0, "investimento": 0.0, "bem": 0.0})
        v = float(r["total"] or 0)
        if r["natureza"] == "receita":
            m["receita"] += -v          # receita entra: VAL_DESPESA e negativo
        elif r["natureza"] == "despesa":
            m["despesa"] += v
        elif r["natureza"] in ("investimento", "bem"):
            m[r["natureza"]] += v       # positivo = dinheiro aplicado/investido no bem

    cur.execute(
        "SELECT g.id AS grupo_id, g.nome AS grupo_nome, "
        "s.id AS subgrupo_id, s.nome AS subgrupo_nome, "
        "cs.categoria "
        "FROM cartao.grupo_custo g "
        "JOIN cartao.subgrupo_custo s ON s.grupo_id = g.id "
        "LEFT JOIN cartao.categoria_subgrupo cs ON cs.subgrupo_id = s.id "
        "ORDER BY g.nome, s.nome;"
    )
    linhas_map = cur.fetchall()

    cur.execute("SELECT id, nome FROM cartao.dimensao ORDER BY ordem, nome;")
    dims = cur.fetchall()
    por_dimensao = []
    for d in dims:
        cur.execute(
            "SELECT COALESCE(dv.nome, '(nao definido)') AS nome, "
            f"SUM({VAL_DESPESA}) AS total "
            f"FROM cartao.transacao t {JOIN_NATUREZA} "
            "LEFT JOIN cartao.transacao_dimensao td ON td.transacao_id = t.transacao_id::text AND td.dimensao_id = %s "
            "LEFT JOIN cartao.dimensao_valor dv ON dv.id = td.valor_id "
            "WHERE to_char(t.data_transacao,'YYYY') = %s AND COALESCE(t.duplicada, false) = false "
            f"AND {NATUREZA_SQL} = 'despesa' AND t.categoria IS NOT NULL "
            "GROUP BY dv.nome ORDER BY total DESC;",
            (d["id"], ano),
        )
        por_dimensao.append({"nome": d["nome"], "linhas": cur.fetchall()})

    # o DRE e onde a ma classificacao vira numero errado - avisa aqui
    aviso_pend = aviso_pendencias_html(levantar_pendencias(cur)) if pode("cadastros") else ""

    cur.close()
    conn.close()

    grupos = {}
    categorias_mapeadas = set()
    for r in linhas_map:
        g = grupos.setdefault(r["grupo_id"], {
            "nome": r["grupo_nome"],
            "subgrupos": {},
        })
        s = g["subgrupos"].setdefault(r["subgrupo_id"], {
            "nome": r["subgrupo_nome"],
            "categorias": [],
        })
        if r["categoria"]:
            s["categorias"].append(r["categoria"])
            categorias_mapeadas.add(r["categoria"])

    nao_classificadas = sorted(set(anual_por_cat) - categorias_mapeadas)

    # ---- centro de custo: total do ano por grupo > subgrupo ----
    blocos_grupo = []
    for g in sorted(grupos.values(), key=lambda x: chave_alfa(x["nome"])):
        subs = []
        for s in sorted(g["subgrupos"].values(), key=lambda x: chave_alfa(x["nome"])):
            s_anual = sum(anual_por_cat.get(c, 0.0) for c in s["categorias"])
            subs.append({
                "nome": s["nome"],
                "total": s_anual,
                "categorias": ", ".join(cat_pt_puro(c) for c in s["categorias"]) or "sem categorias vinculadas",
            })
        blocos_grupo.append({
            "nome": g["nome"],
            "total": sum(s["total"] for s in subs),
            "subgrupos": subs,
        })

    blocos_dimensao = [{
        "nome": pd["nome"],
        "linhas": [{"nome": l["nome"], "total": float(l["total"] or 0)} for l in pd["linhas"]],
    } for pd in por_dimensao]

    # ---- DRE: um mes por linha, do mais recente para o mais antigo ----
    rec_ano = sum(m["receita"] for m in meses_dre.values())
    desp_ano = sum(m["despesa"] for m in meses_dre.values())
    inv_ano = sum(m["investimento"] + m["bem"] for m in meses_dre.values())

    linhas_dre = []
    for mes_key in sorted(meses_dre, reverse=True):
        m = meses_dre[mes_key]
        res = m["receita"] - m["despesa"]
        linhas_dre.append({
            "rotulo": f'{MESES_ABREV[int(mes_key[5:7]) - 1]}/{mes_key[2:4]}',
            "receita": m["receita"],
            "despesa": m["despesa"],
            "resultado": res,
            "margem": (res / m["receita"] * 100) if m["receita"] else 0,
            "investido": m["investimento"] + m["bem"],
        })

    return render_template(
        "dre.html",
        titulo="DRE / Centro de Custos",
        topbar=topbar_html("DRE / Centro de Custos", "dre"),
        aviso_pend=aviso_pend,
        ano=ano,
        anos=list(range(hoje.year - 3, hoje.year + 1)),
        rec_ano=rec_ano,
        desp_ano=desp_ano,
        resultado_ano=rec_ano - desp_ano,
        inv_ano=inv_ano,
        linhas_dre=linhas_dre,
        blocos_dimensao=blocos_dimensao,
        grupos=blocos_grupo,
        nao_classificadas=[
            {"nome": cat_pt_puro(c), "total": anual_por_cat[c]} for c in nao_classificadas
        ],
    )


@bp.route("/relatorios")
@requer("relatorios")
def relatorios():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    aplicar_regras(cur)
    conn.commit()

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cur.execute("SELECT id, dimensao_id, nome FROM cartao.dimensao_valor ORDER BY nome;")
    valores_por_dim = {}
    for v in cur.fetchall():
        valores_por_dim.setdefault(v["dimensao_id"], []).append(v)

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome ORDER BY prefixo;")
    cartoes_cadastrados = cur.fetchall()

    cur.execute("SELECT DISTINCT categoria FROM cartao.transacao WHERE categoria IS NOT NULL;")
    categorias_db = {r["categoria"] for r in cur.fetchall()}
    todas_categorias = sorted((categorias_db | set(CATEGORIAS_EXTRA) | set(CATEGORIA_PT_DB)) - CATEGORIAS_OCULTAS, key=lambda c: chave_alfa(cat_pt(c)))

    cur.execute("SELECT DISTINCT numero_cartao_final FROM cartao.transacao WHERE numero_cartao_final IS NOT NULL;")
    finais_usados = sorted({r["numero_cartao_final"] for r in cur.fetchall()})

    contas_by_id, origem_opcoes = carregar_origens(cur)

    cfg = _montar_filtro_relatorio(dimensoes)
    cur.close()
    conn.close()

    chip_filter = chip_filter_html

    dims_filtros_html = "".join(
        chip_filter(f"dim_{d['id']}", d["nome"],
                    [(v["id"], v["nome"]) for v in valores_por_dim.get(d["id"], [])],
                    cfg["dim_sel"].get(d["id"], []))
        for d in dimensoes if valores_por_dim.get(d["id"])
    )

    cartao_opcoes = [(c["final4"], f'{c["prefixo"]} - final {c["final4"]}') for c in cartoes_cadastrados]
    registrados = {c["final4"] for c in cartoes_cadastrados}
    cartao_opcoes += [(f, f"final {f}") for f in finais_usados if f not in registrados]

    agrupar_opcoes = [("categoria", "Categoria"), ("origem", "Origem"), ("cartao", "Cartão"), ("mes", "Período (mês)")]
    agrupar_opcoes += [(f"dim_{d['id']}", d["nome"]) for d in dimensoes]
    agrupar_opcoes_html = "".join(
        f'<option value="{val}" {"selected" if val == cfg["agrupar"] else ""}>{label}</option>'
        for val, label in agrupar_opcoes
    )

    return f"""
    <html><head><title>Relatórios · Pé de Meia</title>{BASE_CSS}
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
    </head>
    <body>
      {topbar_html('Relatórios', 'relatorios')}
      <div class="wrap">
        <div style="font-size:12px;color:#888;margin-bottom:10px">
          Pagamento de fatura, transferência entre contas próprias, aplicações e compra de bens
          <strong>não são despesa</strong> — só trocam a forma do dinheiro. Por isso ficam fora da visão de Despesas
          (veja a <a href="/categorias">classificação de naturezas</a>).
        </div>
        <div class="rel-filtros">
          <select name="visao" id="selVisao" class="chip-btn" style="border-radius:20px" onchange="aplicarFiltros()">
            <option value="despesa" {"selected" if cfg["visao"]=="despesa" else ""}>Despesas</option>
            <option value="receita" {"selected" if cfg["visao"]=="receita" else ""}>Receitas</option>
            <option value="investimento" {"selected" if cfg["visao"]=="investimento" else ""}>Investimentos e bens</option>
            <option value="tudo" {"selected" if cfg["visao"]=="tudo" else ""}>Tudo (fluxo de caixa)</option>
          </select>
          <select name="agrupar" id="selAgrupar" class="chip-btn" style="border-radius:20px" onchange="aplicarFiltros()">{agrupar_opcoes_html}</select>
          {chip_filter('origem', 'Origem', origem_opcoes, cfg["origens_sel"])}
          {chip_filter('categoria', 'Categoria', [(c, cat_pt(c)) for c in todas_categorias], cfg["categorias_sel"])}
          {chip_filter('cartao', 'Cartão', cartao_opcoes, cfg["cartoes_sel"])}
          {dims_filtros_html}
          <div class="rel-datewrap">
            <input type="date" name="data_ini" id="inputDataIni" value="{cfg["data_ini"]}" onchange="aplicarFiltros()">
            <span style="color:#bbb">–</span>
            <input type="date" name="data_fim" id="inputDataFim" value="{cfg["data_fim"]}" onchange="aplicarFiltros()">
          </div>
          <div class="rel-actions">
            <a href="/relatorios" class="chip-btn" style="text-decoration:none">Limpar tudo</a>
          </div>
        </div>
        <div class="chips-sel" id="chipsSel" style="margin:-6px 0 14px 0"></div>

        <div class="cards">
          <div class="card"><div class="label" id="labelTotal">Total no filtro</div><div class="val" id="totalGeral">-</div></div>
          <div class="card"><div class="label">Lançamentos</div><div class="val" id="qtdGeral">-</div></div>
        </div>

        <div class="chart-card">
          <h3 id="graficoTitulo">Gráfico</h3>
          <canvas id="chartGrupos" height="90"></canvas>
        </div>

        <div class="cat-breakdown">
          <h3>Totais agrupados <span style="font-weight:400;font-size:12px;color:#999">(clique em um grupo para ver os lançamentos)</span></h3>
          <div id="gruposCont"><div style="color:#888;padding:10px 0">Carregando...</div></div>
        </div>
      </div>

      <script>
        // ---- chip filters: dropdown com busca, checkbox toggle e navegacao por teclado ----
        function cfToggle(btn) {{
          const panel = btn.nextElementSibling;
          const abrir = !panel.classList.contains('show');
          document.querySelectorAll('.chip-panel.show').forEach(p => {{ if (p !== panel) p.classList.remove('show'); }});
          if (abrir) {{
            panel.classList.add('show');
            const search = panel.querySelector('.chip-search');
            if (search) {{ search.value = ''; cfFiltrar(search); search.focus(); }}
          }} else {{
            panel.classList.remove('show');
          }}
        }}
        document.addEventListener('click', function(e) {{
          if (!e.target.closest('.chipfilter')) {{
            document.querySelectorAll('.chip-panel.show').forEach(p => p.classList.remove('show'));
          }}
        }});
        function cfClear(e, btn) {{
          e.stopPropagation();
          const panel = btn.closest('.chipfilter').querySelector('.chip-panel');
          panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
          aplicarFiltros();
        }}
        function cfFiltrar(input) {{
          const panel = input.closest('.chip-panel');
          const q = input.value.toLowerCase();
          panel.querySelectorAll('.chip-opt').forEach(opt => {{
            opt.style.display = opt.textContent.toLowerCase().includes(q) ? 'flex' : 'none';
          }});
          panel.querySelectorAll('.chip-hover').forEach(o => o.classList.remove('chip-hover'));
        }}
        function cfKeydown(e, input) {{
          const panel = input.closest('.chip-panel');
          const visiveis = Array.from(panel.querySelectorAll('.chip-opt')).filter(o => o.style.display !== 'none');
          let idx = visiveis.findIndex(o => o.classList.contains('chip-hover'));
          if (e.key === 'ArrowDown') {{
            e.preventDefault();
            if (idx >= 0) visiveis[idx].classList.remove('chip-hover');
            idx = Math.min(idx + 1, visiveis.length - 1);
            if (visiveis[idx]) visiveis[idx].classList.add('chip-hover');
          }} else if (e.key === 'ArrowUp') {{
            e.preventDefault();
            if (idx >= 0) visiveis[idx].classList.remove('chip-hover');
            idx = Math.max(idx - 1, 0);
            if (visiveis[idx]) visiveis[idx].classList.add('chip-hover');
          }} else if (e.key === 'Enter') {{
            e.preventDefault();
            if (idx >= 0) {{
              const cb = visiveis[idx].querySelector('input[type=checkbox]');
              cb.checked = !cb.checked;
              aplicarFiltros();
            }}
          }} else if (e.key === 'Escape') {{
            panel.classList.remove('show');
          }}
        }}

        // ---- filtros aplicados em tempo real via AJAX (o dropdown nao fecha) ----
        function fmtMoeda(v) {{
          return 'R$ ' + Number(v).toLocaleString('pt-BR', {{minimumFractionDigits:2, maximumFractionDigits:2}});
        }}
        function atualizarChipLabels() {{
          document.querySelectorAll('.chipfilter').forEach(cf => {{
            const btn = cf.querySelector('.chip-btn');
            const label = btn.dataset.label;
            const n = cf.querySelectorAll('input[type=checkbox]:checked').length;
            btn.classList.toggle('ativo', n > 0);
            btn.innerHTML = '<span class="chip-plus">+</span> ' + label + (n ? ' (' + n + ')' : '') +
              (n ? '<span class="chip-clear" onclick="cfClear(event, this)">&times;</span>' : '');
          }});
          // chips pequenos mostrando tudo que esta selecionado
          const cont = document.getElementById('chipsSel');
          if (cont) {{
            const marcados = Array.from(document.querySelectorAll('.chipfilter input[type=checkbox]:checked'));
            cont.innerHTML = marcados.map(cb => {{
              const lbl = cb.closest('.chip-opt');
              const curto = lbl.dataset.curto || lbl.textContent.trim();
              const completo = lbl.getAttribute('data-tip') || curto;
              return '<span class="chip-tag" title="' + completo + '"><span>' + curto + '</span>' +
                     '<b onclick="desmarcarFiltro(\\'' + cb.name + '\\', \\'' + cb.value + '\\')">&times;</b></span>';
            }}).join('');
          }}
        }}
        function desmarcarFiltro(nome, valor) {{
          const cb = document.querySelector('.chipfilter input[name="' + nome + '"][value="' + valor + '"]');
          if (cb) {{ cb.checked = false; aplicarFiltros(); }}
        }}
        function coletarQuery() {{
          const params = new URLSearchParams();
          params.set('visao', document.getElementById('selVisao').value);
          params.set('agrupar', document.getElementById('selAgrupar').value);
          document.querySelectorAll('.chip-opt input[type=checkbox]:checked').forEach(cb => params.append(cb.name, cb.value));
          const di = document.getElementById('inputDataIni').value;
          const df = document.getElementById('inputDataFim').value;
          if (di) params.set('data_ini', di);
          if (df) params.set('data_fim', df);
          return params;
        }}
        function aplicarFiltros() {{
          atualizarChipLabels();
          const params = coletarQuery();
          history.replaceState(null, '', '/relatorios?' + params.toString());
          carregarDados(params);
        }}
        function carregarDados(params) {{
          fetch('/relatorios/dados?' + params.toString()).then(r => r.json()).then(renderResultado);
        }}
        const LABEL_VISAO = {{ despesa: 'Total de despesas', receita: 'Total de receitas',
                              investimento: 'Investido / adquirido', tudo: 'Fluxo de caixa (líquido)' }};
        function renderResultado(data) {{
          document.getElementById('totalGeral').textContent = fmtMoeda(data.total_geral);
          document.getElementById('labelTotal').textContent = LABEL_VISAO[data.visao] || 'Total no filtro';
          document.getElementById('qtdGeral').textContent = data.qtd_geral;
          const ehPeriodo = data.agrupar === 'mes';
          document.getElementById('graficoTitulo').textContent =
            ehPeriodo ? 'Evolução mês a mês' : 'Gráfico (' + data.agrupar_label + ')';
          renderGrupos(data.grupos, ehPeriodo);
          renderChart(data.grupos, ehPeriodo);
        }}

        // ---- lista de totais agrupados, clicavel para ver os lancamentos de cada grupo ----
        window.__grupos = [];
        function renderGrupos(grupos, ehPeriodo) {{
          // o grafico fica na ordem cronologica (linha do tempo); ja a lista abaixo
          // mostra o mes mais recente no topo, que e o que se quer olhar primeiro
          const lista = ehPeriodo ? grupos.slice().reverse() : grupos;
          window.__grupos = lista;
          const cont = document.getElementById('gruposCont');
          if (!lista.length) {{
            cont.innerHTML = '<div style="color:#888;padding:10px 0">Nenhum lancamento encontrado com esses filtros.</div>';
            return;
          }}
          // na linha do tempo a barra fica proporcional ao maior mes (fica legivel),
          // e mostramos a variacao em relacao ao mes anterior
          const maxTotal = Math.max.apply(null, lista.map(g => Math.abs(g.total)).concat([1]));
          cont.innerHTML = lista.map((g, i) => {{
            const larguraBarra = ehPeriodo ? (Math.abs(g.total) / maxTotal * 100) : Math.max(g.pct, 0);
            let direita = '<strong>' + fmtMoeda(g.total) + '</strong> <span style="color:#aaa">' + g.pct + '%</span>';
            // lista invertida: o mes anterior e o de baixo (i + 1)
            if (ehPeriodo && i < lista.length - 1) {{
              const ant = lista[i + 1].total;
              if (ant) {{
                const varPct = (g.total - ant) / Math.abs(ant) * 100;
                const cor = varPct > 0 ? 'var(--bad)' : 'var(--good)';
                const sinal = varPct > 0 ? '▲' : '▼';
                direita = '<strong>' + fmtMoeda(g.total) + '</strong> ' +
                          '<span style="color:' + cor + ';font-size:12px" title="variação em relação ao mês anterior">' +
                          sinal + ' ' + Math.abs(varPct).toFixed(1) + '%</span>';
              }}
            }}
            return '<div>' +
              '<div class="rel-grupo-row" style="cursor:pointer" onclick="toggleGrupoDetalhe(' + i + ')">' +
                '<div style="flex:1">' +
                  '<div style="display:flex;justify-content:space-between">' +
                    '<span>' + (g.selo || '') + g.nome + ' <span style="color:#aaa">(' + g.qtd + ')</span></span>' +
                    '<span>' + direita + '</span>' +
                  '</div>' +
                  '<div class="barra"><div style="width:' + larguraBarra + '%"></div></div>' +
                '</div>' +
              '</div>' +
              '<div class="rel-grupo-detalhe" id="grupoDetalhe' + i + '" style="display:none"></div>' +
            '</div>';
          }}).join('');
        }}
        function toggleGrupoDetalhe(i) {{
          const el = document.getElementById('grupoDetalhe' + i);
          const abrir = el.style.display === 'none';
          document.querySelectorAll('.rel-grupo-detalhe').forEach(d => {{ if (d !== el) d.style.display = 'none'; }});
          if (!abrir) {{ el.style.display = 'none'; return; }}
          el.style.display = 'block';
          if (el.dataset.loaded === '1') return;
          el.innerHTML = '<div style="padding:10px;color:#888;font-size:13px">Carregando...</div>';
          const g = window.__grupos[i];
          const params = coletarQuery();
          if (g.valor === null || g.valor === undefined) {{ params.set('valor_none', '1'); }}
          else {{ params.set('valor', g.valor); }}
          fetch('/relatorios/lancamentos?' + params.toString())
            .then(r => r.json())
            .then(data => {{
              el.dataset.loaded = '1';
              if (!data.lancamentos.length) {{
                el.innerHTML = '<div style="padding:10px;color:#888;font-size:13px">Nenhum lancamento.</div>';
                return;
              }}
              el.innerHTML = '<table class="rel-mini-table"><thead><tr><th>Data</th><th>Descrição</th><th>Origem</th><th>Categoria</th><th>Valor</th></tr></thead><tbody>' +
                data.lancamentos.map(l => (
                  '<tr><td>' + l.data + '</td><td>' + l.descricao + '</td>' +
                  '<td data-tip="' + (l.origem_completa || '') + '">' + l.origem + '</td><td>' + l.categoria + '</td>' +
                  '<td class="valor">' + fmtMoeda(l.valor) + '</td></tr>'
                )).join('') +
                '</tbody></table>' +
                (data.total >= 300 ? '<div style="padding:8px 10px;color:#999;font-size:12px">Mostrando os 300 lancamentos mais recentes deste grupo.</div>' : '');
            }});
        }}

        // ---- grafico dinamico conforme os filtros aplicados ----
        let chartInstance = null;
        let chartTipoAtual = null;
        function renderChart(grupos, ehPeriodo) {{
          if (!window.Chart) return;
          const labels = grupos.map(g => g.nome);
          const valores = grupos.map(g => g.total);
          // linha do tempo (mes a mes) fica melhor como linha; os demais, como barras
          const tipo = ehPeriodo ? 'line' : 'bar';
          const dataset = ehPeriodo
            ? {{ label: 'Total (R$)', data: valores, borderColor: '#2e6fd6', backgroundColor: 'rgba(46,111,214,.12)',
                 fill: true, tension: .3, pointRadius: 4, pointHoverRadius: 6, pointBackgroundColor: '#2e6fd6', borderWidth: 2 }}
            : {{ label: 'Total (R$)', data: valores, backgroundColor: '#2e6fd6', borderRadius: 4, maxBarThickness: 46 }};

          if (chartInstance && chartTipoAtual === tipo) {{
            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0] = dataset;
            chartInstance.update();
            return;
          }}
          if (chartInstance) chartInstance.destroy();
          chartTipoAtual = tipo;
          chartInstance = new Chart(document.getElementById('chartGrupos'), {{
            type: tipo,
            data: {{ labels: labels, datasets: [dataset] }},
            options: {{
              responsive: true,
              plugins: {{
                legend: {{ display: false }},
                tooltip: {{ callbacks: {{ label: c => fmtMoeda(c.parsed.y) }} }}
              }},
              scales: {{
                y: {{ beginAtZero: true, ticks: {{ callback: v => 'R$ ' + Number(v).toLocaleString('pt-BR') }} }}
              }}
            }}
          }});
        }}

        document.addEventListener('DOMContentLoaded', function() {{
          carregarDados(new URLSearchParams(window.location.search));
        }});
      </script>
    </body></html>
    """


@bp.route("/relatorios/dados")
@requer("relatorios")
def relatorios_dados():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cfg = _montar_filtro_relatorio(dimensoes)

    # agrupando por periodo o resultado e uma linha do tempo: ordena cronologicamente
    # (do mais antigo para o mais recente). Nos demais agrupamentos, maior valor primeiro.
    ordem = f"{cfg['group_expr']} ASC" if cfg["agrupar"] == "mes" else "total DESC"
    cur.execute(
        f"SELECT {cfg['group_expr']} AS grupo, COUNT(*) AS qtd, SUM({cfg['soma_expr']}) AS total "
        f"FROM cartao.transacao t {cfg['join_natureza']} {cfg['join_extra']} "
        f"WHERE {cfg['where_sql']} GROUP BY {cfg['group_expr']} ORDER BY {ordem};",
        cfg["params"],
    )
    grupos_raw = cur.fetchall()

    cur.execute(
        f"SELECT COUNT(*) AS qtd, SUM({cfg['soma_expr']}) AS total "
        f"FROM cartao.transacao t {cfg['join_natureza']} WHERE {cfg['where_sql']};",
        cfg["params"],
    )
    totalizador = cur.fetchone()
    total_geral = float(totalizador["total"] or 0)
    qtd_geral = totalizador["qtd"] or 0

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome;")
    nomes_cartao = {r["final4"]: esc(r["prefixo"]) for r in cur.fetchall()}

    contas_by_id, _ = carregar_origens(cur)

    cur.close()
    conn.close()

    def selo_grupo(g):
        """Selo do banco quando agrupado por origem (o grafico usa so o nome puro)."""
        if cfg["agrupar"] != "origem":
            return ""
        c = contas_by_id.get(str(g))
        return c["selo"] if c else ""

    def nome_grupo(g):
        if cfg["agrupar"] == "categoria":
            return cat_pt(g)
        if cfg["agrupar"] == "cartao":
            if not g:
                return "(sem cartao)"
            prefixo = nomes_cartao.get(g)
            return f"{prefixo} - final {g}" if prefixo else f"final {g}"
        if cfg["agrupar"] == "origem":
            c = contas_by_id.get(str(g))
            return c["label"] if c else "(sem origem)"
        if cfg["agrupar"] == "mes":
            # '2026-01' -> 'jan/26', mais legivel na linha do tempo
            try:
                ano, mes = str(g).split("-")
                return f"{MESES_ABREV[int(mes) - 1]}/{ano[2:]}"
            except (ValueError, IndexError):
                return g or "(sem periodo)"
        return g if g else "(nao definido)"

    grupos = []
    for g in grupos_raw:
        total_g = float(g["total"] or 0)
        pct = (total_g / total_geral * 100) if total_geral else 0
        grupos.append({
            "valor": g["grupo"],
            "nome": nome_grupo(g["grupo"]),
            "selo": selo_grupo(g["grupo"]),
            "qtd": g["qtd"],
            "total": round(total_g, 2),
            "pct": round(pct, 1),
        })

    agrupar_labels = {"categoria": "Categoria", "origem": "Origem", "cartao": "Cartão", "mes": "Período (mês)"}
    for d in dimensoes:
        agrupar_labels[f"dim_{d['id']}"] = d["nome"]

    return jsonify({
        "total_geral": round(total_geral, 2),
        "qtd_geral": qtd_geral,
        "visao": cfg["visao"],
        "agrupar": cfg["agrupar"],
        "agrupar_label": agrupar_labels.get(cfg["agrupar"], cfg["agrupar"]),
        "grupos": grupos,
    })


@bp.route("/relatorios/lancamentos")
@requer("relatorios")
def relatorios_lancamentos():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id, nome, obrigatoria FROM cartao.dimensao ORDER BY ordem, nome;")
    dimensoes = cur.fetchall()
    cfg = _montar_filtro_relatorio(dimensoes)

    where_sql = cfg["where_sql"]
    params = list(cfg["params"])
    if request.args.get("valor_none") == "1":
        where_sql += f" AND {cfg['group_expr']} IS NULL"
    elif request.args.get("valor") is not None:
        where_sql += f" AND {cfg['group_expr']} = %s"
        params.append(request.args.get("valor"))

    cur.execute(
        f"SELECT t.data_transacao, t.descricao, t.categoria, {cfg['soma_expr']} AS valor, "
        f"t.numero_cartao_final, t.account_id FROM cartao.transacao t {cfg['join_natureza']} {cfg['join_extra']} "
        f"WHERE {where_sql} ORDER BY t.data_transacao DESC LIMIT 300;",
        params,
    )
    rows = cur.fetchall()

    cur.execute("SELECT final4, prefixo FROM cartao.cartao_nome;")
    nomes_cartao = {r["final4"]: esc(r["prefixo"]) for r in cur.fetchall()}

    contas_by_id, _ = carregar_origens(cur)

    cur.close()
    conn.close()

    def nome_cartao_curto(final4):
        if not final4:
            return "-"
        prefixo = nomes_cartao.get(final4)
        return prefixo if prefixo else f"final {final4}"

    def origem_de(r):
        c = contas_by_id.get(str(r["account_id"]))
        if not c:
            return "-", "-"
        # se for cartao de credito e tiver apelido cadastrado, o apelido e mais informativo
        if c["tipo"] == "CREDIT" and r["numero_cartao_final"] and nomes_cartao.get(r["numero_cartao_final"]):
            return c["selo"] + nomes_cartao[r["numero_cartao_final"]], f'{c["label"]} - {nome_cartao_curto(r["numero_cartao_final"])}'
        return c["selo"] + c["label_curto"], c["label"]

    lancamentos = []
    for r in rows:
        curto, completo = origem_de(r)
        lancamentos.append({
            "data": (r["data_transacao"] - timedelta(hours=3)).strftime("%d/%m/%Y"),
            "descricao": r["descricao"],
            "origem": curto,
            "origem_completa": completo,
            "cartao": nome_cartao_curto(r["numero_cartao_final"]),
            "categoria": cat_pt(r["categoria"]),
            "valor": float(r["valor"] or 0),
        })
    return jsonify({"lancamentos": lancamentos, "total": len(lancamentos)})


@bp.route("/investimentos")
@requer("relatorios")
def investimentos_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            "SELECT investimento_id, nome, tipo, subtipo, instituicao, saldo, valor_bruto, "
            "valor_aplicado, impostos, taxa, tipo_taxa, data_posicao, data_vencimento, "
            "data_aplicacao, status "
            "FROM cartao.investimento ORDER BY saldo DESC NULLS LAST;"
        )
        posicoes = cur.fetchall()
    except Exception:
        conn.rollback()
        posicoes = None

    historico = []
    if posicoes is not None:
        # rendimento de cada mes = variacao do saldo total, descontando aportes e resgates
        cur.execute(
            "SELECT to_char(data, 'YYYY-MM') AS mes, MAX(data) AS ultima, "
            "SUM(saldo) AS saldo, SUM(valor_aplicado) AS aplicado "
            "FROM cartao.investimento_saldo "
            "WHERE data = (SELECT MAX(d2.data) FROM cartao.investimento_saldo d2 "
            "              WHERE to_char(d2.data,'YYYY-MM') = to_char(cartao.investimento_saldo.data,'YYYY-MM')) "
            "GROUP BY 1 ORDER BY 1;"
        )
        historico = cur.fetchall()

    cur.close()
    conn.close()

    contexto = {
        "titulo": "Investimentos",
        "topbar": topbar_html("Investimentos", "investimentos"),
    }
    if posicoes is None:
        return render_template("investimentos.html", sincronizado=False, **contexto)

    def _dt(v):
        return v.strftime("%d/%m/%Y") if v else "-"

    brutos = [p for p in posicoes if float(p["saldo"] or 0) > 0]
    ativos = []
    for p in brutos:
        aplicado = float(p["valor_aplicado"] or 0)
        bruto = float(p["valor_bruto"] or 0)
        rend = bruto - aplicado
        taxa = ""
        if p["taxa"] and float(p["taxa"]) > 0:
            taxa = f'{float(p["taxa"]):g}% {p["tipo_taxa"] or ""}'.strip()
        detalhe = p["subtipo"] or p["tipo"] or ""
        if taxa:
            detalhe = f"{detalhe} · {taxa}" if detalhe else taxa
        ativos.append({
            "nome": (p["nome"] or "-")[:46],
            "detalhe": detalhe,
            "aplicado": aplicado,
            "bruto": bruto,
            "rend": rend,
            "pct": (rend / aplicado * 100) if aplicado else 0,
            "impostos": float(p["impostos"] or 0),
            "saldo": float(p["saldo"] or 0),
            "vencimento": _dt(p["data_vencimento"]),
        })

    aplicado_total = sum(a["aplicado"] for a in ativos)
    bruto_total = sum(a["bruto"] for a in ativos)
    rendimento_bruto = bruto_total - aplicado_total

    # historico do mais recente para o mais antigo; a variacao de cada mes e a
    # diferenca para o mes seguinte (o de cima na tabela)
    hist = []
    anterior = None
    for h in reversed(historico):
        saldo = float(h["saldo"] or 0)
        mes = h["mes"]
        hist.append({
            "rotulo": f"{MESES_ABREV[int(mes[5:7]) - 1]}/{mes[2:4]}",
            "aplicado": float(h["aplicado"] or 0),
            "saldo": saldo,
            "variacao": None if anterior is None else anterior - saldo,
        })
        anterior = saldo

    return render_template(
        "investimentos.html",
        sincronizado=True,
        ativos=ativos,
        encerrados=len(posicoes) - len(ativos),
        saldo_total=sum(a["saldo"] for a in ativos),
        aplicado_total=aplicado_total,
        rendimento_bruto=rendimento_bruto,
        rend_pct=(rendimento_bruto / aplicado_total * 100) if aplicado_total else 0,
        ir_total=sum(a["impostos"] for a in ativos),
        historico=hist,
        **contexto,
    )


@bp.route("/api/categoria-lancamentos")
@requer("cadastros")
def api_categoria_lancamentos():
    """Lista os lancamentos de uma categoria - usado pelo botao 'protegida' em /categorias,
    pra mostrar o que esta impedindo a remocao sem precisar ir pra tela de Lancamentos."""
    categoria = request.args.get("categoria") or ""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT t.data_transacao, t.descricao, COALESCE(t.valor_brl, t.valor_original) AS valor "
        "FROM cartao.transacao t WHERE t.categoria = %s ORDER BY t.data_transacao DESC LIMIT 300;",
        (categoria,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([
        {
            "data": (r["data_transacao"] - timedelta(hours=3)).strftime("%d/%m/%Y") if r["data_transacao"] else "-",
            "descricao": r["descricao"] or "-",
            "valor": float(r["valor"]) if r["valor"] is not None else 0,
        }
        for r in rows
    ])
