"""Usuarios e permissoes."""
import psycopg2
import psycopg2.extras
from flask import Blueprint, request, session, render_template

from core import (
    PERFIS,
    PERMISSOES,
    get_conn,
    hash_senha,
    permissoes_do_perfil,
    requer,
    topbar_html,
)

bp = Blueprint("usuarios", __name__)


@bp.route("/usuarios", methods=["GET", "POST"])
@requer("usuarios")
def usuarios_view():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aviso = erro = None

    def total_admins_ativos(excluindo=None):
        cur.execute(
            "SELECT COUNT(*) AS n FROM cartao.usuario "
            "WHERE ativo = true AND 'usuarios' = ANY(permissoes) AND usuario <> %s;",
            (excluindo or "",),
        )
        return cur.fetchone()["n"]

    if request.method == "POST":
        acao = request.form.get("acao")
        alvo = (request.form.get("usuario") or "").strip()
        try:
            if acao == "criar":
                login = (request.form.get("novo_usuario") or "").strip().lower()
                senha = request.form.get("nova_senha") or ""
                perfil = request.form.get("perfil") or "leitura"
                if not login or not senha:
                    erro = "Informe usuário e senha."
                elif len(senha) < 6:
                    erro = "A senha precisa ter pelo menos 6 caracteres."
                else:
                    cur.execute(
                        "INSERT INTO cartao.usuario (usuario, nome, senha_hash, perfil, permissoes) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (usuario) DO NOTHING RETURNING usuario;",
                        (login, (request.form.get("novo_nome") or login.capitalize()).strip(),
                         hash_senha(senha), perfil, permissoes_do_perfil(perfil)),
                    )
                    aviso = f'Usuário "{login}" criado.' if cur.fetchone() else "Já existe um usuário com esse login."

            elif acao == "permissoes":
                perfil = request.form.get("perfil") or "leitura"
                marcadas = [p for p in request.form.getlist("perm") if p in PERMISSOES]
                # nao deixa tirar o proprio acesso de gerenciar usuarios
                if alvo == session.get("user") and "usuarios" not in marcadas:
                    erro = "Você não pode remover a sua própria permissão de gerenciar usuários."
                elif "usuarios" not in marcadas and total_admins_ativos(alvo) == 0:
                    erro = "É preciso ter ao menos um administrador com acesso a usuários."
                else:
                    cur.execute(
                        "UPDATE cartao.usuario SET perfil = %s, permissoes = %s, nome = %s WHERE usuario = %s;",
                        (perfil, marcadas, (request.form.get("nome") or "").strip() or alvo, alvo),
                    )
                    aviso = f'Permissões de "{alvo}" atualizadas.'

            elif acao == "senha":
                nova = request.form.get("senha") or ""
                if len(nova) < 6:
                    erro = "A senha precisa ter pelo menos 6 caracteres."
                else:
                    cur.execute("UPDATE cartao.usuario SET senha_hash = %s WHERE usuario = %s;",
                                (hash_senha(nova), alvo))
                    aviso = f'Senha de "{alvo}" alterada.'

            elif acao == "ativar":
                ativo = request.form.get("ativo") == "1"
                if not ativo and alvo == session.get("user"):
                    erro = "Você não pode desativar o seu próprio acesso."
                elif not ativo and total_admins_ativos(alvo) == 0:
                    erro = "É preciso manter ao menos um administrador ativo."
                else:
                    cur.execute("UPDATE cartao.usuario SET ativo = %s WHERE usuario = %s;", (ativo, alvo))
                    aviso = f'Acesso de "{alvo}" ' + ("reativado." if ativo else "desativado.")

            elif acao == "excluir":
                if alvo == session.get("user"):
                    erro = "Você não pode excluir o seu próprio usuário."
                elif total_admins_ativos(alvo) == 0:
                    erro = "É preciso manter ao menos um administrador."
                else:
                    cur.execute("DELETE FROM cartao.usuario WHERE usuario = %s;", (alvo,))
                    aviso = f'Usuário "{alvo}" excluído.'
            conn.commit()
        except Exception as e:
            conn.rollback()
            erro = str(e)

    cur.execute(
        "SELECT usuario, nome, perfil, permissoes, ativo, criado_em, ultimo_acesso "
        "FROM cartao.usuario ORDER BY ativo DESC, usuario;"
    )
    contas = cur.fetchall()
    cur.close()
    conn.close()

    def _dt(v):
        return v.strftime("%d/%m/%Y %H:%M") if v else "nunca"

    eu = session.get("user")
    usuarios = [{
        "usuario": c["usuario"],
        "nome": c["nome"],
        "perfil": c["perfil"],
        "perfil_rotulo": PERFIS.get(c["perfil"], ("Personalizado",))[0],
        "permissoes": list(c["permissoes"] or []),
        "ativo": c["ativo"],
        "sou_eu": c["usuario"] == eu,
        "ultimo_acesso_fmt": _dt(c["ultimo_acesso"]),
    } for c in contas]

    return render_template(
        "usuarios.html",
        titulo="Usuários e permissões",
        topbar=topbar_html("Usuários e permissões", "usuarios"),
        aviso=aviso,
        erro=erro,
        contas=usuarios,
        perfis={k: {"rotulo": v[0]} for k, v in PERFIS.items()},
        perfis_permissoes={k: v[1] for k, v in PERFIS.items()},
        permissoes={k: {"titulo": t, "descricao": d} for k, (t, d) in PERMISSOES.items()},
    )
