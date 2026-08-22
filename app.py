"""Ponto de entrada: cria o app Flask, registra os blueprints e sobe o servidor.

Quem faz o trabalho e core.py (constantes e helpers) e views/*.py (as rotas).
"""
import os

from flask import Flask

from core import _fmt_moeda, _barra_html, rotulo_valor_dimensao
from views import auth, sistema, lancamentos, relatorios, cadastros, usuarios

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "troque-isto-em-producao")

# cookie de sessao so trafega por HTTPS (o Traefik do Coolify ja forca https) e
# nunca e enviado em navegacao cross-site - reduz roubo de sessao via rede ou CSRF.
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# ---- filtros e globais usados pelos templates ----
@app.template_filter("moeda")
def _filtro_moeda(v):
    return _fmt_moeda(float(v or 0))


@app.template_filter("num")
def _filtro_num(v):
    """Numero sem casas decimais desnecessarias; vazio quando nao ha valor.
    Usado em campo de formulario, onde None tem que virar string vazia."""
    if v is None or v == "":
        return ""
    return f"{float(v):g}"


@app.context_processor
def _globais_template():
    # disponiveis em qualquer template, sem cada view precisar passar
    return {"barra": _barra_html, "rotulo_dim": rotulo_valor_dimensao}


# a ordem nao importa: nenhum blueprint disputa o mesmo caminho
for modulo in (auth, sistema, lancamentos, relatorios, cadastros, usuarios):
    app.register_blueprint(modulo.bp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
