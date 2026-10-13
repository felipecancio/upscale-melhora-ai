"""Servidor local do Alto. A conta libera o uso; a placa ou o serviço faz o aumento."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import secrets
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from alto.accounts import AuthError
from alto.billing import checkout_view
from alto.device import probe as probe_device
from alto.device import snapshot as device_snapshot
from alto.engine import (
    BUILTIN_IDS,
    BUILTIN_MODELS,
    DEFAULT_OUTPUT,
    MAX_UPLOAD,
    MODELS_DIR,
    ROOT,
    WORK,
    Cancelled,
    UpscaleError,
    engine_paths,
    ensure_engine,
    inspect_image,
    list_custom_models,
    list_images,
    make_plan,
    make_result_preview,
    output_filename,
    process_image,
    safe_stem,
    uses_fixed_4x,
)
from alto import accounts, remote

STATIC = ROOT / "static"
CONFIG_PATH = ROOT / "alto-config.json"
HOST = "127.0.0.1"
PORT = 47321
bound_port = PORT

dialog_queue: queue.Queue = queue.Queue()


class Session:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.settings = load_settings()
        self.input_path: Path | None = None
        self.input_name = ""
        self.width = 0
        self.height = 0
        self.folder: Path | None = None
        self.folder_files: list[Path] = []
        self.preview_original = WORK / "preview-original.jpg"
        self.preview_result = WORK / "preview-result.jpg"
        self.result_ready = False
        self.outputs: list[str] = []
        self.status = "idle"
        self.progress = 0.0
        self.message = ""
        self.error = ""
        self.logs: list[str] = []
        self.cancel = threading.Event()
        self.control = {"proc": None}
        self.gpu_name = ""
        self.engine_ready = engine_paths() is not None
        self.engine_error = ""
        self.download = 0.0
        self.delegating = False

    def log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line)
            del self.logs[:-200]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "progress": round(self.progress, 1),
                "message": self.message,
                "error": self.error,
                "logs": self.logs[-40:],
                "resultReady": self.result_ready,
                "outputs": list(self.outputs),
                "gpu": self.gpu_name or device_snapshot().get("name") or "",
                "route": route_name(),
                "ready": self.engine_ready,
                "width": self.width,
                "height": self.height,
                "name": self.input_name,
                "fileCount": len(self.folder_files),
                "folder": str(self.folder) if self.folder else "",
            }


def load_settings() -> dict:
    defaults = {
        "outputDir": str(DEFAULT_OUTPUT),
        "model": "realesrgan-x4plus",
        "tile": 0,
        "gpu": "",
        "dpi": 300,
        "format": "png",
        "quality": 95,
        "tta": False,
        "customModels": str(MODELS_DIR),
        "paper": "A3",
        "customCm": 50,
        "scale": 4,
        "sizeMode": "scale",
        "serviceUrl": "",
    }
    if not CONFIG_PATH.exists():
        return defaults
    try:
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    defaults.update({key: stored[key] for key in defaults if key in stored})
    return defaults


def save_settings() -> None:
    CONFIG_PATH.write_text(
        json.dumps(session.settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


session = Session()
service_jobs: dict[str, dict] = {}
service_lock = threading.Lock()


def service_url() -> str:
    env = os.environ.get("ALTO_SERVICE_URL", "").strip()
    raw = env or str(session.settings.get("serviceUrl") or "").strip()
    return raw.rstrip("/")


def service_is_self() -> bool:
    raw = service_url()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else "http://" + raw)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return port == bound_port


def uses_remote_accounts() -> bool:
    return bool(service_url()) and not service_is_self()


def route_name() -> str:
    info = device_snapshot()
    if not info["ready"]:
        return "checking"
    if os.environ.get("ALTO_IGNORE_GPU") == "1":
        return "service" if service_url() else "unavailable"
    if info["ok"]:
        return "local"
    if service_url():
        return "service"
    return "unavailable"


def wait_route(timeout: float = 180) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        name = route_name()
        if name != "checking":
            return name
        time.sleep(0.2)
    return route_name()


def unavailable_message() -> str:
    return "Este computador não amplia sozinho. O serviço de ampliação ainda não está ligado."


def cookie_header(token: str, clear: bool = False) -> str:
    if clear or not token:
        return "alto_session=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"
    return f"alto_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=2592000"


def session_token(header: str | None) -> str:
    if not header:
        return ""
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "alto_session":
            return unquote(value.strip())
    return ""


def account_view(record: dict | None) -> dict:
    body = accounts.public_identity(record)
    body["route"] = route_name()
    body["gpu"] = session.gpu_name or device_snapshot().get("name") or ""
    if record and record.get("billing"):
        body["billing"] = record["billing"]
    else:
        body["billing"] = checkout_view()
    return body


def output_dir() -> Path:
    raw = str(session.settings.get("outputDir") or "").strip()
    path = Path(raw) if raw else DEFAULT_OUTPUT
    path.mkdir(parents=True, exist_ok=True)
    return path


def custom_model_dir() -> Path | None:
    raw = str(session.settings.get("customModels") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def model_catalog() -> list[dict]:
    catalog = [dict(item, custom=False) for item in BUILTIN_MODELS]
    catalog.extend(list_custom_models(custom_model_dir()))
    return catalog


def resolve_model(model_id: str) -> tuple[str, Path, bool]:
    located = engine_paths()
    if not located:
        raise UpscaleError("O motor de upscale ainda não está pronto.")
    _exe, models = located
    if model_id in BUILTIN_IDS:
        return model_id, models, False
    if Path(model_id).name != model_id:
        raise UpscaleError("Modelo inválido.")
    folder = custom_model_dir()
    if folder and (folder / f"{model_id}.param").exists() and (folder / f"{model_id}.bin").exists():
        return model_id, folder, True
    raise UpscaleError("Modelo não encontrado.")


def remember_image(path: Path, display_name: str | None = None) -> dict:
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff"}:
        raise UpscaleError("Use PNG, JPG, WEBP, BMP ou TIFF.")
    WORK.mkdir(parents=True, exist_ok=True)
    shown = display_name or path.name
    width, height = inspect_image(path, session.preview_original)
    with session.lock:
        session.input_path = path
        session.input_name = shown
        session.width = width
        session.height = height
        session.result_ready = False
        session.outputs = []
        if session.status != "running":
            session.status = "idle"
            session.progress = 0
            session.message = ""
            session.error = ""
    return {"name": shown, "width": width, "height": height, "preview": "/media/original.jpg"}


def build_plan(payload: dict):
    mode = payload.get("mode") or "image"
    if mode == "folder":
        if not session.folder_files:
            raise UpscaleError("Escolha uma pasta com imagens.")
        source = session.folder_files[0]
        width, height = session.width, session.height
        if width < 2:
            width, height = inspect_image(source, session.preview_original)
    else:
        if session.input_path is None:
            raise UpscaleError("Escolha uma imagem.")
        width, height = session.width, session.height
    model_id = str(payload.get("model") or session.settings["model"])
    _name, _folder, custom = resolve_model(model_id)
    return make_plan(
        width,
        height,
        size_mode=str(payload.get("sizeMode") or "scale"),
        scale=int(payload.get("scale") or 4),
        paper=str(payload.get("paper") or "A3"),
        custom_cm=float(payload.get("customCm") or 50),
        dpi=int(payload.get("dpi") or 300),
        tile=int(payload.get("tile") or 0),
        scale4_only=uses_fixed_4x(model_id, custom),
        folder=mode == "folder",
    )


def unique_destination(directory: Path, filename: str) -> Path:
    dest = directory / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    number = 2
    while True:
        candidate = directory / f"{stem}-{number}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def service_options(payload: dict) -> dict:
    return {
        "model": str(payload.get("model") or "realesrgan-x4plus"),
        "sizeMode": str(payload.get("sizeMode") or "scale"),
        "scale": int(payload.get("scale") or 4),
        "paper": str(payload.get("paper") or "A3"),
        "customCm": float(payload.get("customCm") or 50),
        "dpi": int(payload.get("dpi") or 300),
        "format": str(payload.get("format") or "png"),
        "quality": int(payload.get("quality") or 95),
        "tile": int(payload.get("tile") or 0),
        "tta": bool(payload.get("tta")),
    }


def render_source(
    source: Path,
    dest: Path,
    options: dict,
    *,
    width: int,
    height: int,
    on_progress,
    on_log,
    on_gpu,
    cancel: threading.Event,
    control: dict,
    work: Path | None = None,
) -> str:
    model_id, models, custom = resolve_model(str(options.get("model") or "realesrgan-x4plus"))
    located = engine_paths()
    if not located:
        raise UpscaleError("O motor de upscale ainda não está pronto.")
    exe, _builtin = located
    fmt = str(options.get("format") or "png")
    if fmt not in {"png", "jpg", "webp"}:
        raise UpscaleError("Formato de saída desconhecido.")
    quality = min(100, max(70, int(options.get("quality") or 95)))
    gpu = str(options.get("gpu") or "").strip()
    if gpu and not gpu.isdigit():
        raise UpscaleError("A GPU precisa ser um número, como 0 ou 1.")
    plan = make_plan(
        width,
        height,
        size_mode=str(options.get("sizeMode") or "scale"),
        scale=int(options.get("scale") or 4),
        paper=str(options.get("paper") or "A3"),
        custom_cm=float(options.get("customCm") or 50),
        dpi=int(options.get("dpi") or 300),
        tile=int(options.get("tile") or 0),
        scale4_only=uses_fixed_4x(model_id, custom),
        folder=False,
    )
    process_image(
        source,
        dest,
        plan,
        model=model_id,
        models=models,
        exe=exe,
        gpu=gpu,
        tta=bool(options.get("tta")),
        quality=quality,
        fmt=fmt,
        custom_model=custom,
        control=control,
        cancel=cancel,
        on_progress=on_progress,
        on_log=on_log,
        on_gpu=on_gpu,
        work=work,
    )
    return plan.label


def place_message(route: str, percent: float, message: str) -> str:
    if percent >= 100 or message == "Concluído":
        return "Concluído"
    if route == "service":
        return "Ampliando no serviço"
    if message.startswith("Passagem"):
        return f"Ampliando neste computador · {message}"
    return "Ampliando neste computador"


def service_busy() -> bool:
    with service_lock:
        return any(job["status"] == "running" for job in service_jobs.values())


def run_job(payload: dict, token: str = "") -> None:
    try:
        mode = payload.get("mode") or "image"
        if mode == "folder":
            sources = list(session.folder_files)
        else:
            if session.input_path is None:
                raise UpscaleError("Escolha uma imagem.")
            sources = [session.input_path]
        if not sources:
            raise UpscaleError("Nenhuma imagem para ampliar.")

        route = wait_route()
        if route == "checking":
            raise UpscaleError("Ainda estou verificando este computador. Tente de novo em alguns segundos.")
        if route == "unavailable":
            raise UpscaleError(unavailable_message())
        if route == "local" and service_busy():
            raise UpscaleError("O serviço está ampliando outra imagem. Tente de novo em instantes.")

        model_id = str(payload.get("model") or "realesrgan-x4plus")
        if route == "service":
            if model_id not in BUILTIN_IDS:
                raise UpscaleError("Esse modelo extra só amplia neste computador.")
            if not service_url():
                raise UpscaleError(unavailable_message())
            if not token:
                raise UpscaleError("Entre na sua conta para ampliar.")
        else:
            resolve_model(model_id)

        fmt = str(payload.get("format") or "png")
        if fmt not in {"png", "jpg", "webp"}:
            raise UpscaleError("Formato de saída desconhecido.")
        destination_dir = output_dir()
        outputs: list[str] = []
        session.log("Ampliando no serviço." if route == "service" else "Ampliando neste computador.")

        for index, source in enumerate(sources):
            if session.cancel.is_set():
                raise Cancelled("Interrompido.")
            if mode == "image":
                width, height = session.width, session.height
            else:
                width, height = inspect_image(source, session.preview_original)
            visible_name = session.input_name if mode == "image" and session.input_name else source.name
            label = _preview_label(width, height, payload, model_id, route == "service")
            filename = output_filename(safe_stem(visible_name), label, fmt)
            dest = unique_destination(destination_dir, filename)
            span = 100 / len(sources)

            def on_progress(percent: float, message: str, index=index, source=source) -> None:
                prefix = f"{index + 1} de {len(sources)} · {source.name} · " if len(sources) > 1 else ""
                with session.lock:
                    session.progress = index * span + percent / 100 * span
                    session.message = prefix + place_message(route, percent, message)

            def on_log(line: str) -> None:
                session.log(line)

            def on_gpu(name: str) -> None:
                with session.lock:
                    session.gpu_name = name

            if route == "service":
                with session.lock:
                    session.delegating = True
                try:
                    remote.upscale(
                        service_url(),
                        token,
                        source,
                        dest,
                        service_options(payload),
                        on_progress,
                        session.cancel,
                        on_log,
                    )
                finally:
                    with session.lock:
                        session.delegating = False
            else:
                render_source(
                    source,
                    dest,
                    {**payload, "gpu": str(payload.get("gpu") or "").strip()},
                    width=width,
                    height=height,
                    on_progress=on_progress,
                    on_log=on_log,
                    on_gpu=on_gpu,
                    cancel=session.cancel,
                    control=session.control,
                )
            outputs.append(str(dest))
            make_result_preview(dest, session.preview_result)
            with session.lock:
                session.result_ready = True
                session.outputs = list(outputs)

        with session.lock:
            session.status = "done"
            session.progress = 100
            session.message = "Concluído" if len(outputs) == 1 else f"{len(outputs)} imagens salvas"
            session.error = ""
    except Cancelled:
        with session.lock:
            session.status = "stopped"
            session.message = "Interrompido"
            session.error = ""
    except Exception as error:
        traceback.print_exc()
        with session.lock:
            session.status = "error"
            session.error = str(error) if isinstance(error, UpscaleError) else "O aumento falhou. O detalhe está em Ajustes."
            session.message = ""
            if not isinstance(error, UpscaleError):
                session.logs.append(str(error))


def _preview_label(width: int, height: int, payload: dict, model_id: str, remote_job: bool) -> str:
    custom = False if remote_job or model_id in BUILTIN_IDS else True
    if not remote_job and model_id not in BUILTIN_IDS:
        _name, _folder, custom = resolve_model(model_id)
    plan = make_plan(
        width,
        height,
        size_mode=str(payload.get("sizeMode") or "scale"),
        scale=int(payload.get("scale") or 4),
        paper=str(payload.get("paper") or "A3"),
        custom_cm=float(payload.get("customCm") or 50),
        dpi=int(payload.get("dpi") or 300),
        tile=int(payload.get("tile") or 0),
        scale4_only=uses_fixed_4x(model_id, custom),
        folder=False,
    )
    return plan.label


def public_service_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "progress": round(float(job["progress"]), 1),
        "message": job["message"],
        "error": job["error"],
        "logs": list(job["logs"][-40:]),
    }


def start_service_job(record: dict, raw: bytes, filename: str, options: dict) -> str:
    with service_lock:
        if any(job["status"] == "running" for job in service_jobs.values()):
            raise UpscaleError("O serviço já está ampliando outra imagem.")
        with session.lock:
            local_busy = session.status == "running" and not session.delegating
        if local_busy:
            raise UpscaleError("O serviço já está ampliando outra imagem.")
        job_id = secrets.token_urlsafe(16)
        job = {
            "id": job_id,
            "user_id": record["id"],
            "status": "running",
            "progress": 1,
            "message": "Ampliando no serviço",
            "error": "",
            "logs": [],
            "dest": "",
            "cancel": threading.Event(),
            "control": {"proc": None},
        }
        service_jobs[job_id] = job
    threading.Thread(
        target=_run_service_job,
        args=(job, raw, filename, options),
        name=f"alto-servico-{job_id[:6]}",
        daemon=True,
    ).start()
    return job_id


def _run_service_job(job: dict, raw: bytes, filename: str, options: dict) -> None:
    job_dir = WORK / "servico" / job["id"]
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff"}:
            suffix = ".png"
        source = job_dir / f"origem{suffix}"
        source.write_bytes(raw)
        width, height = inspect_image(source, job_dir / "preview.jpg")
        fmt = str(options.get("format") or "png")
        if fmt not in {"png", "jpg", "webp"}:
            raise UpscaleError("Formato de saída desconhecido.")
        dest = job_dir / f"resultado.{fmt if fmt != 'jpg' else 'jpg'}"

        def on_progress(percent: float, message: str) -> None:
            with service_lock:
                job["progress"] = percent
                job["message"] = message

        def on_log(line: str) -> None:
            with service_lock:
                job["logs"].append(line)
                del job["logs"][:-80]

        render_source(
            source,
            dest,
            {**options, "gpu": ""},
            width=width,
            height=height,
            on_progress=on_progress,
            on_log=on_log,
            on_gpu=lambda _name: None,
            cancel=job["cancel"],
            control=job["control"],
            work=job_dir,
        )
        with service_lock:
            job["dest"] = str(dest)
            job["progress"] = 100
            job["message"] = "Concluído"
            job["status"] = "done"
    except Cancelled:
        with service_lock:
            job["status"] = "stopped"
            job["message"] = "Interrompido"
            job["error"] = ""
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as error:
        traceback.print_exc()
        with service_lock:
            job["status"] = "error"
            job["error"] = str(error) if isinstance(error, UpscaleError) else "O aumento falhou."
            job["message"] = ""
            if not isinstance(error, UpscaleError):
                job["logs"].append(str(error))
        shutil.rmtree(job_dir, ignore_errors=True)


def service_job_for(user_id: int, job_id: str) -> dict | None:
    with service_lock:
        job = service_jobs.get(job_id)
        if not job or job["user_id"] != user_id:
            return None
        return job


def ask_dialog(kind: str, initial: str) -> str:
    box: queue.Queue = queue.Queue(1)

    def opener(root) -> None:
        try:
            from tkinter import filedialog

            root.lift()
            root.attributes("-topmost", True)
            root.update_idletasks()
            if kind == "file":
                path = filedialog.askopenfilename(
                    parent=root,
                    title="Escolher imagem",
                    initialdir=initial,
                    filetypes=[
                        ("Imagens", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
                        ("Todos os arquivos", "*.*"),
                    ],
                )
            else:
                path = filedialog.askdirectory(parent=root, title="Escolher pasta", initialdir=initial, mustexist=True)
            root.attributes("-topmost", False)
            box.put(path or "")
        except Exception as error:
            box.put(error)

    dialog_queue.put(opener)
    result = box.get(timeout=180)
    if isinstance(result, Exception):
        raise UpscaleError("Não consegui abrir a janela de arquivos. Arraste a imagem para a tela.")
    return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/":
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            target = (STATIC / path.removeprefix("/static/")).resolve()
            if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
                self._json(404, {"error": "Arquivo não encontrado."})
                return
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._file(target, mime)
            return
        if path == "/favicon.svg":
            self._file(STATIC / "favicon.svg", "image/svg+xml")
            return
        if path == "/api/bootstrap":
            self._json(200, self._bootstrap())
            return
        if path == "/api/me":
            self._me()
            return
        if path == "/api/status":
            self._json(200, session.snapshot())
            return
        if path == "/api/service/jobs":
            self._service_status(parsed.query)
            return
        if path == "/api/service/file":
            self._service_file(parsed.query)
            return
        if path == "/media/original.jpg" and session.preview_original.exists():
            self._file(session.preview_original, "image/jpeg")
            return
        if path == "/media/result.jpg" and session.preview_result.exists() and session.result_ready:
            self._file(session.preview_result, "image/jpeg")
            return
        self._json(404, {"error": "Não encontrado."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/upload":
                self._upload()
                return
            if path == "/api/service/jobs":
                self._service_create()
                return
            payload = self._read_json()
            if path == "/api/login":
                self._login(payload, register=False)
                return
            if path == "/api/register":
                self._login(payload, register=True)
                return
            if path == "/api/logout":
                self._logout()
                return
            if path == "/api/billing/checkout":
                self._checkout()
                return
            if path == "/api/service/stop":
                self._service_stop(str(payload.get("id") or ""))
                return
            if path == "/api/plan":
                plan = build_plan(payload)
                self._json(200, plan.to_json())
                return
            if path == "/api/settings":
                self._update_settings(payload)
                self._json(200, {"ok": True, "models": model_catalog()})
                return
            if path == "/api/pick-image":
                initial = str(Path.home() / "Pictures")
                chosen = ask_dialog("file", initial)
                if not chosen:
                    self._json(200, {"cancelled": True})
                    return
                self._json(200, remember_image(Path(chosen)))
                return
            if path == "/api/pick-folder":
                initial = str(session.folder or Path.home())
                chosen = ask_dialog("folder", initial)
                if not chosen:
                    self._json(200, {"cancelled": True})
                    return
                self._remember_folder(Path(chosen))
                self._json(200, session.snapshot())
                return
            if path == "/api/pick-output":
                chosen = ask_dialog("folder", str(output_dir()))
                if not chosen:
                    self._json(200, {"cancelled": True})
                    return
                session.settings["outputDir"] = chosen
                save_settings()
                self._json(200, {"outputDir": chosen})
                return
            if path == "/api/pick-models":
                chosen = ask_dialog("folder", str(custom_model_dir() or ROOT))
                if not chosen:
                    self._json(200, {"cancelled": True})
                    return
                session.settings["customModels"] = chosen
                save_settings()
                self._json(200, {"customModels": chosen, "models": model_catalog()})
                return
            if path == "/api/run":
                self._start(payload)
                return
            if path == "/api/stop":
                self._stop()
                return
            if path == "/api/reveal":
                self._reveal(str(payload.get("target") or "folder"))
                return
            self._json(404, {"error": "Não encontrado."})
        except (UpscaleError, AuthError) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            traceback.print_exc()
            self._json(500, {"error": str(error)})

    def _bootstrap(self) -> dict:
        snap = session.snapshot()
        return {
            "ready": session.engine_ready,
            "engineError": session.engine_error,
            "download": session.download,
            "gpu": snap["gpu"],
            "route": snap["route"],
            "models": model_catalog(),
            "settings": session.settings,
            "image": {
                "name": snap["name"],
                "width": snap["width"],
                "height": snap["height"],
            }
            if snap["name"]
            else None,
        }

    def _upload(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            self._json(413, {"error": "A imagem passa do tamanho que o Alto aceita de uma vez."})
            return
        name = unquote(self.headers.get("X-Filename") or "imagem.png")
        data = self.rfile.read(length)
        WORK.mkdir(parents=True, exist_ok=True)
        target = WORK / f"entrada-{safe_stem(name)}{Path(name).suffix.lower() or '.png'}"
        target.write_bytes(data)
        self._json(200, remember_image(target, Path(name).name))

    def _remember_folder(self, folder: Path) -> None:
        files = list_images(folder)
        if not files:
            raise UpscaleError("Essa pasta não tem imagens compatíveis.")
        width, height = inspect_image(files[0], session.preview_original)
        with session.lock:
            session.folder = folder
            session.folder_files = files
            session.input_path = files[0]
            session.input_name = files[0].name
            session.width = width
            session.height = height
            session.result_ready = False
            session.outputs = []

    def _update_settings(self, payload: dict) -> None:
        allowed = {
            "outputDir",
            "model",
            "tile",
            "gpu",
            "dpi",
            "format",
            "quality",
            "tta",
            "customModels",
            "paper",
            "customCm",
            "scale",
            "sizeMode",
            "serviceUrl",
        }
        if "serviceUrl" in payload:
            value = str(payload.get("serviceUrl") or "").strip().rstrip("/")
            if value and not value.startswith(("http://", "https://")):
                raise UpscaleError("O endereço do serviço precisa começar com http:// ou https://.")
            payload = dict(payload)
            payload["serviceUrl"] = value
        for key, value in payload.items():
            if key in allowed:
                session.settings[key] = value
        if session.settings.get("outputDir"):
            Path(session.settings["outputDir"]).mkdir(parents=True, exist_ok=True)
        save_settings()

    def _current_account(self) -> dict | None:
        token = session_token(self.headers.get("Cookie"))
        if not token:
            return None
        if uses_remote_accounts():
            found = remote.whoami(service_url(), token)
            if not found or not found.get("user"):
                return None
            return {
                "email": found["user"]["email"],
                "subscription": found.get("subscription") or {},
                "billing": found.get("billing"),
            }
        return accounts.session_user(token)

    def _me(self) -> None:
        try:
            account = self._current_account()
        except UpscaleError as error:
            self._json(503, {"error": str(error)})
            return
        self._json(200, account_view(account))

    def _login(self, payload: dict, register: bool) -> None:
        email = payload.get("email")
        password = payload.get("password")
        if uses_remote_accounts():
            path = "/api/register" if register else "/api/login"
            token, data = remote.authenticate(service_url(), path, {"email": email, "password": password})
            body = {
                "user": data.get("user"),
                "subscription": data.get("subscription"),
                "billing": data.get("billing") or checkout_view(),
                "route": route_name(),
                "gpu": session.gpu_name or device_snapshot().get("name") or "",
            }
            self._json(200, body, {"Set-Cookie": cookie_header(token)})
            return
        token = accounts.register(str(email or ""), str(password or "")) if register else accounts.login(str(email or ""), str(password or ""))
        record = accounts.session_user(token)
        self._json(200, account_view(record), {"Set-Cookie": cookie_header(token)})

    def _logout(self) -> None:
        token = session_token(self.headers.get("Cookie"))
        if token and uses_remote_accounts():
            try:
                remote.logout(service_url(), token)
            except UpscaleError:
                pass
        elif token:
            accounts.logout(token)
        self._json(200, {"ok": True}, {"Set-Cookie": cookie_header("", clear=True)})

    def _checkout(self) -> None:
        try:
            account = self._current_account()
        except UpscaleError as error:
            self._json(503, {"error": str(error)})
            return
        if not account:
            self._json(401, {"error": "Entre na sua conta."})
            return
        if uses_remote_accounts():
            token = session_token(self.headers.get("Cookie"))
            self._json(200, remote.checkout(service_url(), token))
            return
        self._json(200, checkout_view())

    def _service_create(self) -> None:
        record = accounts.session_user(session_token(self.headers.get("Cookie")))
        length = int(self.headers.get("Content-Length") or 0)
        if not record or not record["subscription"].get("allows") or length <= 0 or length > MAX_UPLOAD:
            self.close_connection = True
        if not record:
            self._json(401, {"error": "Entre na sua conta para ampliar."})
            return
        if not record["subscription"].get("allows"):
            self._json(402, {"error": "A mensalidade desta conta terminou. Renove para continuar ampliando."})
            return
        if length <= 0 or length > MAX_UPLOAD:
            self._json(413, {"error": "A imagem passa do tamanho que o Alto aceita de uma vez."})
            return
        try:
            options = json.loads(base64.b64decode(self.headers.get("X-Options") or ""))
        except (ValueError, json.JSONDecodeError):
            self.close_connection = True
            self._json(400, {"error": "Pedido de ampliação incompleto."})
            return
        if not isinstance(options, dict):
            self._json(400, {"error": "Pedido de ampliação incompleto."})
            return
        filename = Path(unquote(self.headers.get("X-Filename") or "imagem.png")).name
        data = self.rfile.read(length)
        job_id = start_service_job(record, data, filename, options)
        self._json(200, {"id": job_id})

    def _service_status(self, query: str) -> None:
        record = accounts.session_user(session_token(self.headers.get("Cookie")))
        if not record:
            self._json(401, {"error": "Entre na sua conta para ampliar."})
            return
        job_id = (parse_qs(query).get("id") or [""])[0]
        with service_lock:
            job = service_jobs.get(job_id)
            if not job or job["user_id"] != record["id"]:
                self._json(404, {"error": "Não encontrei esse aumento."})
                return
            payload = public_service_job(job)
        self._json(200, payload)

    def _service_file(self, query: str) -> None:
        record = accounts.session_user(session_token(self.headers.get("Cookie")))
        if not record:
            self._json(401, {"error": "Entre na sua conta para ampliar."})
            return
        job_id = (parse_qs(query).get("id") or [""])[0]
        with service_lock:
            job = service_jobs.get(job_id)
            if not job or job["user_id"] != record["id"]:
                self._json(404, {"error": "Não encontrei esse aumento."})
                return
            if job["status"] != "done" or not job["dest"]:
                self._json(409, {"error": "O arquivo ainda não está pronto."})
                return
            dest = Path(job["dest"])
        if not dest.exists():
            self._json(404, {"error": "O arquivo já foi entregue."})
            return
        data = dest.read_bytes()
        mime = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
        self._bytes(data, mime)
        with service_lock:
            job["dest"] = ""
        shutil.rmtree(WORK / "servico" / job_id, ignore_errors=True)

    def _service_stop(self, job_id: str) -> None:
        record = accounts.session_user(session_token(self.headers.get("Cookie")))
        if not record:
            self._json(401, {"error": "Entre na sua conta para ampliar."})
            return
        with service_lock:
            job = service_jobs.get(job_id)
            if not job or job["user_id"] != record["id"]:
                self._json(404, {"error": "Não encontrei esse aumento."})
                return
            job["cancel"].set()
            process = job["control"].get("proc")
        if process and process.poll() is None:
            process.kill()
        self._json(200, {"ok": True})

    def _start(self, payload: dict) -> None:
        try:
            account = self._current_account()
        except UpscaleError as error:
            self._json(503, {"error": str(error)})
            return
        if not account:
            self._json(401, {"error": "Entre na sua conta para ampliar."})
            return
        if not (account.get("subscription") or {}).get("allows"):
            self._json(402, {"error": "A mensalidade desta conta terminou. Renove para continuar ampliando."})
            return
        token = session_token(self.headers.get("Cookie"))
        with session.lock:
            if session.status == "running":
                self._json(409, {"error": "Já existe um aumento em andamento."})
                return
            session.status = "running"
            session.progress = 0
            session.message = "Começando"
            session.error = ""
            session.logs = []
            session.result_ready = False
            session.outputs = []
            session.cancel.clear()
        try:
            self._update_settings(payload)
        except (UpscaleError, AuthError) as error:
            with session.lock:
                session.status = "error"
                session.error = str(error)
                session.message = ""
            self._json(400, {"error": str(error)})
            return
        threading.Thread(target=run_job, args=(payload, token), daemon=True).start()
        self._json(200, {"ok": True})

    def _stop(self) -> None:
        session.cancel.set()
        process = session.control.get("proc")
        if process and process.poll() is None:
            process.kill()
        self._json(200, {"ok": True})

    def _reveal(self, target: str) -> None:
        if target == "file" and session.outputs:
            os.startfile(session.outputs[-1])
        else:
            os.startfile(output_dir())
        self._json(200, {"ok": True})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2_000_000:
            raise UpscaleError("Pedido grande demais.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _json(self, code: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, mime: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, mime: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def prepare_engine() -> None:
    try:
        def on_progress(value: float) -> None:
            session.download = value

        ensure_engine(on_progress)
        session.engine_ready = True
    except Exception as error:
        session.engine_ready = False
        session.engine_error = str(error)
    probe_device()


def open_app(url: str) -> None:
    import subprocess

    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    flags = 0x08000000 if sys.platform == "win32" else 0
    for candidate in candidates:
        if candidate.exists():
            subprocess.Popen(
                [str(candidate), f"--app={url}", "--new-window", "--window-size=1440,920"],
                creationflags=flags,
            )
            return
    webbrowser.open(url)


def tk_loop() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()

    def pump() -> None:
        try:
            while True:
                opener = dialog_queue.get_nowait()
                opener(root)
        except queue.Empty:
            pass
        root.after(80, pump)

    root.after(80, pump)
    root.mainloop()


def bind_server() -> tuple[ThreadingHTTPServer, int]:
    for port in range(PORT, PORT + 20):
        try:
            return ThreadingHTTPServer((HOST, port), Handler), port
        except OSError:
            continue
    raise SystemExit("Não consegui abrir uma porta local para o Alto.")


def main() -> None:
    global bound_port
    accounts.init()
    for folder in (WORK, DEFAULT_OUTPUT, MODELS_DIR, STATIC):
        folder.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=prepare_engine, daemon=True).start()
    server, port = bind_server()
    bound_port = port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{HOST}:{port}"
    print(f"Alto está em {url}", flush=True)
    print("Feche esta janela para encerrar.", flush=True)
    if os.environ.get("ALTO_NO_BROWSER") != "1":
        open_app(url)
    try:
        tk_loop()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"Seleção de pastas indisponível nesta janela: {error}")
        print("Arrastar a imagem para a tela continua funcionando.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
