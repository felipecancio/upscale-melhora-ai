"""Cliente do serviço de ampliação. A interface continua neste computador."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from alto.engine import Cancelled, UpscaleError


def authenticate(base: str, path: str, payload: dict) -> tuple[str, dict]:
    code, headers, raw = _request(
        "POST",
        _url(base, path),
        json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json"},
        "Conecte à internet para entrar na conta.",
        timeout=30,
    )
    data = _json(raw)
    if code >= 400:
        raise UpscaleError(str(data.get("error") or "Não consegui entrar na conta."))
    token = _token_from(_header(headers, "Set-Cookie"))
    if not token:
        raise UpscaleError("A conta não devolveu uma sessão.")
    return token, data


def whoami(base: str, token: str) -> dict | None:
    code, _headers, raw = _request(
        "GET",
        _url(base, "/api/me"),
        None,
        {"Cookie": f"alto_session={token}"},
        "Conecte à internet para entrar na conta.",
        timeout=20,
    )
    if code == 401:
        return None
    data = _json(raw)
    if code >= 400:
        raise UpscaleError(str(data.get("error") or "Conecte à internet para entrar na conta."))
    if not data.get("user"):
        return None
    return {
        "user": data["user"],
        "subscription": data.get("subscription"),
        "billing": data.get("billing"),
    }


def logout(base: str, token: str) -> None:
    _request(
        "POST",
        _url(base, "/api/logout"),
        b"{}",
        {"Content-Type": "application/json", "Cookie": f"alto_session={token}"},
        "Conecte à internet para entrar na conta.",
        timeout=20,
    )


def checkout(base: str, token: str) -> dict:
    code, _headers, raw = _request(
        "POST",
        _url(base, "/api/billing/checkout"),
        b"{}",
        {"Content-Type": "application/json", "Cookie": f"alto_session={token}"},
        "Conecte à internet para ver o pagamento.",
        timeout=30,
    )
    data = _json(raw)
    if code >= 400:
        raise UpscaleError(str(data.get("error") or "Não consegui consultar o pagamento."))
    return data


def upscale(
    base: str,
    token: str,
    source: Path,
    dest: Path,
    options: dict,
    on_progress,
    cancel,
    on_log,
) -> None:
    offline = "Conecte à internet para ampliar esta imagem."
    body = source.read_bytes()
    header_options = base64.b64encode(json.dumps(options, ensure_ascii=False).encode("utf-8")).decode("ascii")
    code, _headers, raw = _request(
        "POST",
        _url(base, "/api/service/jobs"),
        body,
        {
            "Content-Type": "application/octet-stream",
            "Cookie": f"alto_session={token}",
            "X-Filename": quote(source.name),
            "X-Options": header_options,
        },
        offline,
        timeout=300,
    )
    data = _json(raw)
    if code >= 400:
        raise UpscaleError(str(data.get("error") or "O serviço não aceitou esta imagem."))
    job_id = str(data.get("id") or "")
    if not job_id:
        raise UpscaleError("O serviço não devolveu o trabalho.")
    seen = 0
    on_progress(2, "Ampliando no serviço")
    while True:
        if cancel.is_set():
            _stop(base, token, job_id)
            raise Cancelled("Interrompido.")
        code, _headers, raw = _request(
            "GET",
            _url(base, "/api/service/jobs?id=" + quote(job_id)),
            None,
            {"Cookie": f"alto_session={token}"},
            offline,
            timeout=30,
        )
        status = _json(raw)
        if code >= 400:
            raise UpscaleError(str(status.get("error") or "O serviço não respondeu."))
        logs = status.get("logs") or []
        for line in logs[seen:]:
            on_log(str(line))
        seen = len(logs)
        progress = float(status.get("progress") or 0)
        on_progress(progress, str(status.get("message") or "Ampliando no serviço"))
        state = status.get("status")
        if state == "done":
            _download(base, token, job_id, dest, offline)
            on_progress(100, "Concluído")
            return
        if state == "stopped":
            raise Cancelled("Interrompido.")
        if state == "error":
            raise UpscaleError(str(status.get("error") or "O serviço não conseguiu ampliar esta imagem."))
        for _ in range(4):
            if cancel.is_set():
                break
            time.sleep(0.12)


def _download(base: str, token: str, job_id: str, dest: Path, offline: str) -> None:
    code, _headers, raw = _request(
        "GET",
        _url(base, "/api/service/file?id=" + quote(job_id)),
        None,
        {"Cookie": f"alto_session={token}"},
        offline,
        timeout=300,
    )
    if code >= 400:
        data = _json(raw)
        raise UpscaleError(str(data.get("error") or "O serviço não devolveu o arquivo."))
    if not raw:
        raise UpscaleError("O serviço devolveu um arquivo vazio.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)


def _stop(base: str, token: str, job_id: str) -> None:
    try:
        _request(
            "POST",
            _url(base, "/api/service/stop"),
            json.dumps({"id": job_id}).encode("utf-8"),
            {"Content-Type": "application/json", "Cookie": f"alto_session={token}"},
            "Conecte à internet para ampliar esta imagem.",
            timeout=15,
        )
    except UpscaleError:
        return


def _url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _header(headers: dict, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _token_from(header: str | None) -> str:
    if not header:
        return ""
    pair = header.split(";", 1)[0].strip()
    name, _, value = pair.partition("=")
    if name != "alto_session":
        return ""
    return value.strip()


def _json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _request(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict,
    offline: str,
    timeout: int,
) -> tuple[int, dict, bytes]:
    sent = {"User-Agent": "Alto", "Accept": "application/json"}
    sent.update(headers)
    request = urllib.request.Request(url, data=body, headers=sent, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()) if error.headers else {}, error.read()
    except urllib.error.URLError:
        raise UpscaleError(offline) from None
