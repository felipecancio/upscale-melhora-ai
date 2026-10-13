"""Pipeline de upscale local.

O aumento em si roda no executável oficial Real-ESRGAN ncnn Vulkan (licença MIT)
e nos modelos publicados com ele (BSD-3-Clause). Este arquivo é código original.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "realesrgan"
WORK = ROOT / "trabalho"
DEFAULT_OUTPUT = ROOT / "saida"
MODELS_DIR = ROOT / "modelos"
ENGINE_URL = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/"
    "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
)
MAX_LONG = 16000
INTERMEDIATE_MAX = 24000
MAX_UPLOAD = 500 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff"}

PAPERS_MM = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
}

BUILTIN_MODELS = [
    {
        "id": "realesrgan-x4plus",
        "name": "Foto",
        "detail": "Equilíbrio para fotografia e arte que vai para o papel.",
    },
    {
        "id": "realesrgan-x4plus-anime",
        "name": "Ilustração",
        "detail": "Traço, pintura digital e anime.",
    },
    {
        "id": "realesr-animevideov3",
        "name": "Desenho leve",
        "detail": "Mais rápido em desenho simples, com menos detalhe fino.",
    },
]
BUILTIN_IDS = {item["id"] for item in BUILTIN_MODELS}

Image.MAX_IMAGE_PIXELS = 300_000_000


class UpscaleError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Plan:
    out_width: int
    out_height: int
    scales: list[int]
    resize: bool
    tile: int
    note: str
    warn: bool
    cm_width: float
    cm_height: float
    label: str
    embed_dpi: int
    already_enough: bool

    def to_json(self) -> dict:
        return {
            "outWidth": self.out_width,
            "outHeight": self.out_height,
            "passCount": len(self.scales),
            "passScales": self.scales,
            "resize": self.resize,
            "tile": self.tile,
            "note": self.note,
            "warn": self.warn,
            "cmWidth": round(self.cm_width, 1),
            "cmHeight": round(self.cm_height, 1),
            "label": self.label,
            "embedDpi": self.embed_dpi,
            "alreadyEnough": self.already_enough,
        }


def engine_paths() -> tuple[Path, Path] | None:
    exe = VENDOR / "realesrgan-ncnn-vulkan.exe"
    weights = VENDOR / "models" / "realesrgan-x4plus.bin"
    if exe.exists() and weights.exists():
        return exe, VENDOR / "models"
    return None


def ensure_engine(on_progress=None) -> None:
    if engine_paths():
        return
    VENDOR.mkdir(parents=True, exist_ok=True)
    archive = WORK / "realesrgan-windows.zip"
    WORK.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(ENGINE_URL, headers={"User-Agent": "Alto"})
    with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(min(100.0, done / total * 100))

    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (VENDOR / member.filename).resolve()
            if not str(target).startswith(str(VENDOR.resolve())):
                raise UpscaleError("O pacote do motor veio com um caminho inseguro.")
        bundle.extractall(VENDOR)
    archive.unlink(missing_ok=True)
    if not engine_paths():
        raise UpscaleError("O motor foi baixado, mas os arquivos esperados não apareceram.")


def list_custom_models(folder: Path | None) -> list[dict]:
    if folder is None or not folder.exists():
        return []
    found = []
    for param in sorted(folder.glob("*.param")):
        if param.stem in BUILTIN_IDS:
            continue
        if not (folder / f"{param.stem}.bin").exists():
            continue
        found.append(
            {
                "id": param.stem,
                "name": param.stem,
                "detail": "Modelo extra desta pasta.",
                "custom": True,
            }
        )
    return found


def list_images(folder: Path) -> list[Path]:
    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and not path.name.startswith(".")
    ]
    return sorted(files, key=lambda path: path.name.lower())


def flatten(image: Image.Image) -> Image.Image:
    if image.mode == "RGBA":
        base = Image.new("RGB", image.size, (255, 255, 255))
        base.paste(image, mask=image.getchannel("A"))
        return base
    if image.mode == "P" and "transparency" in image.info:
        return flatten(image.convert("RGBA"))
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def normalize(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    profile = image.info.get("icc_profile")
    if profile and image.mode in ("RGB", "CMYK"):
        try:
            source = ImageCms.ImageCmsProfile(BytesIO(profile))
            target = ImageCms.createProfile("sRGB")
            image = ImageCms.profileToProfile(image, source, target, outputMode="RGB")
        except Exception:
            image = image.convert("RGB")
    return flatten(image)


def inspect_image(path: Path, preview_path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image = normalize(image)
        width, height = image.size
        preview = image.copy()
        preview.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview.save(preview_path, "JPEG", quality=88)
        return width, height


def prepare_working_png(path: Path, dest: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image = normalize(image)
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(dest, "PNG")
        return image.size


def _paper_long_mm(paper: str, custom_cm: float) -> float:
    if paper == "custom":
        cm = min(150.0, max(5.0, float(custom_cm)))
        return cm * 10.0
    if paper not in PAPERS_MM:
        raise UpscaleError("Tamanho de papel desconhecido.")
    _short, long_side = PAPERS_MM[paper]
    return long_side


def _fit_long_side(src_w: int, src_h: int, long_mm: float, dpi: int) -> tuple[int, int]:
    long_px = max(1, round(long_mm / 25.4 * dpi))
    if src_w >= src_h:
        width = long_px
        height = max(1, round(long_px * src_h / src_w))
    else:
        height = long_px
        width = max(1, round(long_px * src_w / src_h))
    return width, height


def _clamp_box(width: int, height: int) -> tuple[int, int, bool]:
    long_side = max(width, height)
    if long_side <= MAX_LONG:
        return width, height, False
    ratio = MAX_LONG / long_side
    return max(1, round(width * ratio)), max(1, round(height * ratio)), True


def _auto_tile(user_tile: int, input_long: int) -> int:
    if user_tile > 0:
        return user_tile
    if input_long >= 4000:
        return 256
    if input_long >= 2500:
        return 512
    return 0


def _cm(width: int, height: int, dpi: int) -> tuple[float, float]:
    return width / dpi * 2.54, height / dpi * 2.54


def _grow_box(width: int, height: int, factor: int) -> tuple[int, int]:
    return width * factor, height * factor


def uses_fixed_4x(model_id: str, custom: bool) -> bool:
    """Os modelos de foto só existem em 4x. Pedir 2x direto embaralha os blocos da imagem."""
    return custom or model_id != "realesr-animevideov3"


def _scale_steps(scale: int, scale4_only: bool) -> list[int]:
    if scale == 4:
        return [4]
    if scale == 2:
        return [4] if scale4_only else [2]
    if scale == 8:
        return [4, 4] if scale4_only else [4, 2]
    raise UpscaleError("A escala precisa ser 2x, 4x ou 8x.")


def _print_steps(src_w: int, src_h: int, desired_w: int, desired_h: int, scale4_only: bool) -> list[int]:
    scales: list[int] = []
    current_w, current_h = src_w, src_h
    target = max(desired_w, desired_h)
    while len(scales) < 2 and max(current_w, current_h) < target * 0.98:
        current = max(current_w, current_h)
        remaining = target / current
        if scale4_only or remaining > 2.15:
            factor = 4
        elif remaining > 1.35:
            factor = 2
        else:
            break
        next_w, next_h = _grow_box(current_w, current_h, factor)
        if max(next_w, next_h) > INTERMEDIATE_MAX:
            break
        scales.append(factor)
        current_w, current_h = next_w, next_h
    return scales


def _apply_steps(src_w: int, src_h: int, scales: list[int]) -> tuple[list[int], int, int]:
    used: list[int] = []
    current_w, current_h = src_w, src_h
    for step in scales:
        next_w, next_h = _grow_box(current_w, current_h, step)
        if max(next_w, next_h) > INTERMEDIATE_MAX:
            break
        used.append(step)
        current_w, current_h = next_w, next_h
    return used, current_w, current_h


def make_plan(
    src_w: int,
    src_h: int,
    *,
    size_mode: str,
    scale: int,
    paper: str,
    custom_cm: float,
    dpi: int,
    tile: int,
    scale4_only: bool,
    folder: bool,
) -> Plan:
    if src_w < 2 or src_h < 2:
        raise UpscaleError("A imagem é pequena demais.")
    dpi = min(600, max(72, int(dpi)))
    tile = int(tile)
    if tile < 0 or (tile != 0 and tile < 32):
        raise UpscaleError("O bloco precisa ser automático ou de pelo menos 32 px.")

    embed_dpi = dpi
    already = False
    clamped = False

    if size_mode == "scale":
        if scale not in (2, 4, 8):
            raise UpscaleError("A escala precisa ser 2x, 4x ou 8x.")
        desired_w, desired_h, clamped = _clamp_box(src_w * scale, src_h * scale)
        label = f"{scale}x"
        scales = _scale_steps(scale, scale4_only)
    elif size_mode == "print":
        long_mm = _paper_long_mm(paper, custom_cm)
        desired_w, desired_h = _fit_long_side(src_w, src_h, long_mm, dpi)
        desired_w, desired_h, clamped = _clamp_box(desired_w, desired_h)
        label = f"{int(round(custom_cm))} cm" if paper == "custom" else paper
        if src_w >= desired_w and src_h >= desired_h:
            long_px = max(src_w, src_h)
            embed_dpi = max(dpi, round(long_px / (long_mm / 25.4)))
            already = True
            scales = []
            desired_w, desired_h = src_w, src_h
        else:
            scales = _print_steps(src_w, src_h, desired_w, desired_h, scale4_only)
    else:
        raise UpscaleError("Modo de tamanho desconhecido.")

    if already:
        produced_w, produced_h = src_w, src_h
        resize = False
    else:
        scales, produced_w, produced_h = _apply_steps(src_w, src_h, scales)
        resize = (produced_w, produced_h) != (desired_w, desired_h)

    running = max(src_w, src_h)
    inputs = []
    for step in scales:
        inputs.append(running)
        running *= step
    chosen_tile = _auto_tile(tile, max(inputs) if inputs else max(src_w, src_h))
    cm_w, cm_h = _cm(desired_w, desired_h, embed_dpi)
    shortfall = max(desired_w / produced_w, desired_h / produced_h) if produced_w else 1
    note = _note(
        size_mode=size_mode,
        scale=scale,
        scales=scales,
        resize=resize,
        clamped=clamped,
        already=already,
        tile=chosen_tile,
        user_tile=tile,
        embed_dpi=embed_dpi,
        megapixels=desired_w * desired_h / 1_000_000,
        shortfall=shortfall,
    )
    if folder:
        note = "Pela primeira imagem da pasta. As outras seguem a mesma regra. " + note

    warn = bool(clamped or len(scales) >= 2 or shortfall > 1.2 or desired_w * desired_h > 40_000_000)
    return Plan(
        out_width=desired_w,
        out_height=desired_h,
        scales=scales,
        resize=resize,
        tile=chosen_tile,
        note=note,
        warn=warn,
        cm_width=cm_w,
        cm_height=cm_h,
        label=label,
        embed_dpi=embed_dpi,
        already_enough=already,
    )


def _note(
    *,
    size_mode: str,
    scale: int,
    scales: list[int],
    resize: bool,
    clamped: bool,
    already: bool,
    tile: int,
    user_tile: int,
    embed_dpi: int,
    megapixels: float,
    shortfall: float,
) -> str:
    if already:
        text = (
            "A imagem já cobre esse papel. Os pixels originais ficam no arquivo, "
            f"marcados a {embed_dpi} DPI."
        )
    elif size_mode == "scale" and scale == 8 and scales == [4, 4]:
        text = "Duas passagens completas em 4x, ajustadas para 8x. A arte permanece no lugar."
    elif size_mode == "scale" and scale == 8 and scales == [4, 2]:
        text = "Duas passagens do modelo, fechando em 8x."
    elif size_mode == "scale" and len(scales) == 1 and not resize:
        text = f"Uma passagem do modelo, direto em {scale}x."
    elif len(scales) >= 2 and resize:
        text = "Duas passagens do modelo e um ajuste final na medida pedida."
    elif len(scales) == 1 and resize:
        text = "Uma passagem do modelo e um ajuste fino até a medida pedida."
    elif len(scales) >= 2:
        text = "Duas passagens do modelo. O arquivo fica grande e demora mais."
    elif not scales and resize:
        text = "A diferença até a medida pedida é pequena. Só o tamanho final é ajustado."
    else:
        text = "Uma passagem do modelo."

    if shortfall > 1.2 and not already:
        text += " Parte desse aumento final é interpolação, porque a foto original é pequena para o tamanho pedido."
    if clamped:
        text += f" O lado maior foi limitado a {MAX_LONG} px para caber na memória."
    if tile and user_tile == 0:
        text += f" Blocos de {tile} px, automáticos, para a placa processar sem estourar."
    if megapixels >= 40:
        text += f" O arquivo final tem cerca de {megapixels:.0f} megapixels."
    return text


def _creation_flags() -> int:
    if sys.platform == "win32":
        return 0x08000000
    return 0


def run_model(
    exe: Path,
    models: Path,
    src: Path,
    dest: Path,
    model: str,
    scale: int,
    tile: int,
    gpu: str,
    tta: bool,
    control: dict,
    cancel: threading.Event,
    on_percent,
    on_log,
    on_gpu,
) -> None:
    command = [
        str(exe),
        "-i",
        str(src),
        "-o",
        str(dest),
        "-n",
        model,
        "-s",
        str(scale),
        "-m",
        str(models),
        "-f",
        "png",
        "-t",
        str(tile),
    ]
    if gpu.strip():
        command.extend(["-g", gpu.strip()])
    if tta:
        command.append("-x")

    on_log(" ".join(command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(exe.parent),
        creationflags=_creation_flags(),
    )
    control["proc"] = process
    buffer = b""
    assert process.stdout is not None
    bad_lines: list[str] = []
    try:
        while True:
            if cancel.is_set():
                process.kill()
                raise Cancelled("Interrompido.")
            chunk = process.stdout.read(256)
            if not chunk:
                break
            buffer += chunk
            pieces = re.split(br"[\r\n]", buffer)
            buffer = pieces.pop() if pieces else b""
            for piece in pieces:
                line = piece.decode("utf-8", "replace").strip()
                if not line:
                    continue
                on_log(line)
                gpu_match = re.search(r"\[\d+\s+(.+?)\]", line)
                if gpu_match and "queueC" in line:
                    on_gpu(gpu_match.group(1).strip())
                percent = re.search(r"(\d+(?:[.,]\d+)?)\s*%", line)
                if percent:
                    on_percent(float(percent.group(1).replace(",", ".")))
                lowered = line.lower()
                if any(token in lowered for token in ("error", "failed", "invalid gpu", "out of memory")):
                    bad_lines.append(line)
        if buffer.strip():
            on_log(buffer.decode("utf-8", "replace").strip())
    finally:
        code = process.wait()
        control["proc"] = None
    if cancel.is_set():
        raise Cancelled("Interrompido.")
    if code != 0 or not dest.exists():
        detail = bad_lines[-1] if bad_lines else f"O motor encerrou com código {code}."
        hint = ""
        blob = " ".join(bad_lines).lower()
        if any(token in blob for token in ("memory", "vkallocate", "invalid gpu")):
            hint = " Tente o bloco 128 em Ajustes, ou outra GPU."
        raise UpscaleError(detail + hint)


def save_final(src: Path, dest: Path, fmt: str, quality: int, dpi: int, size: tuple[int, int] | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        if size and image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        if fmt == "png":
            image.save(dest, "PNG", dpi=(dpi, dpi))
        elif fmt == "jpg":
            image.convert("RGB").save(
                dest,
                "JPEG",
                quality=quality,
                subsampling=0,
                dpi=(dpi, dpi),
            )
        elif fmt == "webp":
            image.save(dest, "WEBP", quality=quality, method=4)
        else:
            raise UpscaleError("Formato de saída desconhecido.")


def make_result_preview(src: Path, dest: Path) -> None:
    with Image.open(src) as image:
        preview = image.convert("RGB")
        preview.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        preview.save(dest, "JPEG", quality=88)


def process_image(
    source: Path,
    dest: Path,
    plan: Plan,
    *,
    model: str,
    models: Path,
    exe: Path,
    gpu: str,
    tta: bool,
    quality: int,
    fmt: str,
    custom_model: bool,
    control: dict,
    cancel: threading.Event,
    on_progress,
    on_log,
    on_gpu,
    work: Path | None = None,
) -> None:
    folder = work or WORK
    folder.mkdir(parents=True, exist_ok=True)
    working = folder / "prepared.png"
    on_progress(2, "Preparando a imagem")
    prepare_working_png(source, working)
    current = working
    total = max(1, len(plan.scales))

    for index, scale in enumerate(plan.scales):
        if cancel.is_set():
            raise Cancelled("Interrompido.")
        step_out = folder / f"pass-{index + 1}.png"
        model_scale = scale if model == "realesr-animevideov3" and not custom_model else 4

        def percent(value, index=index):
            fraction = (index + value / 100) / total * 0.9
            on_progress(4 + fraction * 100, f"Passagem {index + 1} de {len(plan.scales)}")

        run_model(
            exe,
            models,
            current,
            step_out,
            model,
            model_scale,
            plan.tile,
            gpu,
            tta,
            control,
            cancel,
            percent,
            on_log,
            on_gpu,
        )
        current = step_out

    on_progress(94, "Gravando o arquivo de impressão")
    target = None
    if not plan.already_enough:
        with Image.open(current) as probe:
            if probe.size != (plan.out_width, plan.out_height):
                target = (plan.out_width, plan.out_height)
    save_final(current, dest, fmt, quality, plan.embed_dpi, target)
    on_progress(100, "Concluído")


def safe_stem(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem).strip().rstrip(".")
    return stem or "imagem"


def output_filename(stem: str, label: str, fmt: str) -> str:
    extension = {"jpg": "jpg", "png": "png", "webp": "webp"}[fmt]
    return f"{stem}_alto_{label.replace(' ', '')}.{extension}"
