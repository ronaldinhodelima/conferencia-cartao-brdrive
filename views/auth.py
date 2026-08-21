"""Login e logout."""
import psycopg2
import psycopg2.extras
from flask import Blueprint, request, redirect, session, render_template

from core import (
    USERS,
    get_conn,
    permissoes_do_perfil,
    senha_confere,
)

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = (request.form.get("usuario", "") or "").strip()
        p = request.form.get("senha", "")
        conta = None
        try:
            conn = get_conn()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT usuario, nome, senha_hash, perfil, permissoes, ativo "
                "FROM cartao.usuario WHERE lower(usuario) = lower(%s);",
                (u,),
            )
            conta = cur.fetchone()
            if conta and conta["ativo"] and senha_confere(p, conta["senha_hash"]):
                cur.execute("UPDATE cartao.usuario SET ultimo_acesso = now() WHERE usuario = %s;",
                            (conta["usuario"],))
                conn.commit()
                session["user"] = conta["usuario"]
                session["nome"] = conta["nome"] or conta["usuario"]
                session["perfil"] = conta["perfil"]
                session["permissoes"] = list(conta["permissoes"] or [])
                cur.close()
                conn.close()
                return redirect("/")
            cur.close()
            conn.close()
        except Exception as e:
            print("Aviso: falha ao autenticar pelo banco:", e)

        # rede de seguranca: se a tabela ainda nao existe (primeiro boot), aceita a env
        if conta is None and u in USERS and USERS[u] == p:
            session["user"] = u
            session["nome"] = u
            session["perfil"] = "admin"
            session["permissoes"] = permissoes_do_perfil("admin")
            return redirect("/")

        error = "Usuário ou senha inválidos." if not (conta and not conta["ativo"]) \
            else "Este acesso está desativado. Fale com um administrador."
    return render_template("login.html", titulo="Entrar", erro=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect("/login")
