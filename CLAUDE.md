# YouTube Long Editor

App local (Python) que coge grabaciones largas (1-2h) de directos hechos
con OBS (formato horizontal) y produce un vídeo editado listo para subir a
YouTube: recorta silencios y muletillas (respetando la acción en pantalla),
normaliza el audio, disimula los cortes con micro-zoom, añade un outro, y
genera los capítulos para la descripción del vídeo.

Proyecto hermano de `newclips-viral-pipeline` — comparte el mismo estilo de
arquitectura y reutiliza conceptos ya validados allí (transcripción con
whisper, detección de movimiento visual con optical flow), pero el objetivo
es distinto: en vez de extraer clips cortos virales, se trata de limpiar y
pulir el vídeo largo completo para que se vea editado profesionalmente.

## Arquitectura

Igual que en el proyecto hermano: pipeline de etapas desacopladas en `src/`,
comunicándose vía JSON intermedio guardado en `data/`. Ningún módulo importa
lógica de negocio de otro directamente.

```
src/ingest/           -> carga el vídeo largo de OBS
src/transcribe/       -> whisper -> transcripción con timestamps
src/detect_cuts/      -> silencios de audio + movimiento visual + muletillas -> lista de cortes
src/detect_chapters/  -> bloques temáticos de la transcripción -> capítulos con timestamp+título
src/edit/             -> aplica cortes (+ micro-zoom), normaliza audio, añade outro
src/thumbnail/        -> compone la miniatura de YouTube a partir de frames reales + Claude + Gemini
src/publish/          -> sube el vídeo ya editado a YouTube (YouTube Data API v3)
src/common/           -> config, db, utilidades compartidas con el otro proyecto (adaptar si hace falta)
```

### Contrato de datos entre etapas

- `data/raw/<video_id>.mp4` — vídeo original normalizado
- `data/transcripts/<video_id>.json` — igual formato que en newclips-viral-pipeline: `{words: [...], segments: [...]}`
- `data/cuts/<video_id>/cuts.json` — `[{start, end, type: "silence"|"filler", reason}]` — tramos A ELIMINAR
- `data/chapters/<video_id>/chapters.json` — `[{timestamp_s, title}]`
- `data/output/<video_id>/final.mp4` — vídeo final editado
- `data/output/<video_id>/chapters.txt` — capítulos en formato listo para pegar en YouTube (`00:00 Introducción`, etc.)
- `data/output/<video_id>/thumbnail.png` — miniatura de YouTube (1280x720)

### Regla clave: silencio + acción visual NO se corta

`detect_cuts` combina DOS señales antes de marcar un tramo como recortable:

1. Silencio de audio (energía por debajo de un umbral, duración mínima)
2. Baja intensidad de movimiento visual en ese mismo tramo (optical flow,
   mismo enfoque que `score_motion_segment` en newclips-viral-pipeline)

Un tramo solo se marca para corte si CUMPLE AMBAS condiciones. Silencio con
alto movimiento visual (el usuario concentrado en una acción sin hablar) se
conserva siempre. Ver `config['detect_cuts']['motion_threshold']`.

El movimiento visual se mide EXCLUYENDO `facecam_region` del área
analizada (`config['detect_cuts']['exclude_facecam_from_motion']`, por
defecto activo): el propio streamer moviéndose en su webcam (gestos,
risas) no cuenta como "acción en pantalla" — esa señal solo debe venir del
contenido (el juego/pantalla compartida), nunca de la reacción del
streamer en su recuadro.

Las muletillas detectadas en la transcripción pasan por el mismo filtro de
contexto visual antes de marcarse para corte.

### Recorte automático de intro (detección de cara)

`detect_cuts` recorta además, de forma INDEPENDIENTE a silencio+movimiento,
la intro del vídeo: desde el instante 0 hasta que el usuario aparece de
verdad en pantalla. Se detecta muestreando `data/raw/<video_id>.mp4` cada
1-2s desde el inicio con un detector de caras ligero de OpenCV sobre el
recorte de `facecam_region` — no el frame completo (nada de mediapipe ni
nada pesado). La idea original era un Haar cascade clásico
(`cv2.CascadeClassifier`), pero la versión de OpenCV instalada (5.0.x)
eliminó ese binding de Python; en su lugar se usa `cv2.FaceDetectorYN`
("YuNet", detector basado en DNN igualmente ligero — modelo ONNX de
~230KB en `assets/models/face_detection_yunet_2023mar.onnx`). Para evitar
falsos negativos puntuales (parpadeo, frame
raro) se exige que la cara aparezca en al menos
`config['detect_cuts']['intro_face_min_detection_ratio']` (por defecto
70%) de las muestras dentro de una ventana de
`config['detect_cuts']['intro_face_confirm_window_seconds']` (por defecto
8s) antes de dar la aparición por buena.

Este corte se aplica SIEMPRE que se detecte con fiabilidad una intro sin
cara, sin necesidad de que ese tramo también sea silencio — no pasa por el
filtro de movimiento/silencio de más arriba. Es configurable
(`config['detect_cuts']['trim_intro']`, por defecto true) para desactivarlo
en vídeos sin intro sin cara. Si no se detecta ninguna aparición de cara en
los primeros `config['detect_cuts']['intro_face_max_search_seconds']` (por
defecto 15 min), no se corta nada por esta vía, para no arriesgarse a
recortar el vídeo entero por un fallo del detector.

### Corte automático sin revisión manual

A diferencia del otro proyecto (que tiene cola de revisión), aquí el usuario
quiere que `edit/` aplique los cortes directamente, sin aprobación manual
previa, confiando en los umbrales configurados. Aun así, `detect_cuts` debe
loguear un resumen (nº de cortes, duración total eliminada) antes de que
`edit/` los aplique, para que quede constancia en el log de ejecución.

### Renderizado parcial sin pérdida en el paso de corte

Por rendimiento (idea tomada de auto-editor de WyattBlue), `edit/` no
recodifica el vídeo completo al aplicar los cortes: por cada tramo a
conservar, copia sin recodificar (`-c copy`, prácticamente gratis) el
interior que cae entre dos keyframes reales del vídeo de entrada, y solo
recodifica (crf16/veryfast, como siempre) los bordes de ese tramo hasta
el keyframe más cercano — o el tramo completo, como antes, si es más
corto que un intervalo de keyframe y no hay hueco interior que copiar.
El resultado es exactamente el mismo vídeo de siempre (mismos cortes,
misma precisión a nivel de frame); solo cambia cuánto de ese trabajo se
recodifica de verdad. Medido contra `dinoblade_1`/`icarus_1` reales:
~91%/~76% de la duración conservada es candidata a copia directa (ver
`src/edit/run.py` y `status.md` para el detalle completo, incluida la
medición de tiempo real).

### Zoom hacia la webcam en habla larga

En vez de un micro-zoom en cada punto de corte, `edit/` aplica el zoom
típico de streamer durante los tramos de habla continua de
`edit.long_speech_min_seconds` segundos o más (por defecto 10s), derivados
de `data/transcripts/<video_id>.json` agrupando palabras consecutivas cuyo
hueco es menor que `edit.long_speech_gap_seconds` (por defecto 1.2s). El
zoom sube lento y suave (curva coseno, sin saltos) desde 1.0 hasta
`edit.long_speech_zoom_factor` (por defecto ~1.12-1.15) durante los
primeros `edit.zoom_in_duration_seconds` (por defecto 4.5s) del tramo,
dirigido hacia `facecam_region` (posición aproximada x/y/w/h de la webcam
sobre el frame original, en la raíz de la config — compartida con el
recorte de intro de `detect_cuts` — no hace falta encuadrar la cara con
precisión, solo que el zoom se sienta dirigido hacia ahí). CORTA SECO a
1.0 (salto instantáneo, no una transición) exactamente en el instante en
que se completa la rampa (inicio_del_tramo + zoom_in_duration_seconds) —
NO se mantiene sostenido el resto del tramo de habla, ni el corte espera a
que el tramo termine. Si el tramo de habla dura menos que
`zoom_in_duration_seconds` no da tiempo a completar la rampa, así que no
se aplica zoom en absoluto en ese tramo. Los timestamps de la
transcripción son del vídeo original, así que se
remapean a la línea de tiempo ya cortada restando la duración acumulada de
los cortes anteriores a cada punto — el mismo remapeo que hace falta para
los capítulos, ver más abajo.

### Miniatura de YouTube (thumbnail)

`src/thumbnail/run.py` compone `data/output/<video_id>/thumbnail.png`
(1280x720) SIEMPRE a partir de frames reales del vídeo, nunca inventando
una imagen desde cero:

1. Elige un frame real de `facecam_region` con expresión animada:
   muestrea un par de puntos dentro de cada uno de los primeros
   `config['thumbnail']['face_candidate_segments']` tramos de habla larga
   (mismo criterio que el zoom hacia la webcam de arriba) y, de los
   candidatos con una cara bien detectada (reutiliza el detector YuNet de
   `src/common/face_detection.py`, el mismo que usa el recorte de intro),
   elige el de mayor variación respecto al frame ~0.5s antes.
2. Elige un frame real de la zona de juego (fuera de `facecam_region`) en
   un momento de alto movimiento: muestrea
   `config['thumbnail']['gameplay_candidate_count']` puntos por el vídeo
   y se queda con el de mayor variación de píxeles, EXCLUYENDO
   `facecam_region` del cálculo (misma idea que
   `exclude_facecam_from_motion` de `detect_cuts`). Deliberadamente más
   ligero que `compute_motion_timeseries` (diferencia de píxeles en vez
   de optical flow denso, un puñado de candidatos en vez de recorrer el
   vídeo entero) — generar una miniatura debe tardar segundos, no los
   minutos que tarda el análisis de movimiento completo de `detect_cuts`.
3. Analiza la transcripción completa con Claude
   (`config['detect_chapters']['claude_model']`, mismo modelo/patrón de
   structured outputs que `detect_chapters`) para encontrar el momento
   más "punchy"/gancho del vídeo y proponer un titular corto (3-6
   palabras).
4. Compón ambos frames con Pillow (composición de imagen normal, sin
   IA): gameplay a pantalla completa de fondo, panel de cara con borde en
   una esquina, y el titular quemado encima con fuente/color/contorno
   controlados por nosotros si `config['thumbnail']['text_rendering']` es
   `"pillow"` (por defecto).
5. Mejora de estilo (contraste, iluminación "profesional") con Gemini
   ("Nano Banana", `config['thumbnail']['gemini_model']`, `GEMINI_API_KEY`
   del `.env`) SOLO sobre la composición ya armada del paso anterior — el
   prompt le pide explícitamente conservar la composición y el contenido
   real, nunca generar una imagen nueva. Si `text_rendering` es
   `"gemini"` en vez de `"pillow"`, el titular no se quema en el paso 4 y
   se le pide a Gemini que lo añada aquí. Si la llamada falla, no
   devuelve imagen, o `GEMINI_API_KEY` no está configurada, se cae de
   vuelta a la composición de Pillow sin modificar en vez de fallar el
   módulo entero — una miniatura sin mejorar sigue siendo mejor que
   ninguna.

`config['thumbnail']['enabled']` (por defecto true) desactiva el módulo
entero sin tocar código: si es false, `run()` no hace nada y lo deja
claro en el log.

### Publicación en YouTube

`src/publish/youtube.py` sube `data/output/<video_id>/final.mp4` a
YouTube vía la YouTube Data API v3 (`videos().insert`, subida resumable),
con título (por parámetro, o un placeholder marcado como pendiente si no
se da ninguno), descripción (pega `chapters.txt` si existe) y miniatura
(`thumbnail.png` si existe, vía `thumbnails().set()`, adjuntada después
de que YouTube devuelva el id del vídeo subido). `privacy_status` es
SIEMPRE `"private"` por defecto — nunca `"public"` ni `"unlisted"` salvo
que se pida explícitamente.

Autenticación OAuth estándar (`google-auth-oauthlib`,
`YOUTUBE_CLIENT_SECRET_PATH` del `.env`): la primera vez abre el
navegador para autorizar el acceso (scope `youtube.upload`) y guarda el
token en `token.json` (raíz del repo, gitignored) para no repetir la
autorización en cada ejecución.

IMPORTANTE — salvaguarda estructural, no solo de proceso: `run()` no sube
nada de verdad salvo que se pida `execute=True` explícito (`--execute` en
el CLI); por defecto (`execute=False`) construye la petición completa
(credenciales, body, `MediaFileUpload`) y se detiene ahí SIN llamar a
`.execute()`, para poder verificar que todo está bien construido sin
gastar cuota de subida real ni crear un vídeo de verdad en el canal.

## Convenciones

Mismas que en `newclips-viral-pipeline`: Python 3.11+, type hints, función
`run(video_id, config) -> dict` como entrada única por módulo, config
centralizada en `config/settings.yaml`, claves de API por variable de
entorno (`.env`, nunca en el repo).

## Comandos útiles

```bash
python -m src.ingest.run --file <ruta_al_mp4_de_obs>
python -m src.transcribe.run --video-id <id>
python -m src.detect_cuts.run --video-id <id>
python -m src.detect_chapters.run --video-id <id>
python -m src.edit.run --video-id <id>
python -m src.thumbnail.run --video-id <id>
python -m src.publish.youtube --video-id <id> [--title "..."] [--privacy private|unlisted|public] [--execute]
```

## Estado del proyecto

Ver `status.md` para el detalle de qué módulo está implementado/validado.
