# Estado del proyecto

| Módulo            | Implementado | Validado end-to-end | Notas |
|--------------------|:---:|:---:|-------|
| ingest             | ✅ | ❌ | Implementado con ffmpeg/ffprobe vía subprocess (re-encode H.264/AAC, sin crop/rescale). Fuerza frame rate constante (`-fps_mode cfr` + `-r` con el `r_frame_rate` nominal detectado) para que detect_cuts pueda hacer cortes exactos por frame incluso si la grabación de OBS venía en VFR; si no se detecta un frame rate nominal fiable, cae a `-fps_mode cfr` sin `-r`. Validado con clips sintéticos (CFR 1920x1080@30fps y un caso VFR construido concatenando segmentos a 15/30/60fps) mediante la CLI real (`python -m src.ingest.run`); no probado aún contra una grabación real de OBS de 1-2h |
| transcribe         | ✅ | ❌ | Implementado con faster-whisper (CTranslate2), `word_timestamps=True`, iterando el generador de segmentos de faster-whisper sin chunking manual (ya es streaming) y logueando progreso periódicamente. Resuelve `device: auto` a cpu/cuda explícitamente y elige `compute_type` (float16 en cuda, int8 en cpu) al no haber esa clave en config. Descarta defensivamente palabras sin timestamp válido. Guarda `data/transcripts/<video_id>.json` y registra el estado en `data/pipeline.db` (nuevo `src/common/db.py`, SQLite). Validado end-to-end SOLO con el modelo `tiny` en CPU sobre un clip sintético de 5s sin voz (testsrc2+sine) generado a partir de la etapa de ingesta real; no probado con el modelo `medium` configurado ni con una grabación real con voz |
| detect_cuts        | ✅ | ❌ | Implementado: silencios vía energía RMS (librosa, umbral `silence_db_threshold` en dBFS), muletillas por coincidencia exacta de subsecuencia (normalizada) contra `transcript['words']`, y filtro de movimiento visual (optical flow denso Farneback) que usa el percentil 90 (no la media) de las magnitudes muestreadas en el tramo — necesario porque `detect_silence_segments` agrupa en un único tramo cualquier silencio de audio continuo, que puede mezclar quietud real con un momento de acción; promediar diluiría esa acción por debajo del umbral y el tramo se cortaría igual. Aplica `cut_margin_seconds` y fusiona cortes solapados tras el margen. Guarda `data/cuts/<video_id>/cuts.json` y registra `cuts_detected` en `data/pipeline.db`. Validado end-to-end con un clip sintético construido con ffmpeg (tramos alternando estático/movimiento y silencio/audio, más un transcript sintético con muletillas) a través de la CLI real; no probado con una grabación real de OBS |
| detect_chapters    | ❌ | ❌ | Pendiente — bloques temáticos vía LLM sobre la transcripción |
| edit               | ❌ | ❌ | Pendiente — cortes + micro-zoom + normalización de audio (loudnorm) + outro |

## Decisiones de diseño registradas

- El vídeo de entrada es SIEMPRE la grabación horizontal de OBS (no hay
  descarga de YouTube en este proyecto, a diferencia de newclips-viral-pipeline).
- Los cortes se aplican automáticamente sin cola de revisión manual.
- Silencio + acción visual (movimiento) NUNCA se corta, solo silencio + quietud.
- Cada corte lleva un micro-zoom para disimular el salto.
- Se generan capítulos para la descripción de YouTube como subproducto.

## Próximo paso

Implementar `src/edit/run.py` (aplicar cortes de `data/cuts/<video_id>/cuts.json`
con micro-zoom, normalizar audio con loudnorm, añadir outro ->
`data/output/<video_id>/final.mp4`) o `src/detect_chapters/run.py`.
