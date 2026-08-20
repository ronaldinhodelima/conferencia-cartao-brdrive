import os
import time
import threading
import traceback
import urllib.parse
from datetime import datetime, timezone

import requests
import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request

app = Flask(__name__)

STATE = {"migration": "pending", "error": None}
SYNC_STATE = {"last_run": None, "status": "never_run", "detail": None}

PLUGGY_CLIENT_ID = os.environ.get("PLUGGY_CLIENT_ID")
PLUGGY_CLIENT_SECRET = os.environ.get("PLUGGY_CLIENT_SECRET")
PLUGGY_ITEM_ID = os.environ.get("PLUGGY_ITEM_ID")
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", str(24 * 60 * 60)))
# Enquanto essa env nao for configurada no Coolify, /sync fica aberto (comportamento
# atual). Depois de configurada (aqui e no app principal, com o MESMO valor), o
# endpoint passa a exigir o header X-Sync-Secret - evita que qualquer um que descubra
# a URL interna do worker fique disparando sync a vontade.
SYNC_SECRET = os.environ.get("SYNC_SECRET")

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS cartao;

CREATE TABLE IF NOT EXISTS cartao.pluggy_item (
    item_id         UUID PRIMARY KEY,
    connector_name  TEXT NOT NULL,
    status          TEXT,
    execution_status TEXT,
    last_updated_at TIMESTAMPTZ,
    next_auto_sync_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cartao.conta (
    account_id          UUID PRIMARY KEY,
    item_id             UUID NOT NULL REFERENCES cartao.pluggy_item(item_id),
    nome                TEXT,
    tipo                TEXT,
    subtipo             TEXT,
    bandeira            TEXT,
    nivel               TEXT,
    numero_final        TEXT,
    limite_credito       NUMERIC(14,2),
    limite_disponivel    NUMERIC(14,2),
    saldo_usado          NUMERIC(14,2),
    pagamento_minimo     NUMERIC(14,2),
    vencimento_fatura    DATE,
    atualizado_em        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cartao.transacao (
    transacao_id        UUID PRIMARY KEY,
    account_id           UUID NOT NULL REFERENCES cartao.conta(account_id),
    descricao             TEXT,
    descricao_bruta       TEXT,
    valor_original        NUMERIC(14,2),
    moeda_original         TEXT,
    valor_brl              NUMERIC(14,2),
    data_transacao          TIMESTAMPTZ,
    categoria               TEXT,
    categoria_id             TEXT,
    status                    TEXT,
    tipo                      TEXT,
    numero_cartao_final        TEXT,
    mcc                        INTEGER,
    parcela_atual               INTEGER,
    parcela_total                INTEGER,
    criado_em                     TIMESTAMPTZ,
    atualizado_em                  TIMESTAMPTZ,
    sincronizado_em                TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transacao_data ON cartao.transacao (data_transacao);
CREATE INDEX IF NOT EXISTS idx_transacao_categoria ON cartao.transacao (categoria);
CREATE INDEX IF NOT EXISTS idx_transacao_account ON cartao.transacao (account_id);

CREATE TABLE IF NOT EXISTS cartao.sync_log (
    id              SERIAL PRIMARY KEY,
    item_id         UUID REFERENCES cartao.pluggy_item(item_id),
    executado_em    TIMESTAMPTZ DEFAULT now(),
    status          TEXT,
    transacoes_novas INTEGER,
    transacoes_atualizadas INTEGER,
    mensagem_erro    TEXT
);

-- Posicao atual de cada investimento (renda fixa, previdencia, fundos...).
-- O saldo e patrimonio, nao entra no DRE; o que entra no resultado e o rendimento.
CREATE TABLE IF NOT EXISTS cartao.investimento (
    investimento_id  TEXT PRIMARY KEY,
    item_id          UUID,
    nome             TEXT,
    tipo             TEXT,
    subtipo          TEXT,
    instituicao      TEXT,
    moeda            TEXT,
    saldo            NUMERIC(16,2),   -- liquido (ja descontado o IR)
    valor_bruto      NUMERIC(16,2),
    valor_aplicado   NUMERIC(16,2),
    impostos         NUMERIC(16,2),
    taxa             NUMERIC(12,4),
    tipo_taxa        TEXT,
    data_posicao     TIMESTAMPTZ,
    data_vencimento  TIMESTAMPTZ,
    data_aplicacao   TIMESTAMPTZ,
    status           TEXT,
    atualizado_em    TIMESTAMPTZ DEFAULT now()
);

-- Retrato diario do saldo: a API do Pluggy so devolve a posicao de hoje, entao
-- guardamos o historico para conseguir calcular o rendimento de cada mes.
CREATE TABLE IF NOT EXISTS cartao.investimento_saldo (
    investimento_id  TEXT NOT NULL,
    data             DATE NOT NULL,
    saldo            NUMERIC(16,2),
    valor_bruto      NUMERIC(16,2),
    valor_aplicado   NUMERIC(16,2),
    impostos         NUMERIC(16,2),
    PRIMARY KEY (investimento_id, data)
);
"""


def get_conn():
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ["PGPASSWORD"],
    )


def run_migration():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(SCHEMA_SQL)
        conn.commit()
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='cartao' ORDER BY table_name;"
        )
        tables = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
        STATE["migration"] = "ok"
        STATE["tables"] = tables
    except Exception as e:
        STATE["migration"] = "error"
        STATE["error"] = str(e)
        STATE["trace"] = traceback.format_exc()


# ---------------- Pluggy client ----------------

def pluggy_auth():
    r = requests.post(
        "https://api.pluggy.ai/auth",
        json={"clientId": PLUGGY_CLIENT_ID, "clientSecret": PLUGGY_CLIENT_SECRET},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["apiKey"]


def pluggy_get(path, api_key, params=None):
    r = requests.get(
        f"https://api.pluggy.ai{path}",
        headers={"X-API-KEY": api_key},
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_all_transactions(api_key, account_id):
    """Busca TODAS as transacoes da conta, seguindo a paginacao do Pluggy.

    A API v2 devolve no maximo 500 por pagina e o link da proxima pagina vem no
    campo `next` (uma querystring pronta, ex: "?accountId=...&after=..."). Antes
    liamos `cursor.after`, que nao existe na resposta - por isso so vinham os 500
    mais recentes. Tratamos os dois formatos por seguranca.
    """
    results = []
    params = {"accountId": account_id}
    paginas = 0
    while paginas < 500:
        data = pluggy_get("/v2/transactions", api_key, params)
        page_results = data.get("results", [])
        results.extend(page_results)
        paginas += 1
        if not page_results:
            break

        proxima = data.get("next")
        if proxima:
            qs = urllib.parse.parse_qs(str(proxima).lstrip("?"))
            params = {k: v[0] for k, v in qs.items() if v}
            if params.get("accountId") and params.get("after"):
                continue

        after = (data.get("cursor") or {}).get("after")
        if after:
            params = {"accountId": account_id, "after": after}
            continue
        break
    return results


def upsert_item(cur, item):
    cur.execute(
        """
        INSERT INTO cartao.pluggy_item (item_id, connector_name, status, execution_status, last_updated_at, next_auto_sync_at)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (item_id) DO UPDATE SET
            status = EXCLUDED.status,
            execution_status = EXCLUDED.execution_status,
            last_updated_at = EXCLUDED.last_updated_at,
            next_auto_sync_at = EXCLUDED.next_auto_sync_at,
            updated_at = now();
        """,
        (
            item["id"],
            item["connector"]["name"],
            item.get("status"),
            item.get("executionStatus"),
            item.get("lastUpdatedAt"),
            item.get("nextAutoSyncAt"),
        ),
    )


def upsert_account(cur, item_id, acc):
    credit = acc.get("creditData") or {}
    cur.execute(
        """
        INSERT INTO cartao.conta (
            account_id, item_id, nome, tipo, subtipo, bandeira, nivel, numero_final,
            limite_credito, limite_disponivel, saldo_usado, pagamento_minimo, vencimento_fatura
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (account_id) DO UPDATE SET
            nome = EXCLUDED.nome,
            limite_credito = EXCLUDED.limite_credito,
            limite_disponivel = EXCLUDED.limite_disponivel,
            saldo_usado = EXCLUDED.saldo_usado,
            pagamento_minimo = EXCLUDED.pagamento_minimo,
            vencimento_fatura = EXCLUDED.vencimento_fatura,
            atualizado_em = now();
        """,
        (
            acc["id"],
            item_id,
            acc.get("name"),
            acc.get("type"),
            acc.get("subtype"),
            credit.get("brand"),
            credit.get("level"),
            acc.get("number"),
            credit.get("creditLimit"),
            credit.get("availableCreditLimit"),
            acc.get("balance"),
            credit.get("minimumPayment"),
            credit.get("balanceDueDate"),
        ),
    )


def upsert_transaction(cur, tx):
    meta = tx.get("creditCardMetadata") or {}
    cur.execute(
        """
        INSERT INTO cartao.transacao (
            transacao_id, account_id, descricao, descricao_bruta, valor_original, moeda_original,
            valor_brl, data_transacao, categoria, categoria_id, status, tipo,
            numero_cartao_final, mcc, criado_em, atualizado_em, sincronizado_em
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (transacao_id) DO UPDATE SET
            status = EXCLUDED.status,
            valor_brl = EXCLUDED.valor_brl,
            categoria = EXCLUDED.categoria,
            atualizado_em = EXCLUDED.atualizado_em,
            sincronizado_em = now()
        RETURNING (xmax = 0) AS inserted;
        """,
        (
            tx["id"],
            tx["accountId"],
            tx.get("description"),
            tx.get("descriptionRaw"),
            tx.get("amount"),
            tx.get("currencyCode"),
            tx.get("amountInAccountCurrency"),
            tx.get("date"),
            tx.get("category"),
            tx.get("categoryId"),
            tx.get("status"),
            tx.get("type"),
            meta.get("cardNumber"),
            meta.get("payeeMCC"),
            tx.get("createdAt"),
            tx.get("updatedAt"),
        ),
    )
    return cur.fetchone()[0]


CONTA_MANUAL_ITEM = "00000000-0000-0000-0000-000000000001"


def listar_itens(cur):
    """Todas as conexoes Pluggy a sincronizar.

    A API nao permite listar os itens da aplicacao com a chave de API (GET /items
    devolve 401), entao a lista vem de duas fontes que se complementam:
      1. a env PLUGGY_ITEM_ID (aceita varios ids separados por virgula) - usada
         para cadastrar uma conexao nova;
      2. as conexoes ja gravadas em cartao.pluggy_item - assim, depois do primeiro
         sync, a conexao continua sendo atualizada mesmo que saia da env.
    Basta conectar um banco novo no Pluggy e informar o id uma vez.
    """
    itens = {i.strip() for i in (PLUGGY_ITEM_ID or "").replace(";", ",").split(",") if i.strip()}
    try:
        cur.execute("SELECT item_id FROM cartao.pluggy_item WHERE item_id <> %s;", (CONTA_MANUAL_ITEM,))
        itens |= {str(r[0]) for r in cur.fetchall()}
    except Exception:
        pass
    return sorted(itens)


def upsert_investimento(cur, item_id, inv):
    """Grava a posicao atual de um investimento e um retrato diario do saldo.

    O retrato permite calcular depois quanto rendeu em cada mes - a API do Pluggy
    devolve so a posicao de hoje, sem historico.
    """
    cur.execute(
        """
        INSERT INTO cartao.investimento (
            investimento_id, item_id, nome, tipo, subtipo, instituicao, moeda,
            saldo, valor_bruto, valor_aplicado, impostos, taxa, tipo_taxa,
            data_posicao, data_vencimento, data_aplicacao, status, atualizado_em
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (investimento_id) DO UPDATE SET
            saldo = EXCLUDED.saldo,
            valor_bruto = EXCLUDED.valor_bruto,
            valor_aplicado = EXCLUDED.valor_aplicado,
            impostos = EXCLUDED.impostos,
            data_posicao = EXCLUDED.data_posicao,
            status = EXCLUDED.status,
            atualizado_em = now();
        """,
        (
            inv["id"], item_id, inv.get("name"), inv.get("type"), inv.get("subtype"),
            inv.get("institution"), inv.get("currencyCode"),
            inv.get("balance"), inv.get("amount"), inv.get("amountOriginal"),
            inv.get("taxes"), inv.get("rate"), inv.get("rateType"),
            inv.get("date"), inv.get("dueDate"), inv.get("issueDate"), inv.get("status"),
        ),
    )
    cur.execute(
        """
        INSERT INTO cartao.investimento_saldo (investimento_id, data, saldo, valor_bruto, valor_aplicado, impostos)
        VALUES (%s, CURRENT_DATE, %s, %s, %s, %s)
        ON CONFLICT (investimento_id, data) DO UPDATE SET
            saldo = EXCLUDED.saldo,
            valor_bruto = EXCLUDED.valor_bruto,
            valor_aplicado = EXCLUDED.valor_aplicado,
            impostos = EXCLUDED.impostos;
        """,
        (inv["id"], inv.get("balance"), inv.get("amount"), inv.get("amountOriginal"), inv.get("taxes")),
    )


def run_sync():
    if not (PLUGGY_CLIENT_ID and PLUGGY_CLIENT_SECRET):
        SYNC_STATE.update(
            {
                "status": "error",
                "detail": "Faltam envs PLUGGY_CLIENT_ID / PLUGGY_CLIENT_SECRET",
                "last_run": datetime.now(timezone.utc).isoformat(),
            }
        )
        return SYNC_STATE

    novas = 0
    atualizadas = 0
    investimentos = 0
    conexoes = []
    itens_ok = []          # so as conexoes que realmente gravaram dados
    erro = None
    try:
        api_key = pluggy_auth()
        conn = get_conn()
        cur = conn.cursor()

        item_ids = listar_itens(cur)
        if not item_ids:
            raise RuntimeError("Nenhuma conexao Pluggy configurada (env PLUGGY_ITEM_ID vazia)")

        for item_id in item_ids:
            # uma conexao com problema (ex: autorizacao expirada no banco) nao pode
            # derrubar a sincronizacao das outras
            try:
                item = pluggy_get(f"/items/{item_id}", api_key)
            except Exception as e:
                conexoes.append({"item": item_id, "status": "erro", "detalhe": str(e)[:120]})
                continue

            nome_conexao = (item.get("connector") or {}).get("name") or item_id
            exec_status = item.get("executionStatus")
            erro_item = (item.get("error") or {}).get("message")

            accounts = pluggy_get("/accounts", api_key, {"itemId": item_id}).get("results", [])
            contas = [a for a in accounts if a.get("type") in ("CREDIT", "BANK")]

            if not contas:
                # conexao criada mas nunca concluida (USER_INPUT_TIMEOUT, LOGIN_ERROR...)
                conexoes.append({
                    "item": item_id, "conexao": nome_conexao, "status": item.get("status"),
                    "execucao": exec_status, "contas": 0,
                    "detalhe": erro_item or "nenhuma conta - refaca a conexao no Pluggy",
                })
                continue

            upsert_item(cur, item)
            for acc in contas:
                upsert_account(cur, item["id"], acc)
            conn.commit()

            novas_item = atualizadas_item = 0
            for acc in contas:
                for tx in fetch_all_transactions(api_key, acc["id"]):
                    if upsert_transaction(cur, tx):
                        novas_item += 1
                    else:
                        atualizadas_item += 1
                conn.commit()
            novas += novas_item
            atualizadas += atualizadas_item

            # investimentos da conexao (renda fixa, previdencia, fundos...)
            inv_item = 0
            try:
                for inv in pluggy_get("/investments", api_key, {"itemId": item_id}).get("results", []):
                    upsert_investimento(cur, item["id"], inv)
                    inv_item += 1
                conn.commit()
            except Exception as e:
                print(f"Aviso: falha ao sincronizar investimentos de {item_id}: {e}")
            investimentos += inv_item

            conexoes.append({
                "conexao": nome_conexao, "status": item.get("status"), "contas": len(contas),
                "transacoes_novas": novas_item, "investimentos": inv_item,
            })
            itens_ok.append(item["id"])

        # o log referencia pluggy_item, entao so registra com uma conexao que gravou dados
        if itens_ok:
            cur.execute(
                """
                INSERT INTO cartao.sync_log (item_id, status, transacoes_novas, transacoes_atualizadas, mensagem_erro)
                VALUES (%s,%s,%s,%s,%s);
                """,
                (itens_ok[0], "SUCCESS", novas, atualizadas, None),
            )
            conn.commit()
        cur.close()
        conn.close()

        SYNC_STATE.update(
            {
                "status": "ok",
                "last_run": datetime.now(timezone.utc).isoformat(),
                "detail": {
                    "transacoes_novas": novas,
                    "transacoes_atualizadas": atualizadas,
                    "investimentos": investimentos,
                    "conexoes": conexoes,
                },
            }
        )
    except Exception as e:
        erro = f"{e}"
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO cartao.sync_log (item_id, status, transacoes_novas, transacoes_atualizadas, mensagem_erro)
                VALUES (%s,%s,%s,%s,%s);
                """,
                (itens_ok[0] if itens_ok else None, "ERROR", novas, atualizadas, erro),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass
        SYNC_STATE.update(
            {
                "status": "error",
                "last_run": datetime.now(timezone.utc).isoformat(),
                "detail": erro,
                "trace": traceback.format_exc(),
            }
        )
    return SYNC_STATE


def scheduler_loop():
    # roda uma sincronização assim que sobe, depois a cada SYNC_INTERVAL_SECONDS
    while True:
        run_sync()
        time.sleep(SYNC_INTERVAL_SECONDS)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "migration": STATE["migration"]})


@app.route("/")
def root():
    return jsonify({"migration": STATE, "sync": SYNC_STATE})


@app.route("/sync", methods=["GET", "POST"])
def sync_now():
    if SYNC_SECRET and request.headers.get("X-Sync-Secret") != SYNC_SECRET:
        return jsonify({"ok": False, "erro": "nao autorizado"}), 401
    result = run_sync()
    return jsonify(result)


if __name__ == "__main__":
    run_migration()
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8000)
