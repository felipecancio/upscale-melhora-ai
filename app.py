"""Porta pública do Alto. A ampliação continua no computador."""

from fastapi import FastAPI

app = FastAPI(title="Alto")


@app.get("/")
def home() -> dict:
    return {
        "ok": True,
        "name": "Alto",
        "message": "A conta pública fica aqui. A ampliação continua no computador.",
    }
