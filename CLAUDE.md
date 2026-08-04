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
src/common/           -> config, db, utilidades compartidas con el otro proyecto (adaptar si hace falta)
```

### Contrato de datos entre etapas

- `data/raw/<video_id>.mp4` — vídeo original normalizado
- `data/transcripts/<video_id>.json` — igual formato que en newclips-viral-pipeline: `{words: [...], segments: [...]}`
- `data/cuts/<video_id>/cuts.json` — `[{start, end, type: "silence"|"filler", reason}]` — tramos A ELIMINAR
- `data/chapters/<video_id>/chapters.json` — `[{timestamp_s, title}]`
- `data/output/<video_id>/final.mp4` — vídeo final editado
- `data/output/<video_id>/chapters.txt` — capítulos en formato listo para pegar en YouTube (`00:00 Introducción`, etc.)

### Regla clave: silencio + acción visual NO se corta

`detect_cuts` combina DOS señales antes de marcar un tramo como recortable:

1. Silencio de audio (energía por debajo de un umbral, duración mínima)
2. Baja intensidad de movimiento visual en ese mismo tramo (optical flow,
   mismo enfoque que `score_motion_segment` en newclips-viral-pipeline)

Un tramo solo se marca para corte si CUMPLE AMBAS condiciones. Silencio con
alto movimiento visual (el usuario concentrado en una acción sin hablar) se
conserva siempre. Ver `config['detect_cuts']['motion_threshold']`.

Las muletillas detectadas en la transcripción pasan por el mismo filtro de
contexto visual antes de marcarse para corte.

### Corte automático sin revisión manual

A diferencia del otro proyecto (que tiene cola de revisión), aquí el usuario
quiere que `edit/` aplique los cortes directamente, sin aprobación manual
previa, confiando en los umbrales configurados. Aun así, `detect_cuts` debe
loguear un resumen (nº de cortes, duración total eliminada) antes de que
`edit/` los aplique, para que quede constancia en el log de ejecución.

### Zoom hacia la webcam en habla larga

En vez de un micro-zoom en cada punto de corte, `edit/` aplica el zoom
típico de streamer durante los tramos de habla continua de
`edit.long_speech_min_seconds` segundos o más (por defecto 10s), derivados
de `data/transcripts/<video_id>.json` agrupando palabras consecutivas cuyo
hueco es menor que `edit.long_speech_gap_seconds` (por defecto 1.2s). El
zoom sube lento y suave (curva coseno, sin saltos) desde 1.0 hasta
`edit.long_speech_zoom_factor` (por defecto ~1.12-1.15) durante los
primeros `edit.zoom_in_duration_seconds` (por defecto 2.5s) del tramo,
dirigido hacia `edit.facecam_region` (posición aproximada x/y/w/h de la
webcam sobre el frame original — no hace falta encuadrar la cara con
precisión, solo que el zoom se sienta dirigido hacia ahí). SE QUEDA en ese
zoom el resto del tramo (no vuelve a bajar gradualmente); al terminar el
tramo CORTA SECO a 1.0 (salto instantáneo, no una transición). Los
timestamps de la transcripción son del vídeo original, así que se
remapean a la línea de tiempo ya cortada restando la duración acumulada de
los cortes anteriores a cada punto — el mismo remapeo que hace falta para
los capítulos, ver más abajo.

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
```

## Estado del proyecto

Ver `status.md` para el detalle de qué módulo está implementado/validado.
