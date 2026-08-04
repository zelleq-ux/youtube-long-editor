"""
Etapa 1: Ingesta.

A diferencia de newclips-viral-pipeline, aquí la fuente es SIEMPRE un
archivo local (la grabación horizontal de OBS), no una URL. Normaliza el
vídeo con ffmpeg y lo deja listo en data/raw/<video_id>.mp4.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from src.common import db
from src.common.config import REPO_ROOT, load_config

logger = logging.getLogger(__name__)

_REQUIRED_BINARIES = ("ffmpeg", "ffprobe")


def _check_binarios_disponibles() -> None:
    """Comprueba que ffmpeg y ffprobe estén en el PATH antes de hacer nada más."""
    faltantes = [b for b in _REQUIRED_BINARIES if shutil.which(b) is None]
    if faltantes:
        raise RuntimeError(
            "No se encuentran en el PATH los binarios requeridos: "
            f"{', '.join(faltantes)}. Instala ffmpeg (incluye ffprobe) y añádelo al PATH."
        )


def slugify_video_id(stem: str) -> str:
    """
    Convierte el nombre de archivo (sin extensión) en un video_id seguro
    para el sistema de archivos: minúsculas, caracteres raros -> "_",
    colapsando repeticiones y sin "_" al principio/final.
    """
    slug = stem.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "video"


def _parse_frame_rate(raw: str | None) -> float | None:
    """Convierte un frame rate de ffprobe (p.ej. "30000/1001") a float."""
    if not raw:
        return None
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            num_f, den_f = float(num), float(den)
        except ValueError:
            return None
        if den_f == 0:
            return None
        return num_f / den_f
    try:
        return float(raw)
    except ValueError:
        return None


def _probe(path: Path) -> dict:
    """Ejecuta ffprobe sobre el archivo y devuelve el JSON con format + streams."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló analizando {path}:\n{result.stderr[-2000:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe devolvió un JSON inválido para {path}: {exc}") from exc


def _extract_duration(video_stream: dict, format_info: dict) -> float:
    """Duración en segundos: preferimos la del stream de vídeo, si no la del contenedor."""
    for candidate in (video_stream.get("duration"), format_info.get("duration")):
        if candidate is None:
            continue
        try:
            return float(candidate)
        except (TypeError, ValueError):
            continue
    raise ValueError(
        "No se pudo determinar la duración del vídeo de entrada (ffprobe no la reportó)."
    )


def run(video_id: str, config: dict, *, local_path: str) -> dict:
    """
    Normaliza el vídeo local de OBS a mp4 en data/raw/<video_id>.mp4.

    Args:
        video_id: identificador único del vídeo dentro del pipeline.
        config: dict cargado de config/settings.yaml.
        local_path: ruta al archivo de grabación de OBS.

    Returns:
        dict con {"video_id": str, "raw_path": str, "duration_s": float}
    """
    _check_binarios_disponibles()

    input_path = Path(local_path).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"El archivo de entrada no existe: {input_path}")
    if not input_path.is_file():
        raise ValueError(f"La ruta de entrada no es un archivo: {input_path}")

    raw_dir = (REPO_ROOT / config["paths"]["raw"]).resolve()
    output_path = raw_dir / f"{video_id}.mp4"

    if output_path.resolve() == input_path:
        raise ValueError(
            "La ruta de salida coincide con la de entrada; no se puede sobrescribir "
            f"el archivo original: {input_path}"
        )

    logger.info("Entrada: %s", input_path)

    probe = _probe(input_path)
    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        raise ValueError(f"El archivo de entrada no tiene ninguna pista de vídeo: {input_path}")
    if not audio_streams:
        raise ValueError(
            f"El archivo de entrada no tiene ninguna pista de audio: {input_path}. "
            "Se esperaba audio en una grabación de OBS; revisa la fuente."
        )

    video_stream = video_streams[0]
    width = video_stream.get("width")
    height = video_stream.get("height")
    fps = _parse_frame_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    duration_s = _extract_duration(video_stream, probe.get("format", {}))

    logger.info(
        "Detectado: %sx%s @ %s fps, duración %.2fs, %d pista(s) de audio",
        width, height, f"{fps:.3f}" if fps else "?", duration_s, len(audio_streams),
    )
    if len(audio_streams) > 1:
        # Solo se mapea la pista de audio 0 (-map 0:a:0, más abajo). Si OBS
        # grabó varias pistas (p.ej. una con el mic propio y otra con el
        # audio de Discord de un compañero de stream en un track separado),
        # aquí se estarían descartando en silencio todas menos la primera:
        # ese audio jamás llegaría a las siguientes etapas (transcripción,
        # detect_cuts...). Se avisa explícitamente para que el usuario
        # verifique qué pista es la 0 y si es la que quiere conservar.
        logger.warning(
            "El archivo de entrada tiene %d pistas de audio, pero solo se conserva la "
            "pista 0 (índices ffprobe: %s). Si alguna de las otras pistas contiene audio "
            "que quieres en el vídeo final (p.ej. un compañero de stream en un track "
            "separado), ese audio se está descartando silenciosamente en la ingesta.",
            len(audio_streams), [s.get("index") for s in audio_streams],
        )

    # Frame rate objetivo para forzar CFR (frame rate constante). Se usa
    # r_frame_rate (el nominal declarado por el contenedor/stream, p.ej.
    # "30000/1001" o "60/1") en vez de avg_frame_rate: en una grabación VFR
    # de OBS el promedio real puede ser algo como 59.87fps aunque el valor
    # nominal configurado sea 60fps, y para un -r de salida interesa el
    # nominal, no el promedio observado. Se conserva la cadena original de
    # ffprobe (sin pasar por float) para no introducir artefactos de
    # redondeo (p.ej. 29.969999999999999) al construir el argumento de ffmpeg.
    r_frame_rate_raw = video_stream.get("r_frame_rate")
    fps_cfr = _parse_frame_rate(r_frame_rate_raw)
    # Cota de cordura: descarta valores <= 0 o absurdamente altos (stream
    # malformado); en ese caso no se pasa -r y solo se fuerza -fps_mode cfr.
    if fps_cfr is not None and not (0 < fps_cfr <= 300):
        logger.warning(
            "Frame rate nominal detectado (%s -> %.3f fps) fuera de rango razonable; "
            "se ignora para -r y solo se fuerza -fps_mode cfr.",
            r_frame_rate_raw, fps_cfr,
        )
        fps_cfr = None
        r_frame_rate_raw = None

    if fps_cfr is None:
        logger.warning(
            "No se pudo determinar un frame rate nominal fiable para forzar CFR "
            "(r_frame_rate=%r); se fuerza CFR con -fps_mode cfr sin -r explícito.",
            r_frame_rate_raw,
        )
    else:
        logger.info("Frame rate objetivo para CFR (-r): %s (%.3f fps)", r_frame_rate_raw, fps_cfr)

    raw_dir.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        logger.warning("El archivo de salida ya existe y va a ser sobrescrito: %s", output_path)

    logger.info("Salida: %s", output_path)

    # Re-encode (no stream-copy): las siguientes etapas hacen cortes exactos por
    # frame y optical flow, así que interesa un archivo limpio y uniformemente
    # codificado en vez de conservar el códec/GOP original de OBS. Se mantiene
    # resolución y orientación originales (no se recorta ni redimensiona).
    #
    # Se fuerza además CFR (frame rate constante) con -fps_mode cfr (más
    # -r con el nominal detectado cuando se conoce): una grabación VFR de
    # OBS tiene intervalos entre frames irregulares, y detect_cuts hace
    # cortes exactos por frame + optical flow asumiendo un mapeo uniforme
    # entre tiempo y número de frame. Con VFR ese mapeo se desvía y los
    # timestamps de la transcripción/cortes dejan de corresponder al frame
    # real, así que aquí se normaliza a CFR para que las siguientes etapas
    # trabajen sobre una base temporal fiable.
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "medium",
        "-pix_fmt", "yuv420p",
    ]
    if fps_cfr is not None:
        cmd += ["-r", str(r_frame_rate_raw)]
    cmd += ["-fps_mode", "cfr"]
    cmd += [
        "-c:a", "aac",
        "-ar", "48000",
        "-ac", "2",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("Normalizando con ffmpeg (re-encode H.264/AAC)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falló al normalizar {input_path}:\n{result.stderr[-4000:]}")

    logger.info(
        "Ingesta completa: video_id=%s raw_path=%s duration_s=%.2f",
        video_id, output_path, duration_s,
    )

    # Solo se marca como "ingested" una vez el re-encode de ffmpeg ha
    # terminado con éxito (comprobación de returncode más arriba).
    db.set_status(video_id, "ingested")

    return {
        "video_id": video_id,
        "raw_path": str(output_path),
        "duration_s": duration_s,
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Ingesta de grabación local de OBS")
    parser.add_argument("--file", required=True, help="Ruta al archivo de OBS")
    parser.add_argument("--video-id", help="Identificador a usar (por defecto, derivado del nombre de archivo)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    video_id = args.video_id or slugify_video_id(Path(args.file).stem)
    config = load_config()

    run(video_id, config, local_path=args.file)


if __name__ == "__main__":
    _cli()
