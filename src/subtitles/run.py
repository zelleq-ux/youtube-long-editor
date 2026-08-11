"""
Etapa nueva: generación de subtítulos (.srt) para el vídeo largo ya editado.

Genera data/output/<video_id>/subtitles.srt a partir de
data/transcripts/<video_id>.json (timestamps por palabra, línea de tiempo
ORIGINAL) y data/cuts/<video_id>/cuts.json, siguiendo el mismo patrón de
remapeo que ya usan edit/ y detect_chapters/ (src.common.timeline).

Descarte/ajuste de palabras cortadas: una palabra cuyo intervalo
[start, end] ORIGINAL solapa con CUALQUIER corte de cuts.json (aunque sea
parcialmente -- p.ej. un margen de corte impreciso que se come el borde de
una palabra) se DESCARTA por completo en vez de recortarse: su audio ya no
suena entero en el vídeo final, así que no tiene sentido mostrarla como
subtítulo. Las palabras que sobreviven se remapean con
map_to_edited_timeline (misma función que usan el zoom hacia la webcam de
edit/ y los timestamps de capítulos de detect_chapters/).

Agrupado en líneas de subtítulo (estilo estático, sin animación -- estándar
de la industria para contenido largo):

- Máximo config['subtitles']['max_chars_per_line'] (42) caracteres por
  línea y config['subtitles']['max_lines'] (2) líneas por subtítulo -- el
  presupuesto total de caracteres de un subtítulo es el producto de ambos
  (84 con los valores por defecto).
- Cada subtítulo dura entre config['subtitles']['min_cue_seconds'] (1s) y
  config['subtitles']['max_cue_seconds'] (6s) en pantalla.
- Los cortes de subtítulo (dónde empieza uno y termina el anterior) están
  gobernados por el presupuesto de caracteres y de duración; cuando hace
  falta cortar, se PREFIERE hacerlo en una pausa natural (puntuación de
  cierre de frase/cláusula, o un hueco largo entre el fin de una palabra y
  el inicio de la siguiente >= config['subtitles']['natural_pause_gap_seconds'])
  en vez de en un punto arbitrario -- ver _group_words_into_cues, que
  recuerda la última pausa natural vista dentro del grupo que se está
  formando y corta ahí en vez de justo en la palabra que desborda el
  presupuesto, siempre que esa pausa no sea el propio inicio del grupo.
- Ritmo de lectura de referencia ~15-20 caracteres/segundo
  (config['subtitles']['reading_cps_min']/['reading_cps_max']): el límite
  SUPERIOR (20 cps) se usa para calcular un SUELO de duración cuando el
  tramo de habla real es más corto que lo que tardaría un espectador en
  leer el texto completo (evita subtítulos que parpadean demasiado rápido
  para textos cortos con timestamps muy juntos); el límite INFERIOR (15
  cps) no se aplica como techo de duración -- alargar un subtítulo más
  allá de lo que dura el habla real para "no leer demasiado rápido" haría
  que el subtítulo siguiera en pantalla después de que la persona ya haya
  dicho otra cosa, así que la duración máxima real la marca siempre
  max_cue_seconds (6s) o el inicio del siguiente subtítulo, lo que llegue
  antes. Con el presupuesto de caracteres por defecto (84) y reading_cps_max
  (20), el suelo de lectura (84/20 = 4.2s) queda siempre por debajo de
  max_cue_seconds (6s), así que ambos límites nunca entran en conflicto con
  la configuración por defecto.

Formato de salida: .srt estándar (numeración secuencial desde 1,
timestamps HH:MM:SS,mmm).

config['subtitles']['enabled'] (default true) desactiva el módulo entero
sin tocar código -- run() no hace nada y lo deja claro en el log, igual que
config['thumbnail']['enabled'].

Calibración contra el vídeo final real (2026-08-09, bug encontrado en
revisión manual de dinoblade_1): map_to_edited_timeline asume que cada
corte de cuts.json elimina EXACTAMENTE `end - start` segundos, pero
edit/run.py no corta así de exacto -- su "renderizado parcial sin
pérdida" (ver CLAUDE.md/status.md) recorta la cabeza/cola de cada tramo
conservado con `-ss`/`-to` re-codificando, que redondea al frame de vídeo
más cercano; ese redondeo es pequeño por corte (menos de un frame) pero
va SIEMPRE en la misma dirección ("MÁS contenido conservado, nunca
menos", ya documentado en la fila `edit` de status.md) y se ACUMULA con
cada corte adicional. Con pocos cortes (como en el test sintético) es
inapreciable; con los 146 cortes reales de dinoblade_1 crece hasta ~5.3s
al final del vídeo -- confirmado retranscribiendo con faster-whisper tres
ventanas reales de `final.mp4` y comparando contra los cues ya generados:
la palabra reportada por el .srt SIN calibrar aparecía sistemáticamente
~2-4s ANTES de cuando de verdad se pronuncia, y la magnitud del desfase
correlaciona con el Nº DE CORTES ya pasados en ese punto (no con el
tiempo transcurrido -- un modelo proporcional-al-tiempo ajustaba peor a
los mismos tres puntos de control).

Como cuts.json no sabe nada de este redondeo (es puramente aritmético) y
relanzar edit/run.py para obtener los límites exactos de cada tramo sale
caro (recodificar de nuevo toda la grabación), la corrección se calibra
con el ÚNICO dato que sí se puede medir sin volver a cortar nada:
la diferencia entre la duración NOMINAL del contenido principal (según
cuts.json) y la duración REAL del contenido principal en
data/output/<video_id>/final.mp4 (si ya existe -- restando la duración
del outro si `config['edit']['append_outro']` está activo), repartida a
partes iguales entre los cortes (`drift_per_cut` = diferencia total /
nº de cortes) y sumada según cuántos cortes ha pasado ya cada palabra
(ver _calibrated_edited_timestamp). Es una aproximación LINEAL, no
frame-perfecta (el redondeo real de cada corte varía un poco), pero
reduce el desfase medido en los tres puntos de control de ~2-4s a
~0.1-0.6s -- una mejora de un orden de magnitud, suficiente para que el
subtítulo ya no se perciba desincronizado. Si final.mp4 todavía no
existe (subtítulos ejecutado antes que edit/, o append_outro sin outro
real) se cae de vuelta al remapeo SIN calibrar (drift_per_cut=0.0) en vez
de fallar -- ver _compute_drift_per_cut.

Intro grabado aparte (2026-08-10, ver CLAUDE.md "Intro grabado aparte"):
si existe data/output/<video_id>/intro.mp4, se transcribe TAMBIÉN (con
src.common.transcription.transcribe_file, el mismo núcleo que usa
transcribe/run.py -- ver ese módulo para el porqué de compartirlo en vez
de importar directamente entre etapas) y sus palabras entran en el .srt
con sus propios timestamps, DESDE 0:00 -- el intro no pasa por cuts.json
(se usa completo, tal cual) así que sus palabras no necesitan filtrarse ni
remapearse, solo limpiarse igual que las del contenido principal. No se
persiste a disco (a diferencia de data/transcripts/<video_id>.json): un
intro de 1-2 min se retranscribe en segundos con el modelo `tiny`/`small`
en CPU, así que cachearlo no compensa la complejidad de invalidar la
caché si el usuario cambia intro.mp4.

Todas las palabras del contenido PRINCIPAL se desplazan +intro_duration_s
(además de la calibración de deriva de arriba, que sigue aplicándose
igual): ese desplazamiento se ancla a la duración REAL del vídeo final,
no a una estimación aislada -- compute_drift_per_cut resta
intro_duration_s de final.mp4 exactamente igual que ya resta la duración
del outro (ver más arriba) antes de calcular la deriva, así que cualquier
imprecisión de intro_duration_s (p.ej. redondeo de frames si el intro se
tuvo que recodificar a otro fps en edit/, ver prepend_intro) se absorbe
en la misma corrección lineal repartida entre cortes, en vez de quedar
como un desplazamiento fijo sin corregir. intro_duration_s en sí se lee
con ffprobe directamente de data/output/<video_id>/intro.mp4 (duración
NOMINAL del archivo tal y como lo entregó el usuario) -- no depende de
que edit/ ya se haya ejecutado, igual que el resto de este módulo ya
asume que final.mp4 puede no existir todavía.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

from src.common import db
from src.common.config import REPO_ROOT, load_config
from src.common.timeline import map_to_edited_timeline, merge_short_kept_segments
from src.common.transcription import transcribe_file

logger = logging.getLogger(__name__)

# Umbral de sanidad para _compute_drift_per_cut: si la deriva total medida
# (real - nominal) supera esta fracción de la duración nominal, algo va
# mal (final.mp4 de otro cuts.json, corrupto, etc.) y es más seguro NO
# calibrar que confiar en un número disparatado -- el redondeo de frames
# esperado es de un orden de magnitud MUY por debajo de esto (~0.1% en
# dinoblade_1).
_MAX_PLAUSIBLE_DRIFT_FRACTION = 0.05

_DEFAULT_MAX_CHARS_PER_LINE = 42
_DEFAULT_MAX_LINES = 2
_DEFAULT_MIN_CUE_SECONDS = 1.0
_DEFAULT_MAX_CUE_SECONDS = 6.0
_DEFAULT_READING_CPS_MIN = 15.0
_DEFAULT_READING_CPS_MAX = 20.0
_DEFAULT_NATURAL_PAUSE_GAP_SECONDS = 0.5

# Puntuación que marca el final de una frase (pausa natural "fuerte") o de
# una cláusula (pausa natural "débil" -- también preferible a cortar en
# mitad de una frase, aunque marque una pausa más corta que un punto).
_SENTENCE_BREAK_CHARS = (".", "!", "?", "…")
_CLAUSE_BREAK_CHARS = (",", ";", ":")
_NATURAL_BREAK_CHARS = _SENTENCE_BREAK_CHARS + _CLAUSE_BREAK_CHARS


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


def _prepare_intro_words(transcript_words: list[dict]) -> list[dict]:
    """
    Convierte transcript['words'] (contrato de transcribe_file/transcribe_file,
    timestamps YA 0-based del propio intro.mp4 -- no pasa por cuts.json,
    así que no hace falta filtrar solapes ni remapear) al mismo formato
    {"text", "start", "end"} que produce filter_and_remap_words, para poder
    mezclarlas directamente con las palabras del contenido principal.
    """
    result: list[dict] = []
    for w in transcript_words:
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        start = float(w["start"])
        end = float(w["end"])
        if end <= start:
            continue
        result.append({"text": text, "start": start, "end": end})
    return result


def _word_overlaps_any_cut(start: float, end: float, sorted_cuts: list[dict]) -> bool:
    """`sorted_cuts` debe estar ordenado por "start"."""
    for c in sorted_cuts:
        if c["start"] >= end:
            break
        if c["end"] > start:
            return True
    return False


def _calibrated_edited_timestamp(t: float, sorted_cuts: list[dict], drift_per_cut: float) -> float:
    """
    map_to_edited_timeline(t, sorted_cuts) más una corrección lineal de
    `drift_per_cut` segundos por cada corte ya pasado (c["start"] < t) --
    ver "Calibración contra el vídeo final real" en el docstring del
    módulo. Con drift_per_cut=0.0 (el valor por defecto cuando no hay
    final.mp4 real contra el que calibrar) se comporta exactamente igual
    que map_to_edited_timeline sin calibrar.
    """
    nominal = map_to_edited_timeline(t, sorted_cuts)
    if drift_per_cut == 0.0:
        return nominal
    cuts_passed = sum(1 for c in sorted_cuts if c["start"] < t)
    return max(0.0, nominal + cuts_passed * drift_per_cut)


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


def compute_drift_per_cut(
    video_id: str, config: dict, sorted_cuts: list[dict], nominal_main_duration: float,
    intro_duration_s: float = 0.0,
) -> float:
    """
    Deriva (segundos) que hay que sumar por cada corte ya pasado para que
    el remapeo coincida con la duración REAL del contenido principal en
    data/output/<video_id>/final.mp4, en vez de con la duración NOMINAL
    que predice la aritmética de cuts.json -- ver "Calibración contra el
    vídeo final real" en el docstring del módulo para el porqué.

    `intro_duration_s` (ver "Intro grabado aparte" en el docstring del
    módulo) se resta de final_duration exactamente igual que la duración
    del outro: si hay intro, ocupa una porción de final.mp4 que no es
    "contenido principal" y que la aritmética de cuts.json tampoco conoce,
    así que hay que descontarla antes de comparar duración nominal vs.
    real -- de lo contrario esa porción entera se interpretaría (mal) como
    deriva de redondeo de cortes.

    Devuelve 0.0 (sin calibrar, mismo comportamiento que antes de este
    fix) si: no hay cortes, no existe final.mp4 todavía, no se puede
    determinar su duración, o la deriva medida es implausiblemente grande
    (ver _MAX_PLAUSIBLE_DRIFT_FRACTION) -- en cualquiera de estos casos es
    más seguro remapear sin calibrar que confiar en un número erróneo.
    """
    if not sorted_cuts or nominal_main_duration <= 0:
        return 0.0

    final_path = (REPO_ROOT / config["paths"]["output"]).resolve() / video_id / "final.mp4"
    final_duration = _probe_duration(final_path)
    if final_duration is None:
        logger.info(
            "No se encontró %s (o no se pudo leer su duración); los subtítulos se generan SIN "
            "calibrar contra la duración real del vídeo final (ver docstring del módulo).",
            final_path,
        )
        return 0.0

    outro_duration = 0.0
    edit_config = config.get("edit", {})
    if edit_config.get("append_outro", True):
        outro_path_str = config.get("paths", {}).get("outro")
        if outro_path_str:
            probed_outro = _probe_duration((REPO_ROOT / outro_path_str).resolve())
            if probed_outro is not None:
                outro_duration = probed_outro

    real_main_duration = final_duration - outro_duration - intro_duration_s
    total_drift = real_main_duration - nominal_main_duration

    if abs(total_drift) > nominal_main_duration * _MAX_PLAUSIBLE_DRIFT_FRACTION:
        logger.warning(
            "La deriva medida entre final.mp4 (%.3fs de contenido principal) y cuts.json "
            "(%.3fs nominales) es implausiblemente grande (%.3fs); se generan los subtítulos SIN "
            "calibrar -- probablemente final.mp4 no corresponde a este cuts.json.",
            real_main_duration, nominal_main_duration, total_drift,
        )
        return 0.0

    drift_per_cut = total_drift / len(sorted_cuts)
    logger.info(
        "Calibrando remapeo de subtítulos contra %s: %.3fs nominales vs. %.3fs reales de contenido "
        "principal (%+.3fs acumulados en %d corte(s), %.4fs/corte; intro_duration_s=%.3fs).",
        final_path.name, nominal_main_duration, real_main_duration, total_drift,
        len(sorted_cuts), drift_per_cut, intro_duration_s,
    )
    return drift_per_cut


def filter_and_remap_words(
    words: list[dict], sorted_cuts: list[dict], drift_per_cut: float = 0.0
) -> list[dict]:
    """
    Descarta las palabras de transcript['words'] (línea de tiempo ORIGINAL)
    cuyo intervalo solapa con algún corte, y remapea las que sobreviven a
    la línea de tiempo YA EDITADA (calibrada contra el vídeo final real si
    `drift_per_cut` != 0.0 -- ver _calibrated_edited_timestamp).

    Returns:
        [{"text": str, "start": float, "end": float}, ...] en la línea de
        tiempo editada, en el mismo orden que `words` (ya viene ordenado
        por tiempo en la transcripción).
    """
    result: list[dict] = []
    for w in words:
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        start = float(w["start"])
        end = float(w["end"])
        if end <= start:
            continue
        if _word_overlaps_any_cut(start, end, sorted_cuts):
            continue
        edited_start = _calibrated_edited_timestamp(start, sorted_cuts, drift_per_cut)
        edited_end = _calibrated_edited_timestamp(end, sorted_cuts, drift_per_cut)
        if edited_end <= edited_start:
            continue
        result.append({"text": text, "start": edited_start, "end": edited_end})
    return result


def _join_words(word_group: list[dict]) -> str:
    return " ".join(w["text"] for w in word_group)


def _is_natural_break(word: dict, next_word: dict | None, pause_gap: float) -> bool:
    text = word["text"]
    if text and text[-1] in _NATURAL_BREAK_CHARS:
        return True
    if next_word is not None and (next_word["start"] - word["end"]) >= pause_gap:
        return True
    return False


def _group_words_into_cues(words: list[dict], config: dict) -> list[list[dict]]:
    """
    Agrupa `words` (ya remapeadas a la línea de tiempo editada) en grupos,
    uno por subtítulo, respetando el presupuesto de caracteres
    (max_chars_per_line * max_lines) y de duración (max_cue_seconds) --
    ver el docstring del módulo para el porqué de cada límite.

    Cuando añadir la siguiente palabra desbordaría cualquiera de los dos
    presupuestos, el corte se hace en la ÚLTIMA pausa natural vista dentro
    del grupo actual (ver _is_natural_break) en vez de justo antes de la
    palabra que desborda -- salvo que esa pausa sea el principio mismo del
    grupo (nada que ganar cortando ahí) o no haya ninguna, en cuyo caso se
    corta duro justo antes de la palabra que desborda.
    """
    subtitles_config = config.get("subtitles", {})
    max_chars_per_cue = int(
        subtitles_config.get("max_chars_per_line", _DEFAULT_MAX_CHARS_PER_LINE)
    ) * int(subtitles_config.get("max_lines", _DEFAULT_MAX_LINES))
    max_cue_seconds = float(subtitles_config.get("max_cue_seconds", _DEFAULT_MAX_CUE_SECONDS))
    pause_gap = float(
        subtitles_config.get("natural_pause_gap_seconds", _DEFAULT_NATURAL_PAUSE_GAP_SECONDS)
    )

    def _overflows(group: list[dict]) -> bool:
        return (
            len(_join_words(group)) > max_chars_per_cue
            or group[-1]["end"] - group[0]["start"] > max_cue_seconds
        )

    groups: list[list[dict]] = []
    current: list[dict] = []
    last_break_idx: int | None = None

    for i, word in enumerate(words):
        if current and _overflows(current + [word]):
            if last_break_idx is not None and last_break_idx < len(current) - 1:
                groups.append(current[: last_break_idx + 1])
                current = current[last_break_idx + 1 :]
            else:
                groups.append(current)
                current = []
            last_break_idx = None
            # Caso límite: el remanente tras cortar en la pausa natural, más
            # esta palabra, sigue desbordando (solo puede pasar si ese
            # remanente ya iba casi al límite) -- corte duro adicional para
            # no propagar el desborde.
            if current and _overflows(current + [word]):
                groups.append(current)
                current = []

        current.append(word)
        next_word = words[i + 1] if i + 1 < len(words) else None
        if _is_natural_break(word, next_word, pause_gap):
            last_break_idx = len(current) - 1

    if current:
        groups.append(current)

    return groups


def _wrap_two_lines_balanced(text: str, max_chars_per_line: int) -> list[str] | None:
    """
    Busca, entre todos los puntos de corte posibles (espacios), el que
    reparte `text` en dos líneas <= max_chars_per_line cada una y más
    equilibradas entre sí (menor diferencia de longitud). None si ningún
    punto de corte cumple el límite en ambas líneas (una sola palabra más
    larga que max_chars_per_line, caso raro en español).
    """
    words = text.split(" ")
    best: tuple[str, str] | None = None
    best_diff: int | None = None
    for i in range(1, len(words)):
        line1 = " ".join(words[:i])
        line2 = " ".join(words[i:])
        if len(line1) <= max_chars_per_line and len(line2) <= max_chars_per_line:
            diff = abs(len(line1) - len(line2))
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = (line1, line2)
    return list(best) if best else None


def _wrap_greedy(text: str, max_chars_per_line: int) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current_line = ""
    for word in words:
        candidate = f"{current_line} {word}".strip()
        if len(candidate) <= max_chars_per_line:
            current_line = candidate
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    return lines


def wrap_cue_text(text: str, max_chars_per_line: int, max_lines: int) -> list[str]:
    """
    Reparte `text` en como mucho `max_lines` líneas de <= max_chars_per_line
    caracteres. Con max_lines=2 (el caso normal, ver config) busca el punto
    de corte más equilibrado (_wrap_two_lines_balanced); si no existe uno
    válido, o si max_lines != 2, cae a un reparto greedy (llena cada línea
    todo lo posible antes de pasar a la siguiente) -- mejor esfuerzo, no
    debería ocurrir en la práctica dado que _group_words_into_cues ya
    garantiza que el texto completo cabe en max_lines * max_chars_per_line
    caracteres.
    """
    if len(text) <= max_chars_per_line:
        return [text]
    if max_lines == 2:
        balanced = _wrap_two_lines_balanced(text, max_chars_per_line)
        if balanced is not None:
            return balanced
    return _wrap_greedy(text, max_chars_per_line)


def build_cues(word_groups: list[list[dict]], config: dict, total_edited_duration: float) -> list[dict]:
    """
    Convierte cada grupo de palabras (ya agrupado por _group_words_into_cues)
    en un subtítulo con su texto envuelto en líneas y su ventana de tiempo
    en pantalla, aplicando el suelo de duración por ritmo de lectura (ver
    docstring del módulo) y evitando solapar con el siguiente subtítulo o
    exceder la duración total del vídeo ya editado.
    """
    subtitles_config = config.get("subtitles", {})
    max_chars_per_line = int(subtitles_config.get("max_chars_per_line", _DEFAULT_MAX_CHARS_PER_LINE))
    max_lines = int(subtitles_config.get("max_lines", _DEFAULT_MAX_LINES))
    min_cue_seconds = float(subtitles_config.get("min_cue_seconds", _DEFAULT_MIN_CUE_SECONDS))
    max_cue_seconds = float(subtitles_config.get("max_cue_seconds", _DEFAULT_MAX_CUE_SECONDS))
    reading_cps_max = float(subtitles_config.get("reading_cps_max", _DEFAULT_READING_CPS_MAX))

    cues: list[dict] = []
    slow_pace_count = 0
    for idx, group in enumerate(word_groups):
        text = _join_words(group)
        lines = wrap_cue_text(text, max_chars_per_line, max_lines)

        start = group[0]["start"]
        raw_end = group[-1]["end"]

        reading_floor = (len(text) / reading_cps_max) if reading_cps_max > 0 else 0.0
        desired_end = max(raw_end, start + max(min_cue_seconds, reading_floor))
        desired_end = min(desired_end, start + max_cue_seconds)

        next_start = (
            word_groups[idx + 1][0]["start"] if idx + 1 < len(word_groups) else total_edited_duration
        )
        end = min(desired_end, next_start, total_edited_duration)
        # Nunca por debajo de la duración real de la locución, ni siquiera si
        # los topes de arriba (siguiente subtítulo / fin de vídeo) la
        # recortaron -- ver el docstring del módulo sobre por qué no se
        # aplica un TECHO de duración por ritmo de lectura.
        end = max(end, raw_end)

        if end > start and (len(text) / (end - start)) > reading_cps_max + 1e-6:
            slow_pace_count += 1

        cues.append({"start": start, "end": end, "lines": lines})

    if slow_pace_count:
        logger.info(
            "%d subtítulo(s) por encima del ritmo de lectura de referencia (%.0f cps) "
            "porque la locución real no dejó hueco para extenderlos más.",
            slow_pace_count, reading_cps_max,
        )

    return cues


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def cues_to_srt(cues: list[dict]) -> str:
    blocks: list[str] = []
    for i, cue in enumerate(cues, start=1):
        start_ts = _format_srt_timestamp(cue["start"])
        end_ts = _format_srt_timestamp(cue["end"])
        text = "\n".join(cue["lines"])
        blocks.append(f"{i}\n{start_ts} --> {end_ts}\n{text}\n")
    return "\n".join(blocks) + ("\n" if blocks else "")


def _write_srt(cues: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cues_to_srt(cues), encoding="utf-8")


def run(video_id: str, config: dict) -> dict:
    """
    Returns:
        dict con {"video_id", "subtitles_path" (None si
        config['subtitles']['enabled'] es false), "cues"}
    """
    subtitles_config = config.get("subtitles", {})
    if not subtitles_config.get("enabled", True):
        logger.info(
            "config['subtitles']['enabled'] es false; no se generan subtítulos para '%s'.", video_id
        )
        return {"video_id": video_id, "subtitles_path": None, "cues": []}

    transcript_path = _transcript_path(video_id, config)
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    cuts_path = _cuts_path(video_id, config)
    with open(cuts_path, "r", encoding="utf-8") as f:
        cuts = json.load(f)
    # Misma fusión de cortes con hueco mínimo insuficiente que aplica
    # edit/run.py (ver "Fusión de cortes con hueco mínimo insuficiente" en
    # su docstring) -- imprescindible aplicarla AQUÍ también: si
    # edit/run.py fusiona cortes al recortar pero subtitles/ remapea
    # contra el cuts.json SIN fusionar, la calibración de deriva de este
    # módulo (ver más abajo) compararía una duración nominal que ya no
    # corresponde a lo que edit/ realmente cortó, introduciendo un
    # desajuste de sincronización real (no solo el redondeo de frames que
    # esa calibración ya corrige). Ambos módulos leen el mismo
    # config['edit']['min_kept_segment_seconds'] para no divergir.
    min_kept_segment_seconds = float(config.get("edit", {}).get("min_kept_segment_seconds", 0.6))
    cuts = merge_short_kept_segments(cuts, min_kept_segment_seconds)

    duration = float(
        transcript.get("duration_s")
        or (transcript["segments"][-1]["end"] if transcript.get("segments") else 0.0)
    )
    sorted_cuts = sorted(cuts, key=lambda c: c["start"])
    nominal_main_duration = map_to_edited_timeline(duration, sorted_cuts)

    intro_duration_s = 0.0
    intro_words: list[dict] = []
    intro_path = _intro_path(video_id, config)
    if intro_path.exists():
        probed_intro_duration = _probe_duration(intro_path)
        if probed_intro_duration is not None and probed_intro_duration > 0:
            intro_duration_s = probed_intro_duration
            logger.info(
                "Intro detectado en %s (%.2fs); se transcribe y se antepone al .srt, y el "
                "contenido principal se desplaza %.2fs.",
                intro_path, intro_duration_s, intro_duration_s,
            )
            intro_transcript = transcribe_file(intro_path, config, log_progress=False)
            intro_words = _prepare_intro_words(intro_transcript.get("words", []))
            logger.info("%d palabra(s) transcritas del intro.", len(intro_words))
        else:
            logger.warning(
                "No se pudo determinar la duración de %s; se ignora para los subtítulos "
                "(se comporta como si no existiera).",
                intro_path,
            )

    drift_per_cut = compute_drift_per_cut(
        video_id, config, sorted_cuts, nominal_main_duration, intro_duration_s
    )
    total_main_edited_duration = _calibrated_edited_timestamp(duration, sorted_cuts, drift_per_cut)
    total_edited_duration = intro_duration_s + total_main_edited_duration

    all_words = transcript.get("words", [])
    main_words = filter_and_remap_words(all_words, sorted_cuts, drift_per_cut)
    logger.info(
        "%d/%d palabra(s) conservada(s) tras descartar las que caen dentro de un tramo cortado.",
        len(main_words), len(all_words),
    )
    if intro_duration_s > 0:
        main_words = [
            {"text": w["text"], "start": w["start"] + intro_duration_s, "end": w["end"] + intro_duration_s}
            for w in main_words
        ]
    words = intro_words + main_words

    word_groups = _group_words_into_cues(words, config)
    cues = build_cues(word_groups, config, total_edited_duration)
    logger.info("%d subtítulo(s) generado(s) a partir de %d palabra(s) conservada(s).", len(cues), len(words))

    output_dir = (REPO_ROOT / config["paths"]["output"]).resolve() / video_id
    subtitles_path = output_dir / "subtitles.srt"
    _write_srt(cues, subtitles_path)
    logger.info("Subtítulos guardados en %s", subtitles_path)

    db.set_status(video_id, "subtitles_generated")

    return {"video_id": video_id, "subtitles_path": str(subtitles_path), "cues": cues}


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Generar subtítulos (.srt) de un vídeo")
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
