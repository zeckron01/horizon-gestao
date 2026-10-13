"""Ponto de entrada na Vercel: expõe a aplicação ASGI. Todas as rotas são encaminhadas para cá (ver vercel.json)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.app import app  # noqa: E402,F401
