"""Healthcheck e sincronizacao com o Pluggy."""
from flask import Blueprint, jsonify

from core import (
    disparar_sincronizacao,
    get_ultima_sincronizacao,
    login_required,
    requer,
)

bp = Blueprint("sistema", __name__)


@bp.route("/api/sync-status")
@login_required
def api_sync_status():
    return jsonify(get_ultima_sincronizacao())


@bp.route("/api/sync-agora", methods=["POST"])
@requer("sincronizar")
def api_sync_agora():
    ok, erro = disparar_sincronizacao()
    if not ok:
        return jsonify({"executado_em": None, "status": "erro", "mensagem_erro": erro}), 502
    return jsonify(get_ultima_sincronizacao())


@bp.route("/health")
def health():
    return jsonify({"status": "ok"})
