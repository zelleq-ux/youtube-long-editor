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
src/subtitles/        -> genera la pista de subtítulos (.srt) del vídeo ya editado
src/thumbnail/        -> extrae frames candidatos a miniatura (el usuario compone la miniatura final a mano)
src/publish/          -> sube el vídeo ya editado a YouTube (YouTube Data API v3)
src/common/           -> config, db, utilidades compartidas con el otro proyecto (adaptar si hace falta)
```

### Contrato de datos entre etapas

- `data/raw/<video_id>.mp4` — vídeo original normalizado
- `data/transcripts/<video_id>.json` — igual formato que en newclips-viral-pipeline: `{words: [...], segments: [...]}`
- `data/cuts/<video_id>/cuts.json` — `[{start, end, type: "silence"|"filler", reason}]` — tramos A ELIMINAR
- `data/chapters/<video_id>/chapters.json` — `[{timestamp_s, title}]`
- `data/output/<video_id>/intro.mp4` — OPCIONAL, grabación de intro aparte (ver "Intro grabado aparte" más abajo); si no existe, el pipeline funciona exactamente igual que sin este archivo
- `data/output/<video_id>/final.mp4` — vídeo final editado (intro + contenido editado + outro)
- `data/output/<video_id>/chapters.txt` — capítulos en formato listo para pegar en YouTube (`00:00 Introducción`, etc.)
- `data/output/<video_id>/subtitles.srt` — subtítulos del vídeo ya editado, formato .srt estándar
- `data/output/<video_id>/thumbnail_candidate_<N>.png` — frames candidatos a miniatura (resolución completa, sin componer) que genera `src/thumbnail/run.py`
- `data/output/<video_id>/thumbnail.png` — miniatura FINAL de YouTube, creada a mano por el usuario a partir de uno de los candidatos (ver más abajo) — `src/thumbnail/run.py` nunca escribe este archivo

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
como FRACCIÓN 0.0-1.0 del frame original — no píxeles absolutos, así
funciona igual sin importar la resolución del vídeo — en la raíz de la
config, compartida con el recorte de intro de `detect_cuts`; no hace
falta encuadrar la cara con precisión, solo que el zoom se sienta
dirigido hacia ahí). CORTA SECO a
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

### Subtítulos

`src/subtitles/run.py` genera `data/output/<video_id>/subtitles.srt` a
partir de `data/transcripts/<video_id>.json` (timestamps por palabra) y
`data/cuts/<video_id>/cuts.json`, remapeando cada palabra a la línea de
tiempo YA EDITADA con `map_to_edited_timeline` de `src/common/timeline.py`
(la misma utilidad que usan `edit/` y `detect_chapters/`) y descartando
las palabras cuyo audio cae dentro de un tramo cortado. Las palabras
conservadas se agrupan en líneas de subtítulo con el estándar de la
industria para contenido largo (estilo estático, sin animación): máximo
42 caracteres por línea, máximo 2 líneas por subtítulo, cada subtítulo
entre 1 y 6 segundos en pantalla, ritmo de lectura de referencia ~15-20
caracteres/segundo, y el corte entre subtítulos consecutivos prefiere caer
en una pausa natural (puntuación o un hueco largo entre palabras) en vez
de a mitad de frase. `config['subtitles']['enabled']` (default true)
desactiva el módulo entero sin tocar código.

IMPORTANTE -- calibración contra el vídeo final real: `map_to_edited_timeline`
asume que cada corte elimina exactamente `end - start` segundos, pero el
"renderizado parcial sin pérdida" de `edit/` redondea al frame más cercano
en cada corte (siempre hacia MÁS contenido conservado, ver más abajo) y
ese redondeo se ACUMULA con los cortes -- imperceptible con pocos cortes,
pero varios segundos de deriva con los cientos de cortes reales de una
grabación de 1-2h, suficiente para que los subtítulos se perciban
desincronizados. `subtitles/` corrige esto calibrando contra la duración
REAL del contenido principal en `data/output/<video_id>/final.mp4` (si ya
existe): la diferencia frente a la duración nominal de `cuts.json` se
reparte a partes iguales entre los cortes y se suma a cada palabra según
cuántos cortes la preceden. Por eso `subtitles/` debe ejecutarse DESPUÉS
de `edit/` en el pipeline (ver "Comandos útiles") -- sin `final.mp4`
todavía, o si la deriva medida es implausible, se cae de vuelta al
remapeo sin calibrar en vez de fallar.

### Intro grabado aparte

El usuario puede grabar un intro (1-2 min explicando de qué trata el
vídeo) en una sesión de OBS APARTE, normalmente unos minutos después del
directo — no es parte de la grabación principal. Si guarda ese archivo
como `data/output/<video_id>/intro.mp4`, `edit/` lo antepone al PRINCIPIO
del vídeo final (antes incluso del primer segundo del contenido ya
cortado/con zoom/normalizado) y el outro sigue exactamente donde ya
estaba, al final: `final.mp4` = intro + contenido editado + outro. Es
completamente OPCIONAL — sin ese archivo, el pipeline se comporta
exactamente igual que si esta funcionalidad no existiera
(`config['edit']['prepend_intro']`, por defecto true, permite además
desactivarlo sin borrar el archivo).

El intro NO pasa por `detect_cuts` ni por el zoom hacia la webcam: se usa
completo, tal cual lo grabó el usuario. Como puede venir de una sesión de
OBS distinta, puede tener resolución/fps/audio diferentes a
`data/raw/<video_id>.mp4` — nunca se asume que coinciden. `edit/` decide
automáticamente cuánto hace falta convertir para poder unirlo (mismo
mecanismo de tres niveles ya usado para el outro, generalizado para poder
anteponer Y posponer clips — ver `src/edit/run.py`, `_glue_extra_clip`):
si los parámetros de vídeo Y audio coinciden, concatenación directa sin
recodificar nada; si solo difiere el audio, se recodifica únicamente el
audio del intro; si difiere la resolución/fps, se recodifica el intro
completo. En todos los casos se escala/adapta SIEMPRE el intro a la
resolución del contenido PRINCIPAL, nunca al revés — el intro es una
grabación corta, perder algo de calidad ahí es aceptable; el contenido
principal del stream no.

Capítulos y subtítulos se ajustan para seguir cayendo en el punto
correcto del vídeo ya con el intro delante:

- `detect_chapters` antepone siempre un capítulo fijo "Introducción" en
  0:00 representando el intro (en vez del capítulo genérico condicional
  de antes) y desplaza todos los capítulos ya detectados por la duración
  del intro (leída con ffprobe directamente de `intro.mp4` — nominal, sin
  calibrar contra el vídeo final, porque este módulo puede correr antes
  que `edit/`).
- `subtitles` transcribe el intro por separado (mismo núcleo de
  faster-whisper que la etapa de transcripción principal, sin persistir
  el resultado a disco — un clip de 1-2 min se retranscribe en segundos)
  y antepone sus propias palabras al `.srt` con timestamps 0-based; las
  palabras del contenido principal se desplazan por la duración del intro
  ANCLADA a la duración real de `final.mp4` (la misma calibración de
  deriva ya documentada arriba para los cortes: `compute_drift_per_cut`
  resta la duración del intro de `final.mp4` exactamente igual que ya
  resta la del outro, así que cualquier imprecisión en esa duración se
  absorbe en la misma corrección lineal en vez de quedar como un
  desplazamiento fijo sin corregir).

`thumbnail` sigue extrayendo candidatos SOLO de `data/raw/<video_id>.mp4`
(el contenido principal del stream) — el intro nunca toca ese archivo, así
que queda excluido de los candidatos por construcción, sin necesidad de
ningún filtro adicional (igual que ya se ignora el outro).

### Miniatura de YouTube (thumbnail)

`src/thumbnail/run.py` NO compone ninguna miniatura (simplificado
drásticamente 2026-08-09 -- las dos versiones anteriores sí componían,
primero con paneles con borde + titular vía Claude + mejora con Gemini,
después con recorte de sujeto vía `rembg` + fondo desenfocado + título
quemado; ambos diseños quedan retirados por completo, ver `status.md` si
hace falta el detalle). Se limita a EXTRAER `--num-candidates` (default
5) frames reales, a la resolución COMPLETA del vídeo original (sin
recortar, sin excluir ninguna zona del frame, sin redimensionar), de los
momentos de mayor movimiento del directo, guardados como
`data/output/<video_id>/thumbnail_candidate_<N>.png` (orden cronológico).
Reutiliza la misma señal ligera de movimiento que usaban las versiones
anteriores de este módulo (diferencia media de píxeles en gris respecto
al frame ~0.5s antes -- deliberadamente más barato que el optical flow
denso de `detect_cuts`, para que extraer candidatos siga siendo cosa de
segundos), pero ahora sobre el FRAME COMPLETO (streamer + juego visibles
juntos, sin excluir `facecam_region` de nada). De los candidatos con más
movimiento se eligen los de mayor puntuación que queden separados entre
sí por al menos `--min-gap-seconds` (default 60s) -- evita quedarse con
varios frames casi idénticos del mismo instante de acción.

El usuario elige uno de los candidatos, lo compone a mano (título,
recortes, cualquier otro elemento) con su propio editor, y guarda el
resultado como `data/output/<video_id>/thumbnail.png` -- la ruta que
sigue consumiendo `publish/youtube.py` sin cambios. Ese módulo ahora
lanza `FileNotFoundError` con un mensaje claro ("thumbnail.png no
encontrado, generar/elegir uno primero") si ese archivo no existe
todavía, en vez de subir el vídeo sin miniatura en silencio.

`config['thumbnail']['enabled']` (por defecto true) desactiva el módulo
entero sin tocar código: si es false, `run()` no hace nada y lo deja
claro en el log.

### Publicación en YouTube

`src/publish/youtube.py` sube `data/output/<video_id>/final.mp4` a
YouTube vía la YouTube Data API v3 (`videos().insert`, subida resumable),
con título (por parámetro, o un placeholder marcado como pendiente si no
se da ninguno), descripción (pega `chapters.txt` si existe), miniatura
(`thumbnail.png` -- SIEMPRE obligatoria desde 2026-08-09, ver sección de
arriba: `run()` lanza `FileNotFoundError` con un mensaje claro si no
existe todavía, en vez de subir sin miniatura; vía `thumbnails().set()`,
adjuntada después de que YouTube devuelva el id del vídeo subido) y
subtítulos (`subtitles.srt` si existe -- este sí sigue siendo opcional,
vía `captions().insert()`, adjuntados también después de que exista el
id del vídeo, en español por defecto). `privacy_status` es SIEMPRE
`"private"` por defecto — nunca `"public"` ni `"unlisted"` salvo que se
pida explícitamente.

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
gastar cuota de subida real ni crear un vídeo de verdad en el canal. Los
subtítulos son la única excepción a "siempre se construye la petición
aunque no se ejecute": `captions().insert()` necesita el id que devuelve
YouTube al subir el vídeo, así que con `execute=False` no se construye ni
se llama en absoluto (no hay ningún id todavía sobre el que construirla).

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
# opcional: guardar un intro grabado aparte como data/output/<id>/intro.mp4
# antes de este paso -- edit/ lo antepone al vídeo final si existe (ver CLAUDE.md
# "Intro grabado aparte"); si no existe, este paso no cambia nada
python -m src.edit.run --video-id <id>
python -m src.subtitles.run --video-id <id>
python -m src.thumbnail.run --video-id <id> [--num-candidates 5] [--min-gap-seconds 60]
# elegir/editar un data/output/<id>/thumbnail_candidate_N.png a mano y guardarlo como
# data/output/<id>/thumbnail.png antes de publicar
python -m src.publish.youtube --video-id <id> [--title "..."] [--privacy private|unlisted|public] [--caption-language es] [--execute]
```

## Estado del proyecto

Ver `status.md` para el detalle de qué módulo está implementado/validado.
