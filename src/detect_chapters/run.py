"""
Etapa 4: Detección de capítulos.

Analiza la transcripción completa (data/transcripts/<video_id>.json) con
Claude (config['detect_chapters']['claude_model']) para detectar bloques
temáticos -- cambios de juego, de tema de conversación, secciones
claramente diferenciadas -- y genera una lista de capítulos con timestamp
+ título. Usa structured outputs (output_format sobre client.messages.parse,
ver _ChaptersResponseModel) en vez de pedirle a Claude que "solo devuelva
JSON válido" en el prompt: la API garantiza la forma de la respuesta, así
que el prompt se centra en la tarea (qué es un capítulo, separación
mínima) y no en el formato de salida.

IMPORTANTE -- remapeo a la línea de tiempo editada: Claude analiza la
transcripción ORIGINAL (antes de aplicar los cortes de detect_cuts), así
que cada timestamp que propone se remapea restando la duración acumulada
de los cortes anteriores a ese punto -- reutilizando
src.common.timeline.map_to_edited_timeline, la misma lógica que usa
edit/run.py para el zoom hacia la webcam (movida a src/common/ para no
importar lógica de negocio de edit/ directamente, ver CLAUDE.md). Si el
timestamp propuesto cae DENTRO de un tramo cortado (Claude no sabe nada de
cuts.json), se ajusta primero al inicio del tramo conservado más cercano
(ver _snap_to_nearest_kept_start) antes de remapear -- en la práctica esto
casi siempre es el tramo conservado que sigue al corte (el que empieza
justo cuando termina el corte), salvo que el tramo conservado ANTERIOR sea
tan corto que su propio inicio quede más cerca del timestamp propuesto.

config['detect_chapters']['min_chapter_seconds'] se aplica dos veces: se
le pide a Claude en el prompt que no genere capítulos más juntos que eso
(evita que proponga un capítulo por cada pequeño cambio de frase), y
además se verifica programáticamente sobre la línea de tiempo YA EDITADA
(remap_chapters_to_edited_timeline) descartando cualquier capítulo que
quede demasiado cerca del anterior tras el remapeo -- los cortes pueden
acercar en el tiempo capítulos que en el vídeo original estaban bien
separados, así que la separación real que importa es la del vídeo final,
no la que ve Claude.

El primer capítulo debe empezar en el instante 0 de la línea de tiempo
editada (convención de YouTube: el primer timestamp de la lista de
capítulos debe ser 00:00) -- si Claude no propone ninguno prácticamente
ahí, se antepone uno genérico ("Introducción").

Intro grabado aparte (2026-08-10, ver CLAUDE.md "Intro grabado aparte"):
si existe data/output/<video_id>/intro.mp4, este módulo antepone SIEMPRE
un capítulo "Introducción" en 0:00 representando ese clip (en vez del
genérico anterior, que solo se insertaba si Claude no proponía nada cerca
de 0) y desplaza todos los capítulos ya calculados por la duración de ese
intro (remap_chapters_to_edited_timeline, parámetro intro_duration_s).
Esa duración se lee directamente (ffprobe) de intro.mp4 -- NOMINAL, sin
calibrar contra un final.mp4 real, porque este módulo puede ejecutarse
ANTES que edit/ (ver "Nota de orden" más abajo: los timestamps de
capítulos ya son una aproximación a la línea de tiempo que producirá
edit/, no una medición frame-exacta -- a diferencia de subtitles/, que sí
espera a poder calibrar contra el vídeo final real porque necesita
precisión de subsegundo). intro_duration_s=0.0 (el valor por defecto, sin
intro.mp4) reproduce EXACTAMENTE el comportamiento anterior.

Guarda data/chapters/<video_id>/chapters.json (línea de tiempo editada) y
data/output/<video_id>/chapters.txt en formato listo para pegar en la
descripción de YouTube, p.ej.:

    00:00 Introducción
    04:32 Empieza la partida de Rust
    18:10 Evento random en el mapa
    ...

(o `H:MM:SS` en vez de `M:SS` si el vídeo dura una hora o más, ver
_format_youtube_timestamp).

Nota de orden: si este módulo corre antes que edit/, los timestamps ya
están remapeados a la línea de tiempo que producirá edit/ (asumiendo que
edit/ aplica exactamente los cortes de cuts.json en ese momento) -- ver
CLAUDE.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

import anthropic
from pydantic import BaseModel

from src.common import db
from src.common.config import REPO_ROOT, load_config
from src.common.timeline import compute_keep_segments, map_to_edited_timeline

logger = logging.getLogger(__name__)

# Respuesta no-streaming: de sobra para una lista de capítulos (salida
# pequeña) incluso con el pensamiento adaptativo de Sonnet 5 activo por
# defecto (cuenta como parte de max_tokens). Muy por debajo del umbral al
# que el SDK exige streaming para evitar timeouts HTTP (~16000 sin
# streaming).
_MAX_TOKENS = 8000

_GENERIC_INTRO_TITLE = "Introducción"

# Margen (s) para considerar que el primer capítulo que propone Claude "ya
# está prácticamente en 0" y no hace falta anteponer uno genérico -- un
# capítulo a, p.ej., 0.3s tras remapear/redondear no es una intro real
# perdida, es ruido de redondeo.
_FIRST_CHAPTER_EPSILON_SECONDS = 0.5


class _ChapterModel(BaseModel):
    timestamp_original_s: float
    title: str


class _ChaptersResponseModel(BaseModel):
    chapters: list[_ChapterModel]


_SYSTEM_PROMPT = (
    "Eres un editor de vídeo que prepara los capítulos para la descripción "
    "de YouTube de directos en español. Analizas la transcripción completa "
    "de una grabación y detectas bloques temáticos claramente "
    "diferenciados: cambios de juego, cambios de tema de conversación, o "
    "secciones con un propósito distinto (introducción, charla con el "
    "chat, un evento concreto, etc.). Cada capítulo debe marcar un cambio "
    "real de contenido -- nunca generes uno por una simple pausa o un "
    "cambio de frase dentro del mismo tema."
)


def _transcript_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["transcripts"]).resolve() / f"{video_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe la transcripción para '{video_id}': {path}. "
            f"Ejecuta primero la etapa de transcripción (python -m src.transcribe.run --video-id {video_id})."
        )
    return path


def _cuts_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["cuts"]).resolve() / video_id / "cuts.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No existen los cortes para '{video_id}': {path}. "
            f"Ejecuta primero la etapa de detección de cortes (python -m src.detect_cuts.run --video-id {video_id})."
        )
    return path


def _intro_path(video_id: str, config: dict) -> Path:
    return (REPO_ROOT / config["paths"]["output"]).resolve() / video_id / "intro.mp4"


def _probe_duration(path: Path) -> float | None:
    """ffprobe format=duration de `path`, o None si no existe o ffprobe falla."""
    if not path.exists():
        return None
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _format_transcript_for_prompt(transcript: dict) -> str:
    """
    Una línea por segmento de transcript['segments'] (más compacto que
    transcript['words'] -- de sobra para identificar bloques temáticos, y
    evita mandar miles de palabras sueltas a Claude): `[Ns] texto`, con N
    en segundos enteros desde el inicio del vídeo ORIGINAL. Segundos en
    vez de MM:SS para que Claude no tenga que hacer aritmética de reloj
    para producir timestamp_original_s -- puede leer el número
    directamente de la línea que eligió.
    """
    lines: list[str] = []
    for seg in transcript.get("segments", []):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"[{float(seg['start']):.0f}s] {text}")
    return "\n".join(lines)


def _build_prompt(transcript_text: str, duration: float, min_chapter_seconds: float) -> str:
    return (
        f"Transcripción completa de un directo de {duration:.0f} segundos de "
        f"duración (vídeo ORIGINAL, sin editar todavía). Cada línea es "
        f"`[Ns] texto`, donde N son los segundos desde el inicio del vídeo en "
        f"que empieza ese fragmento:\n\n"
        f"{transcript_text}\n\n"
        f"Identifica los bloques temáticos del vídeo y genera un capítulo por "
        f"cada uno, con su timestamp de inicio (en segundos, sobre la línea de "
        f"tiempo ORIGINAL de arriba) y un título breve y descriptivo en "
        f"español (sin emojis, sin numeración delante). Si el vídeo tiene una "
        f"introducción o un tramo inicial claramente distinto antes de que "
        f"empiece el contenido principal, inclúyelo como primer capítulo.\n\n"
        f"Dejar al menos {min_chapter_seconds:.0f} segundos entre el inicio de "
        f"un capítulo y el siguiente: si dos posibles cambios de tema están "
        f"más cerca que eso, quédate solo con el más significativo de los dos "
        f"en vez de generar ambos."
    )


def detect_chapters_with_claude(
    transcript: dict, config: dict, client: "anthropic.Anthropic | None" = None
) -> list[dict]:
    """
    Llama a Claude (config['detect_chapters']['claude_model']) sobre la
    transcripción completa para detectar bloques temáticos.

    `client` es inyectable (por defecto construye un anthropic.Anthropic()
    real) para poder testear el resto de la lógica (parseo de la
    respuesta) con una respuesta simulada, sin llamar a la API real -- ver
    tests/test_detect_chapters.py.

    Returns:
        [{"timestamp_original_s": float, "title": str}, ...] en la línea
        de tiempo del vídeo ORIGINAL, en el orden que devolvió Claude
        (sin ordenar, remapear ni validar todavía -- ver
        remap_chapters_to_edited_timeline). Lista vacía si la
        transcripción no tiene ningún segmento con texto.
    """
    detect_chapters_config = config.get("detect_chapters", {})
    model = detect_chapters_config.get("claude_model", "claude-sonnet-5")
    min_chapter_seconds = float(detect_chapters_config.get("min_chapter_seconds", 120))
    duration = float(
        transcript.get("duration_s")
        or (transcript["segments"][-1]["end"] if transcript.get("segments") else 0.0)
    )

    transcript_text = _format_transcript_for_prompt(transcript)
    if not transcript_text.strip():
        logger.warning("Transcripción vacía; no se detectan capítulos.")
        return []

    if client is None:
        api_key = config.get("_env", {}).get("anthropic_api_key")
        client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    prompt = _build_prompt(transcript_text, duration, min_chapter_seconds)

    logger.info("Analizando transcripción con %s para detectar capítulos...", model)
    response = client.messages.parse(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_format=_ChaptersResponseModel,
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(
            "Claude rechazó la petición de detección de capítulos (stop_reason=refusal)."
        )
    if response.parsed_output is None:
        raise RuntimeError(
            f"Claude no devolvió capítulos estructurados (stop_reason={response.stop_reason})."
        )
    if response.stop_reason == "max_tokens":
        logger.warning(
            "La respuesta de Claude se truncó por max_tokens; los capítulos pueden estar incompletos."
        )

    raw_chapters = [
        {"timestamp_original_s": c.timestamp_original_s, "title": c.title}
        for c in response.parsed_output.chapters
    ]
    logger.info("Claude propuso %d capítulo(s) en bruto (línea de tiempo original).", len(raw_chapters))
    return raw_chapters


def _snap_to_nearest_kept_start(t: float, keep_segments: list[tuple[float, float]]) -> float:
    """
    Si `t` cae DENTRO de un tramo conservado, se devuelve sin cambios. Si
    cae en un hueco (un tramo cortado), se ajusta al `start` del tramo
    conservado más cercano -- comparando la distancia de t al `start` del
    tramo conservado anterior (si existe) contra la distancia al `start`
    del tramo conservado siguiente (si existe), y devolviendo el que esté
    más cerca. Se compara contra el START de ambos lados (no contra el
    final del tramo anterior) para que "más cercano" sea una única
    magnitud consistente en las dos direcciones -- comparar contra el
    final del anterior favorecería sistemáticamente el lado izquierdo
    (esa distancia es siempre menor o igual que la del propio hueco),
    incluso cuando el tramo anterior arranca mucho más lejos que el
    siguiente. En la práctica esto casi siempre resuelve al tramo
    siguiente (su `start` coincide con el punto donde termina el corte,
    que es lo más cerca que puede estar un hueco de contenido conservado
    por ese lado); solo resuelve al tramo anterior si su propio inicio
    queda más cerca de t que el inicio del siguiente tramo conservado.
    """
    prev_start: float | None = None
    next_start: float | None = None

    for start, end in keep_segments:
        if start <= t < end:
            return t
        if end <= t:
            prev_start = start
        elif start > t and next_start is None:
            next_start = start

    candidates: list[tuple[float, float]] = []
    if next_start is not None:
        candidates.append((next_start - t, next_start))
    if prev_start is not None:
        candidates.append((t - prev_start, prev_start))

    if not candidates:
        return t

    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _enforce_min_separation(chapters: list[dict], min_seconds: float) -> list[dict]:
    """
    `chapters` debe venir ordenado por "timestamp_s". Descarta cualquier
    capítulo que quede a menos de min_seconds del ÚLTIMO capítulo
    conservado (no del anterior en la lista original) -- así una racha de
    varios capítulos demasiado juntos colapsa en uno solo, el primero, en
    vez de conservar uno de cada dos.
    """
    if not chapters:
        return []
    kept = [chapters[0]]
    for ch in chapters[1:]:
        if ch["timestamp_s"] - kept[-1]["timestamp_s"] >= min_seconds:
            kept.append(ch)
    return kept


def remap_chapters_to_edited_timeline(
    raw_chapters: list[dict], cuts: list[dict], duration: float, config: dict,
    intro_duration_s: float = 0.0,
) -> list[dict]:
    """
    Convierte `raw_chapters` (timestamps del vídeo ORIGINAL, ver
    detect_chapters_with_claude) a la línea de tiempo YA EDITADA:

    1. Cada timestamp se ajusta primero al tramo conservado más cercano si
       cae dentro de un corte (ver _snap_to_nearest_kept_start).
    2. Se remapea restando la duración acumulada de los cortes anteriores
       (src.common.timeline.map_to_edited_timeline).
    3. Se ordena por tiempo y se aplica config['detect_chapters']
       ['min_chapter_seconds'] como separación mínima REAL, sobre la
       línea de tiempo editada (ver _enforce_min_separation) -- los
       cortes pueden acercar capítulos que en el original estaban bien
       separados.
    4a. Si intro_duration_s > 0 (existe data/output/<video_id>/intro.mp4,
        ver CLAUDE.md "Intro grabado aparte"): TODOS los capítulos de
        arriba se desplazan +intro_duration_s y se antepone un capítulo
        fijo "Introducción" en 0.0 representando ese clip -- 0:00 ya no lo
        disputa ningún capítulo detectado por Claude, lo posee el intro
        real. Se reaplica la separación mínima por si el primer capítulo
        real, tras desplazarse, sigue cayendo demasiado cerca de 0.0.
    4b. Si intro_duration_s == 0.0 (el valor por defecto, sin intro real):
        comportamiento EXACTO de siempre -- se garantiza que el primer
        capítulo quede exactamente en 0.0: si el primero que sobrevive ya
        está prácticamente ahí (ver _FIRST_CHAPTER_EPSILON_SECONDS) se
        fuerza a 0.0 exacto; si no, se antepone uno genérico
        ("Introducción") y se reaplica la separación mínima (el capítulo
        genérico en 0.0 puede a su vez descartar por cercanía al que
        antes era el primero).

    Returns:
        [{"timestamp_s": float, "title": str}, ...] ordenado por tiempo,
        con el primer elemento siempre en 0.0.
    """
    detect_chapters_config = config.get("detect_chapters", {})
    min_seconds = float(detect_chapters_config.get("min_chapter_seconds", 120))

    sorted_cuts = sorted(cuts, key=lambda c: c["start"])
    keep_segments = compute_keep_segments(cuts, duration)

    remapped: list[dict] = []
    for ch in raw_chapters:
        title = str(ch["title"]).strip()
        if not title:
            continue
        original_t = max(0.0, min(float(ch["timestamp_original_s"]), duration))
        snapped_t = _snap_to_nearest_kept_start(original_t, keep_segments)
        edited_t = map_to_edited_timeline(snapped_t, sorted_cuts)
        remapped.append({"timestamp_s": round(edited_t, 3), "title": title})

    remapped.sort(key=lambda c: c["timestamp_s"])
    remapped = _enforce_min_separation(remapped, min_seconds)

    if intro_duration_s > 0:
        shifted = [
            {"timestamp_s": round(c["timestamp_s"] + intro_duration_s, 3), "title": c["title"]}
            for c in remapped
        ]
        with_intro = [{"timestamp_s": 0.0, "title": _GENERIC_INTRO_TITLE}] + shifted
        return _enforce_min_separation(with_intro, min_seconds)

    if remapped and remapped[0]["timestamp_s"] <= _FIRST_CHAPTER_EPSILON_SECONDS:
        remapped[0]["timestamp_s"] = 0.0
    else:
        remapped.insert(0, {"timestamp_s": 0.0, "title": _GENERIC_INTRO_TITLE})
        remapped = _enforce_min_separation(remapped, min_seconds)

    return remapped


def _format_youtube_timestamp(seconds: float, use_hours: bool) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if use_hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _write_chapters_txt(chapters: list[dict], path: Path) -> None:
    """
    `H:MM:SS` para TODOS los capítulos si el vídeo dura una hora o más
    (el último capítulo, tras ordenar, marca la duración relevante),
    `M:SS` si no -- formato consistente en todo el archivo en vez de
    mezclar ambos según cada timestamp individual.
    """
    use_hours = bool(chapters) and chapters[-1]["timestamp_s"] >= 3600
    lines = [
        f"{_format_youtube_timestamp(c['timestamp_s'], use_hours)} {c['title']}"
        for c in chapters
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(video_id: str, config: dict) -> dict:
    """
    Returns:
        dict con {"video_id", "chapters_path", "chapters_txt_path",
                  "chapters": [{"timestamp_s": float, "title": str}, ...]}
    """
    transcript_path = _transcript_path(video_id, config)
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    cuts_path = _cuts_path(video_id, config)
    with open(cuts_path, "r", encoding="utf-8") as f:
        cuts = json.load(f)

    duration = float(
        transcript.get("duration_s")
        or (transcript["segments"][-1]["end"] if transcript.get("segments") else 0.0)
    )

    raw_chapters = detect_chapters_with_claude(transcript, config)

    intro_duration_s = 0.0
    intro_path = _intro_path(video_id, config)
    if intro_path.exists():
        probed = _probe_duration(intro_path)
        if probed is not None and probed > 0:
            intro_duration_s = probed
            logger.info(
                "Intro detectado en %s (%.2fs); se antepondrá como capítulo 'Introducción' en 0:00 y "
                "se desplazarán %.2fs el resto de capítulos.",
                intro_path, intro_duration_s, intro_duration_s,
            )
        else:
            logger.warning(
                "No se pudo determinar la duración de %s; se ignora para los capítulos "
                "(se comporta como si no existiera).",
                intro_path,
            )

    chapters = remap_chapters_to_edited_timeline(raw_chapters, cuts, duration, config, intro_duration_s)

    logger.info(
        "%d capítulo(s) final(es) tras remapeo a la línea de tiempo editada y validación de separación mínima.",
        len(chapters),
    )

    chapters_dir = (REPO_ROOT / config["paths"]["chapters"]).resolve() / video_id
    chapters_dir.mkdir(parents=True, exist_ok=True)
    chapters_json_path = chapters_dir / "chapters.json"
    with open(chapters_json_path, "w", encoding="utf-8") as f:
        json.dump(chapters, f, ensure_ascii=False, indent=2)

    output_dir = (REPO_ROOT / config["paths"]["output"]).resolve() / video_id
    chapters_txt_path = output_dir / "chapters.txt"
    _write_chapters_txt(chapters, chapters_txt_path)

    logger.info("Capítulos guardados en %s y %s", chapters_json_path, chapters_txt_path)

    db.set_status(video_id, "chapters_detected")

    return {
        "video_id": video_id,
        "chapters_path": str(chapters_json_path),
        "chapters_txt_path": str(chapters_txt_path),
        "chapters": chapters,
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Generar capítulos de un vídeo")
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    run(args.video_id, config)


if __name__ == "__main__":
    _cli()
