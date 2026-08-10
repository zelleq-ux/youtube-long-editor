"""
Detección ligera de caras (YuNet) sobre una región recortada del frame,
compartida entre detect_cuts/ (recorte de intro por detección de cara,
exclusión de facecam_region del cálculo de movimiento) y thumbnail/
(elegir un frame de cara real para la miniatura).

Movido desde src/detect_cuts/run.py (donde se implementó y validó
originalmente, ver status.md) al añadir thumbnail/run.py, que necesita el
mismo detector -- ver CLAUDE.md, "ningún módulo importa lógica de negocio
de otro directamente": esta lógica vive en src/common/ para que ningún
módulo la importe directamente de otro.

La idea original era un Haar cascade clásico (cv2.CascadeClassifier),
pero la versión de OpenCV instalada en este proyecto (5.0.x) eliminó ese
binding de Python por completo (sin CascadeClassifier ni ninguna
constante CASCADE_*, confirmado en este entorno). En su lugar se usa
cv2.FaceDetectorYN ("YuNet"), el detector de caras basado en DNN que sí
trae esta versión de OpenCV: sigue siendo ligero (modelo ONNX de ~230KB,
milisegundos por frame sobre un recorte pequeño) y, como el cascade, solo
necesita analizar la región de la webcam, no el frame completo -- nada de
mediapipe ni nada más pesado.
"""
from __future__ import annotations

import cv2
import numpy as np

from src.common.config import REPO_ROOT

_FACE_MODEL_FILENAME = "face_detection_yunet_2023mar.onnx"
FACE_MODEL_PATH = REPO_ROOT / "assets" / "models" / _FACE_MODEL_FILENAME

# Parámetros de cv2.FaceDetectorYN.create; no son configurables hoy en
# settings.yaml.
_FACE_SCORE_THRESHOLD = 0.6
_FACE_NMS_THRESHOLD = 0.3
_FACE_TOP_K = 10  # solo hace falta saber si hay >=1 cara en un recorte pequeño, no las mejores 5000


def load_face_detector(input_size: tuple[int, int]) -> "cv2.FaceDetectorYN":
    """
    Crea el detector de caras YuNet sobre el modelo ONNX de
    assets/models/. No se cachea a nivel de módulo: input_size depende de
    la región recortada (facecam_region), que puede variar de un vídeo a
    otro, y crear el detector es barato (solo carga el modelo).
    """
    if not FACE_MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Falta el modelo de detección de caras ({FACE_MODEL_PATH}). "
            "Sin él no se puede usar la detección de cara."
        )
    return cv2.FaceDetectorYN.create(
        str(FACE_MODEL_PATH), "", input_size,
        score_threshold=_FACE_SCORE_THRESHOLD,
        nms_threshold=_FACE_NMS_THRESHOLD,
        top_k=_FACE_TOP_K,
    )


def facecam_region_to_pixels(region: dict, frame_width: int, frame_height: int) -> dict:
    """
    Convierte una región (x/y/w/h como FRACCIÓN 0.0-1.0 del frame, p.ej.
    config['facecam_region']) a píxeles absolutos para un frame concreto de
    frame_width x frame_height.

    Cambiado de píxeles absolutos a fracción (2026-08-10): la región se
    fijaba a mano en píxeles sobre un frame de referencia (1920x1080, el de
    dinoblade_1/icarus_1) y se aplicaba tal cual sin importar la resolución
    real del vídeo procesado -- correcto mientras todos los vídeos
    compartieran esa resolución, pero descalibrado sin avisar en cuanto
    apareció una grabación a otra resolución (shift_at_midnight_1,
    2560x1440: la webcam quedaba en una posición completamente distinta si
    se interpretaban esos mismos píxeles literalmente). Con fracción del
    frame, la misma config sirve para cualquier resolución sin recalibrar,
    asumiendo que la posición/tamaño de la webcam en OBS se mantiene
    proporcional al canvas entre grabaciones.

    Por defecto (claves ausentes) x=0.0, y=0.0, w=1.0, h=1.0 -- el frame
    completo, igual que el default anterior en píxeles (x=0, y=0,
    w=frame_width, h=frame_height).

    Se usa round() (no truncado) al convertir a píxeles: para una región
    calibrada y su vídeo de referencia exactos, evita que el redondeo de
    punto flotante de la división origen->fracción se pierda un píxel de
    más al truncar en la conversión de vuelta (fracción->píxel) -- p.ej.
    400/1080 no es exactamente representable en binario, pero
    round(1080 * (400/1080)) sigue dando 400, mientras que int(...) podría
    dar 399 según el redondeo exacto del float intermedio.
    """
    frame_width = max(1, frame_width)
    frame_height = max(1, frame_height)
    return {
        "x": round(float(region.get("x", 0.0)) * frame_width),
        "y": round(float(region.get("y", 0.0)) * frame_height),
        "w": round(float(region.get("w", 1.0)) * frame_width),
        "h": round(float(region.get("h", 1.0)) * frame_height),
    }


def facecam_crop_box(region: dict, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
    """
    Convierte una región (x/y/w/h como fracción 0.0-1.0 del frame, p.ej.
    config['facecam_region'] -- ver facecam_region_to_pixels) en una caja de
    recorte (x0, y0, x1, y1) en píxeles, clampada a los límites reales del
    frame -- la región es una posición aproximada fijada a mano, así que
    puede desbordar ligeramente por redondeo incluso ya convertida.
    """
    frame_width = max(1, frame_width)
    frame_height = max(1, frame_height)
    px = facecam_region_to_pixels(region, frame_width, frame_height)
    x0 = min(max(0, px["x"]), frame_width - 1)
    y0 = min(max(0, px["y"]), frame_height - 1)
    x1 = min(x0 + max(1, px["w"]), frame_width)
    y1 = min(y0 + max(1, px["h"]), frame_height)
    return x0, y0, x1, y1


def detect_faces(
    frame: np.ndarray, crop_box: tuple[int, int, int, int], face_detector: "cv2.FaceDetectorYN"
) -> np.ndarray | None:
    """
    Detecciones crudas de YuNet dentro de crop_box (o None si el recorte
    está vacío) -- un array Nx15 (x, y, w, h, 5 landmarks x/y, confianza
    en la última columna) o None si no detecta ninguna cara. Solo analiza
    ese recorte (no el frame completo): al ser una región pequeña (el
    tamaño de la webcam), la inferencia es barata incluso muestreando
    cientos de frames.
    """
    x0, y0, x1, y1 = crop_box
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    _, faces = face_detector.detect(crop)
    return faces


def frame_has_face(
    frame: np.ndarray, crop_box: tuple[int, int, int, int], face_detector: "cv2.FaceDetectorYN"
) -> bool:
    """True si detect_faces encuentra al menos una cara dentro de crop_box."""
    faces = detect_faces(frame, crop_box, face_detector)
    return faces is not None and len(faces) > 0
