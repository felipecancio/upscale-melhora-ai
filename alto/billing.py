"""Estrutura da mensalidade. Sem chave, nada é cobrado."""

from __future__ import annotations

import os


def checkout_view() -> dict:
    token = os.environ.get("MERCADOPAGO_ACCESS_TOKEN", "").strip()
    if token:
        return {
            "ready": False,
            "message": "A chave de cobrança está no servidor, mas o checkout ainda não foi ligado. Nada foi cobrado.",
        }
    return {
        "ready": False,
        "message": (
            "A cobrança recorrente entra quando a chave do Mercado Pago estiver no servidor. "
            "Durante a avaliação, o aumento continua liberado."
        ),
    }
