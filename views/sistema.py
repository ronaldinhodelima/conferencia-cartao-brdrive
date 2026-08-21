"""Healthcheck e sincronizacao com o Pluggy."""
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
