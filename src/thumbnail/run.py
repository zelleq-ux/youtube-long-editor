"""
Miniatura de YouTube (thumbnail).

Genera data/output/<video_id>/thumbnail.png (1280x720) a partir de FRAMES
REALES del vídeo -- nunca inventando una imagen desde cero. El pipeline:

1. _select_face_frame: elige un frame real de facecam_region con una
   expresión animada. Candidatos: 1-2 puntos dentro de cada uno de los
   primeros config['thumbnail']['face_candidate_segments'] tramos de
   habla continua larga (mismo criterio de agrupación que
   config['edit']['long_speech_min_seconds']/['long_speech_gap_seconds']
   usa en edit/run.py para el zoom hacia la webcam -- reimplementado aquí
   en _group_speech_runs en vez de importarlo directamente de edit/run.py,
   ver CLAUDE.md; un tramo de habla larga es más probable que contenga una
   expresión animada que un instante aleatorio). En cada candidato se
   ejecuta el detector de caras YuNet de src.common.face_detection (el
   mismo que usa detect_intro_face_cut) sobre facecam_region, y se mide
   además cuánto cambia el recorte respecto al frame de ~0.5s antes
   (diferencia media de píxeles en gris); se elige el candidato con cara
   detectada y mayor variación. Si ningún candidato tiene cara detectada
   (caso raro), cae al de mayor variación sin más, con un aviso.

2. _select_gameplay_frame: elige un frame real de la zona de juego (fuera
   de facecam_region) en un momento de alto movimiento. Muestrea
   config['thumbnail']['gameplay_candidate_count'] puntos uniformemente
   repartidos por el vídeo (evitando el primer/último 10%, que suele ser
   intro/despedida) y mide la diferencia media de píxeles en gris respecto
   a ~0.5s antes, EXCLUYENDO facecam_region (misma idea que
   exclude_facecam_from_motion en detect_cuts: el streamer moviéndose no
   cuenta como "acción del juego"). Esto es deliberadamente una versión
   ligera de la detección de movimiento de detect_cuts (diferencia de
   píxeles en vez de optical flow denso Farneback, y solo un puñado de
   candidatos en vez de recorrer el vídeo entero): generar una miniatura
   debe ser cosa de segundos, no de los varios minutos que tarda
   compute_motion_timeseries sobre una grabación de 1-2h, y aquí no aplica
   la misma exigencia de precisión que en detect_cuts (elegir un frame
   "bastante bueno" para una miniatura, no decidir si se pierde contenido
   real de un corte).

3. _extract_headline: analiza la transcripción completa con Claude
   (config['detect_chapters']['claude_model'], mismo modelo que
   detect_chapters, mismo patrón de structured outputs vía
   client.messages.parse) para encontrar el momento más "punchy"/gancho
   del vídeo y proponer un titular corto (3-6 palabras) en español.

4. _compose_thumbnail: composición de imagen normal con Pillow (recorte +
   escalado + pegado, sin IA) -- fondo de gameplay a pantalla completa
   (1280x720), panel de cara con borde en una esquina, y si
   config['thumbnail']['text_rendering'] es "pillow" (por defecto), el
   titular quemado encima con fuente/color/contorno controlados por
   nosotros (Impact o Arial Bold, blanco con contorno negro grueso --
   estilo miniatura clásico). Si es "gemini", el titular NO se quema aquí
   y se le pide a Gemini que lo añada en el paso siguiente.

5. _enhance_with_gemini: SOLO mejora de estilo (contraste, iluminación
   "profesional") sobre la composición ya armada del paso 4 -- se le pasa
   esa imagen completa como entrada junto con un prompt que pide
   explícitamente conservar la composición y el contenido real, nunca
   generar una imagen nueva. Usa el modelo de
   config['thumbnail']['gemini_model'] ("Nano Banana", GEMINI_API_KEY del
   .env) vía el SDK google-genai (client.interactions.create, la API
   documentada para esta familia de modelos -- distinta de
   client.models.generate_content, la API "clásica" de Gemini para texto).
   Si la llamada falla, no devuelve imagen, o GEMINI_API_KEY no está
   configurada, se cae de vuelta a la composición de Pillow sin modificar
   en vez de fallar el módulo entero -- una miniatura sin mejorar sigue
   siendo mejor que ninguna.

config['thumbnail']['enabled'] (por defecto true) permite desactivar el
módulo entero sin tocar código: si es false, run() no hace nada (ni abre
el vídeo, ni llama a ninguna API) y lo deja claro en el log.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
from pathlib import Path
from typing import Callable

import anthropic
import cv2
import numpy as np
from google import genai
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from src.common import db
from src.common.config import REPO_ROOT, load_config
from src.common.face_detection import detect_faces, facecam_crop_box, load_face_detector

logger = logging.getLogger(__name__)

_CANVAS_WIDTH = 1280
_CANVAS_HEIGHT = 720

# Panel de cara: fracción del ancho del canvas + margen/borde -- valores
# fijos de diseño, no configurables (no hay ninguna razón para que varíen
# de un vídeo a otro dentro de este proyecto).
_FACE_PANEL_WIDTH_RATIO = 0.42
_FACE_PANEL_MARGIN = 24
_FACE_PANEL_BORDER = 8
_FACE_PANEL_BORDER_COLOR = (255, 255, 255)

# Titular quemado con Pillow (ver _draw_headline): tamaño de fuente que se
# va reduciendo hasta que quepa en el ancho del canvas, blanco con
# contorno negro grueso -- estilo clásico de miniatura, legible incluso en
# el tamaño diminuto al que YouTube muestra las miniaturas.
_HEADLINE_MAX_FONT_SIZE = 96
_HEADLINE_MIN_FONT_SIZE = 40
_HEADLINE_MARGIN = 36
_HEADLINE_FILL = (255, 255, 255)
_HEADLINE_STROKE = (0, 0, 0)
_HEADLINE_STROKE_WIDTH = 7
_HEADLINE_LINE_SPACING = 8

# Impact es la fuente clásica de miniatura (bold, condensada, muy legible
# a tamaño pequeño); Arial Bold como alternativa si no está instalada.
# ImageFont.load_default(size=...) es el último recurso (fuente bitmap
# básica de Pillow, funciona en cualquier entorno aunque se vea peor).
_FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/impact.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
]

_GAMEPLAY_SAMPLE_MARGIN_RATIO = 0.1  # evita el primer/último 10% del vídeo (intro/despedida)
_MOTION_PROBE_GAP_SECONDS = 0.5  # separación entre el frame candidato y el frame "anterior" para medir variación

_HEADLINE_SYSTEM_PROMPT = (
    "Eres un editor de vídeo que elige el titular corto y llamativo para la "
    "miniatura de YouTube de un directo de gaming en español. Analizas la "
    "transcripción completa y encuentras el momento más punchy o gancho del "
    "vídeo -- una frase o instante que despierte curiosidad, sorpresa o "
    "emoción -- no necesariamente el resumen del vídeo, sino el momento más "
    "llamativo para captar clics."
)

_GEMINI_BASE_PROMPT = (
    "This is a composed YouTube thumbnail built from real screenshots of a "
    "gameplay livestream (a streamer facecam over real gameplay footage). "
    "Enhance it to look like a professional, high-impact YouTube thumbnail: "
    "increase contrast and color saturation, add punchy dramatic lighting, "
    "and make it visually striking. Preserve the existing composition, "
    "layout, and the real people/scenes exactly as shown -- do not invent "
    "new content, do not change who or what is in the image, do not alter "
    "the framing or crop."
)


class _HeadlineModel(BaseModel):
    headline: str


def _raw_video_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["raw"]).resolve() / f"{video_id}.mp4"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe el vídeo de entrada para '{video_id}': {path}. "
            "Ejecuta primero la etapa de ingesta (python -m src.ingest.run --file <ruta_al_mp4_de_obs>)."
        )
    return path


def _transcript_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["transcripts"]).resolve() / f"{video_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe la transcripción para '{video_id}': {path}. "
            f"Ejecuta primero la etapa de transcripción (python -m src.transcribe.run --video-id {video_id})."
        )
    return path


def _output_dir(video_id: str, config: dict) -> Path:
    out_dir = (REPO_ROOT / config["paths"]["output"]).resolve() / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _load_cut_intervals(video_id: str, config: dict) -> list[tuple[float, float]]:
    """
    Tramos ya marcados para eliminar en data/cuts/<video_id>/cuts.json
    (silencio, muletilla, o la intro sin cara -- ver CLAUDE.md), si esa
    etapa ya se ha ejecutado para este vídeo. Se usan para descartar
    candidatos de frame que caigan ahí -- en particular el corte de
    intro, que puede solapar con tramos de habla real (la intro no
    depende de silencio) pero durante el cual facecam_region no
    corresponde todavía a la disposición normal de pantalla (p.ej.
    webcam a pantalla completa antes de la disposición de juego, ver
    CLAUDE.md/detect_intro_face_cut) -- un candidato ahí produce un
    recorte de cara en blanco o irreconocible. Lista vacía si
    detect_cuts no se ha ejecutado todavía: no bloquea la generación de
    miniatura, solo no filtra nada.
    """
    cuts_dir = config.get("paths", {}).get("cuts")
    if not cuts_dir:
        return []
    path = (REPO_ROOT / cuts_dir).resolve() / video_id / "cuts.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        cuts = json.load(f)
    return [(float(c["start"]), float(c["end"])) for c in cuts]


def _in_any_interval(t: float, intervals: list[tuple[float, float]]) -> bool:
    return any(start <= t < end for start, end in intervals)


def _format_transcript_for_prompt(transcript: dict) -> str:
    """
    Una línea por segmento: `[Ns] texto`, con N en segundos enteros desde
    el inicio del vídeo -- mismo formato que usa detect_chapters
    (reimplementado aquí en vez de importarlo, ver CLAUDE.md: es una
    función de formateo trivial, no lógica de negocio sustancial).
    """
    lines: list[str] = []
    for seg in transcript.get("segments", []):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"[{float(seg['start']):.0f}s] {text}")
    return "\n".join(lines)


def _group_speech_runs(transcript: dict, config: dict) -> list[tuple[float, float]]:
    """
    Agrupa palabras consecutivas de transcript['words'] cuyo hueco es
    menor que config['edit']['long_speech_gap_seconds'], devolviendo los
    tramos (línea de tiempo ORIGINAL) cuya duración es >=
    config['edit']['long_speech_min_seconds'] -- mismo criterio que usa
    edit/run.py para el zoom hacia la webcam (reimplementado aquí en vez
    de importado, ver CLAUDE.md), reutilizado para muestrear candidatos a
    frame de cara.
    """
    words = transcript.get("words", [])
    if not words:
        return []

    edit_config = config.get("edit", {})
    gap_threshold = float(edit_config.get("long_speech_gap_seconds", 1.2))
    min_seconds = float(edit_config.get("long_speech_min_seconds", 10.0))

    runs: list[tuple[float, float]] = []
    run_start = float(words[0]["start"])
    run_end = float(words[0]["end"])
    for prev_word, word in zip(words, words[1:]):
        gap = float(word["start"]) - float(prev_word["end"])
        if gap <= gap_threshold:
            run_end = float(word["end"])
        else:
            if run_end - run_start >= min_seconds:
                runs.append((run_start, run_end))
            run_start = float(word["start"])
            run_end = float(word["end"])
    if run_end - run_start >= min_seconds:
        runs.append((run_start, run_end))
    return runs


def _select_face_frame(
    video_id: str,
    config: dict,
    detect_faces_at: "Callable[[np.ndarray, tuple[int, int, int, int]], np.ndarray | None] | None" = None,
) -> np.ndarray:
    """
    Devuelve un frame completo (BGR, tal cual lo da OpenCV) de
    data/raw/<video_id>.mp4 elegido por tener una cara bien detectada en
    facecam_region y, entre los que la tienen, la mayor variación respecto
    al frame ~_MOTION_PROBE_GAP_SECONDS antes (expresión más animada).

    `detect_faces_at` es inyectable (por defecto None, construye el
    detector YuNet real de src.common.face_detection sobre facecam_region)
    -- mismo patrón que el parámetro `detector` de detect_intro_face_cut
    en detect_cuts/run.py, para poder testear la lógica de selección de
    candidatos (puntuación por confianza + variación, fallback sin cara
    detectada) con un detector simulado, sin depender de que YuNet
    reconozca un marcador sintético dibujado a mano.

    Si data/cuts/<video_id>/cuts.json ya existe, los tramos de habla larga
    cuyo inicio caiga dentro de un corte ya detectado (típicamente la
    intro sin cara -- ver _load_cut_intervals) se descartan antes de
    elegir los primeros face_candidate_segments: encontrado con una
    generación real contra dinoblade_1, cuya intro (~17 min sin
    facecam_region en su disposición normal) contiene varios tramos de
    habla real y producía candidatos con la cara fuera de encuadre.
    """
    facecam_region = config.get("facecam_region")
    if not facecam_region:
        raise ValueError("config['facecam_region'] no está definido; hace falta para elegir el frame de cara.")

    transcript_path = _transcript_path(video_id, config)
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    n_segments = int(config.get("thumbnail", {}).get("face_candidate_segments", 3))
    all_runs = _group_speech_runs(transcript, config)
    cut_intervals = _load_cut_intervals(video_id, config)
    usable_runs = [r for r in all_runs if not _in_any_interval(r[0], cut_intervals)] if cut_intervals else all_runs
    if cut_intervals and not usable_runs:
        logger.warning(
            "Todos los tramos de habla larga de '%s' caen dentro de un corte ya detectado (p.ej. la intro); "
            "se ignora ese filtro para no quedarnos sin candidatos.",
            video_id,
        )
        usable_runs = all_runs
    runs = usable_runs[:n_segments]

    candidate_times: list[float] = []
    for start, end in runs:
        dur = end - start
        candidate_times.append(start + dur * 0.35)
        candidate_times.append(start + dur * 0.7)

    input_path = _raw_video_path(video_id, config)
    cap = cv2.VideoCapture(str(input_path), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir {input_path} para extraer el frame de cara.")

    try:
        if not candidate_times:
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            duration = total_frames / fps if fps > 0 else 0.0
            logger.warning(
                "No se detectó ningún tramo de habla larga para '%s'; se usa el punto medio del vídeo "
                "como único candidato de frame de cara.",
                video_id,
            )
            candidate_times = [duration / 2.0]

        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        crop_box = facecam_crop_box(facecam_region, frame_width, frame_height)
        x0, y0, x1, y1 = crop_box
        crop_size = (x1 - x0, y1 - y0)
        if detect_faces_at is None:
            detector = load_face_detector(crop_size)
            detect_faces_at = lambda frame, box: detect_faces(frame, box, detector)  # noqa: E731

        best_frame, best_score = None, -1.0
        fallback_frame, fallback_score = None, -1.0

        for t in candidate_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, (t - _MOTION_PROBE_GAP_SECONDS)) * 1000)
            ok_prev, frame_prev = cap.read()
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue

            motion = 0.0
            if ok_prev:
                gray_now = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
                gray_prev = cv2.cvtColor(frame_prev[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
                if gray_now.shape == gray_prev.shape:
                    motion = float(cv2.absdiff(gray_now, gray_prev).mean())

            if motion > fallback_score:
                fallback_score = motion
                fallback_frame = frame

            faces = detect_faces_at(frame, crop_box)
            if faces is not None and len(faces) > 0:
                confidence = float(faces[:, -1].max())
                score = confidence + motion / 255.0
                if score > best_score:
                    best_score = score
                    best_frame = frame

        if best_frame is None:
            logger.warning(
                "Ningún candidato de '%s' tuvo una cara detectada con confianza en facecam_region; "
                "se usa el de mayor variación en su lugar.",
                video_id,
            )
            best_frame = fallback_frame
        if best_frame is None:
            raise RuntimeError(f"No se pudo extraer ningún frame candidato de facecam_region para '{video_id}'.")
        return best_frame
    finally:
        cap.release()


def _select_gameplay_frame(video_id: str, config: dict) -> np.ndarray:
    """
    Devuelve un frame completo (BGR) de data/raw/<video_id>.mp4 elegido
    por tener la mayor variación de píxeles respecto al frame
    ~_MOTION_PROBE_GAP_SECONDS antes, EXCLUYENDO facecam_region (ver
    docstring del módulo para el porqué de esta versión ligera de
    detección de movimiento en vez de reutilizar compute_motion_timeseries
    de detect_cuts).
    """
    facecam_region = config.get("facecam_region")
    input_path = _raw_video_path(video_id, config)
    cap = cv2.VideoCapture(str(input_path), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir {input_path} para extraer el frame de gameplay.")

    try:
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration = total_frames / fps if fps > 0 else 0.0
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        exclude_box = facecam_crop_box(facecam_region, frame_width, frame_height) if facecam_region else None

        n = max(1, int(config.get("thumbnail", {}).get("gameplay_candidate_count", 24)))
        lo = duration * _GAMEPLAY_SAMPLE_MARGIN_RATIO
        hi = duration * (1 - _GAMEPLAY_SAMPLE_MARGIN_RATIO)
        if duration <= 0 or hi <= lo:
            candidate_times = [0.0]
        elif n == 1:
            candidate_times = [(lo + hi) / 2]
        else:
            candidate_times = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

        cut_intervals = _load_cut_intervals(video_id, config)
        if cut_intervals:
            filtered = [t for t in candidate_times if not _in_any_interval(t, cut_intervals)]
            candidate_times = filtered or candidate_times

        mask: np.ndarray | None = None
        best_frame, best_score = None, -1.0

        for t in candidate_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, (t - _MOTION_PROBE_GAP_SECONDS)) * 1000)
            ok_a, frame_a = cap.read()
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
            ok_b, frame_b = cap.read()
            if not (ok_a and ok_b):
                continue

            gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
            gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)
            if gray_a.shape != gray_b.shape:
                continue
            diff = cv2.absdiff(gray_a, gray_b)

            if mask is None:
                mask = np.ones(diff.shape, dtype=bool)
                if exclude_box:
                    ex0, ey0, ex1, ey1 = exclude_box
                    mask[ey0:ey1, ex0:ex1] = False

            score = float(diff[mask].mean())
            if score > best_score:
                best_score = score
                best_frame = frame_b

        if best_frame is None:
            raise RuntimeError(f"No se pudo extraer ningún frame candidato de gameplay para '{video_id}'.")
        return best_frame
    finally:
        cap.release()


def _extract_headline(
    transcript: dict, config: dict, client: "anthropic.Anthropic | None" = None
) -> str:
    """
    Llama a Claude (config['detect_chapters']['claude_model']) sobre la
    transcripción completa para encontrar el momento más punchy/gancho y
    proponer un titular corto de miniatura. `client` es inyectable (mismo
    patrón que detect_chapters_with_claude) para testear sin red.
    """
    detect_chapters_config = config.get("detect_chapters", {})
    model = detect_chapters_config.get("claude_model", "claude-sonnet-5")

    transcript_text = _format_transcript_for_prompt(transcript)
    if not transcript_text.strip():
        logger.warning("Transcripción vacía; se usa un titular genérico de respaldo.")
        return "Directo en vivo"

    if client is None:
        api_key = config.get("_env", {}).get("anthropic_api_key")
        client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    prompt = (
        f"Transcripción completa de un directo:\n\n{transcript_text}\n\n"
        "Elige el momento más punchy o gancho del vídeo y genera un titular "
        "corto (3-6 palabras) tipo miniatura de YouTube en español, en el "
        "estilo habitual de un titular llamativo (sin punto final, sin "
        "comillas)."
    )

    logger.info("Analizando transcripción con %s para extraer el titular...", model)
    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=_HEADLINE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_format=_HeadlineModel,
    )

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude rechazó la petición de extracción del titular (stop_reason=refusal).")
    if response.parsed_output is None:
        raise RuntimeError(f"Claude no devolvió un titular estructurado (stop_reason={response.stop_reason}).")

    return response.parsed_output.headline.strip()


def _bgr_to_pil(frame: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def _cover_resize(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Escala `image` para CUBRIR target_w x target_h recortando el sobrante, sin deformar el aspect ratio."""
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _load_headline_font(size: int) -> "ImageFont.ImageFont":
    for path in _FONT_CANDIDATES:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _wrap_headline(text: str) -> list[str]:
    """Como mucho 2 líneas: si el titular es de 1-2 palabras se deja en una, si no se parte por la mitad de palabras."""
    words = text.split()
    if len(words) <= 2:
        return [text]
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def _fit_headline_font(draw: "ImageDraw.ImageDraw", lines: list[str], max_width: int) -> "ImageFont.ImageFont":
    size = _HEADLINE_MAX_FONT_SIZE
    while size > _HEADLINE_MIN_FONT_SIZE:
        font = _load_headline_font(size)
        widest = max(
            draw.textbbox((0, 0), line, font=font, stroke_width=_HEADLINE_STROKE_WIDTH)[2] for line in lines
        )
        if widest <= max_width:
            return font
        size -= 4
    return _load_headline_font(_HEADLINE_MIN_FONT_SIZE)


def _draw_headline(canvas: Image.Image, headline: str) -> None:
    """Titular en la parte superior del canvas, blanco con contorno negro, centrado -- ver constantes _HEADLINE_*."""
    draw = ImageDraw.Draw(canvas)
    lines = _wrap_headline(headline.upper())
    max_width = _CANVAS_WIDTH - 2 * _HEADLINE_MARGIN
    font = _fit_headline_font(draw, lines, max_width)
    line_height = draw.textbbox((0, 0), "Ay", font=font, stroke_width=_HEADLINE_STROKE_WIDTH)[3]

    y = _HEADLINE_MARGIN
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=_HEADLINE_STROKE_WIDTH)
        line_width = bbox[2] - bbox[0]
        x = (_CANVAS_WIDTH - line_width) // 2
        draw.text(
            (x, y), line, font=font, fill=_HEADLINE_FILL,
            stroke_width=_HEADLINE_STROKE_WIDTH, stroke_fill=_HEADLINE_STROKE,
        )
        y += line_height + _HEADLINE_LINE_SPACING


def _compose_thumbnail(
    face_frame_bgr: np.ndarray,
    gameplay_frame_bgr: np.ndarray,
    facecam_region: dict,
    headline: str,
    burn_text: bool,
) -> Image.Image:
    """
    Composición de imagen normal con Pillow (sin IA): fondo de gameplay a
    pantalla completa, panel de cara con borde blanco en la esquina
    inferior derecha, y opcionalmente el titular quemado arriba (ver
    burn_text -- controlado por config['thumbnail']['text_rendering']).
    """
    canvas = _cover_resize(_bgr_to_pil(gameplay_frame_bgr), _CANVAS_WIDTH, _CANVAS_HEIGHT)

    fh, fw = face_frame_bgr.shape[:2]
    x0, y0, x1, y1 = facecam_crop_box(facecam_region, fw, fh)
    face_crop = _bgr_to_pil(face_frame_bgr[y0:y1, x0:x1])

    panel_w = round(_CANVAS_WIDTH * _FACE_PANEL_WIDTH_RATIO)
    panel_h = max(1, round(panel_w * face_crop.height / face_crop.width))
    face_panel = face_crop.resize((panel_w, panel_h), Image.LANCZOS)

    panel_x = _CANVAS_WIDTH - _FACE_PANEL_MARGIN - panel_w
    panel_y = _CANVAS_HEIGHT - _FACE_PANEL_MARGIN - panel_h

    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        (
            panel_x - _FACE_PANEL_BORDER, panel_y - _FACE_PANEL_BORDER,
            panel_x + panel_w + _FACE_PANEL_BORDER, panel_y + panel_h + _FACE_PANEL_BORDER,
        ),
        fill=_FACE_PANEL_BORDER_COLOR,
    )
    canvas.paste(face_panel, (panel_x, panel_y))

    if burn_text:
        _draw_headline(canvas, headline)

    return canvas


def _build_gemini_prompt(headline: str, text_rendering: str) -> str:
    if text_rendering == "gemini":
        return (
            _GEMINI_BASE_PROMPT
            + f' Also add the bold, high-contrast headline text "{headline}" as large, punchy '
            "thumbnail-style typography (e.g. white text with a thick black outline), positioned "
            "so it does not cover the face."
        )
    return _GEMINI_BASE_PROMPT + " Keep the existing text exactly as it is -- do not modify, move, or remove it."


def _enhance_with_gemini(
    image: Image.Image, headline: str, config: dict, client: "genai.Client | None" = None
) -> tuple[Image.Image, bool]:
    """
    Mejora de estilo SOLO (contraste/iluminación) sobre `image`, ya
    compuesta -- nunca genera una imagen nueva desde cero. Devuelve
    (imagen_resultante, mejorada): si GEMINI_API_KEY no está configurada,
    la llamada falla, o no devuelve ninguna imagen utilizable, se cae de
    vuelta a `image` sin modificar (mejorada=False) en vez de fallar el
    módulo entero.
    """
    thumbnail_config = config.get("thumbnail", {})
    model = thumbnail_config.get("gemini_model", "gemini-3.1-flash-image")
    text_rendering = thumbnail_config.get("text_rendering", "pillow")

    if client is None:
        api_key = config.get("_env", {}).get("gemini_api_key")
        if not api_key:
            logger.warning(
                "GEMINI_API_KEY no configurada; se omite la mejora de estilo y se usa la composición de "
                "Pillow tal cual."
            )
            return image, False
        client = genai.Client(api_key=api_key)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_bytes = buf.getvalue()
    prompt = _build_gemini_prompt(headline, text_rendering)

    logger.info("Mejorando estilo de la miniatura con Gemini (%s)...", model)
    try:
        interaction = client.interactions.create(
            model=model,
            input=[
                {"type": "text", "text": prompt},
                {"type": "image", "data": base64.b64encode(image_bytes).decode("utf-8"), "mime_type": "image/png"},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de Gemini no debe tumbar la miniatura entera
        logger.warning("La llamada a Gemini falló (%s); se usa la composición de Pillow sin mejorar.", exc)
        return image, False

    output_image = getattr(interaction, "output_image", None)
    output_data = getattr(output_image, "data", None) if output_image is not None else None
    if output_data is None:
        logger.warning("Gemini no devolvió ninguna imagen; se usa la composición de Pillow sin mejorar.")
        return image, False

    try:
        result_image = Image.open(io.BytesIO(base64.b64decode(output_data))).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - una respuesta indecodificable no debe tumbar la miniatura entera
        logger.warning("No se pudo decodificar la imagen devuelta por Gemini (%s); se usa la de Pillow.", exc)
        return image, False

    return result_image, True


def _fit_canvas(image: Image.Image) -> Image.Image:
    """Fuerza exactamente _CANVAS_WIDTH x _CANVAS_HEIGHT pase lo que devuelva Gemini (puede cambiar de tamaño)."""
    image = image.convert("RGB")
    if image.size == (_CANVAS_WIDTH, _CANVAS_HEIGHT):
        return image
    return _cover_resize(image, _CANVAS_WIDTH, _CANVAS_HEIGHT)


def run(video_id: str, config: dict) -> dict:
    """
    Returns:
        dict con {"video_id", "thumbnail_path", "headline",
                  "enhanced_with_gemini": bool}. thumbnail_path/headline
        son None si config['thumbnail']['enabled'] es false.
    """
    thumbnail_config = config.get("thumbnail", {})
    if not thumbnail_config.get("enabled", True):
        logger.info(
            "config['thumbnail']['enabled'] es false; no se genera ninguna miniatura para '%s'.", video_id
        )
        return {"video_id": video_id, "thumbnail_path": None, "headline": None, "enhanced_with_gemini": False}

    logger.info("Seleccionando frame de cara real para '%s'...", video_id)
    face_frame = _select_face_frame(video_id, config)

    logger.info("Seleccionando frame de gameplay real para '%s'...", video_id)
    gameplay_frame = _select_gameplay_frame(video_id, config)

    transcript_path = _transcript_path(video_id, config)
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    headline = _extract_headline(transcript, config)
    logger.info("Titular elegido: %r", headline)

    text_rendering = thumbnail_config.get("text_rendering", "pillow")
    composed = _compose_thumbnail(
        face_frame, gameplay_frame, config["facecam_region"], headline, burn_text=(text_rendering == "pillow")
    )

    final_image, enhanced = _enhance_with_gemini(composed, headline, config)
    final_image = _fit_canvas(final_image)

    output_dir = _output_dir(video_id, config)
    thumbnail_path = output_dir / "thumbnail.png"
    final_image.save(thumbnail_path, format="PNG")
    logger.info("Miniatura guardada en %s (mejorada con Gemini: %s)", thumbnail_path, enhanced)

    db.set_status(video_id, "thumbnail_generated")

    return {
        "video_id": video_id,
        "thumbnail_path": str(thumbnail_path),
        "headline": headline,
        "enhanced_with_gemini": enhanced,
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Generar la miniatura de un vídeo")
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
