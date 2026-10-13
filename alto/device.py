"""Descobre se a placa deste computador consegue rodar o Real-ESRGAN."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from PIL import Image

from alto.engine import WORK, _creation_flags, engine_paths

_lock = threading.Lock()
_state = {"ready": False, "ok": False, "name": "", "error": ""}


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def probe() -> None:
    with _lock:
        if _state["ready"]:
            return
    ok = False
    name = ""
    error = ""
    try:
        located = engine_paths()
        if not located:
            error = "Motor ausente."
        else:
            ok, name, error = _run_probe(located[0], located[1])
    except Exception as exc:
        error = str(exc)
        ok = False
    with _lock:
        _state["ready"] = True
        _state["ok"] = ok
        _state["name"] = name
        _state["error"] = error
    if ok:
        print(f"Placa pronta: {name or 'compatível'}.", flush=True)
    else:
        print("Este computador não amplia sozinho.", flush=True)


def _run_probe(exe: Path, models: Path) -> tuple[bool, str, str]:
    folder = WORK / "gpu-probe"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "entrada.png"
    dest = folder / "saida.png"
    if dest.exists():
        dest.unlink()
    Image.new("RGB", (16, 16), (120, 80, 40)).save(source, "PNG")
    command = [
        str(exe),
        "-i",
        str(source),
        "-o",
        str(dest),
        "-n",
        "realesrgan-x4plus",
        "-s",
        "4",
        "-m",
        str(models),
        "-f",
        "png",
        "-t",
        "32",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(exe.parent),
            capture_output=True,
            timeout=120,
            creationflags=_creation_flags(),
        )
    except subprocess.TimeoutExpired:
        return False, "", "A placa não respondeu."
    text = (completed.stdout or b"").decode("utf-8", "replace") + (completed.stderr or b"").decode("utf-8", "replace")
    name = ""
    for line in text.splitlines():
        if "queueC" not in line or "[" not in line:
            continue
        start = line.find("[")
        end = line.find("]", start)
        if start >= 0 and end > start:
            inner = line[start + 1 : end].strip()
            parts = inner.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                name = parts[1].strip()
    if completed.returncode != 0 or not dest.exists():
        return False, name, "A placa não ampliou a imagem de prova."
    with Image.open(dest) as image:
        if image.size != (64, 64):
            return False, name, "A placa não ampliou a imagem de prova."
    return True, name, ""
