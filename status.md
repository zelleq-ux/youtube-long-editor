# Estado del proyecto

| Módulo            | Implementado | Validado end-to-end | Notas |
|--------------------|:---:|:---:|-------|
| ingest             | ✅ | ❌ | Implementado con ffmpeg/ffprobe vía subprocess (re-encode H.264/AAC, sin crop/rescale). Fuerza frame rate constante (`-fps_mode cfr` + `-r` con el `r_frame_rate` nominal detectado) para que detect_cuts pueda hacer cortes exactos por frame incluso si la grabación de OBS venía en VFR; si no se detecta un frame rate nominal fiable, cae a `-fps_mode cfr` sin `-r`. Validado con clips sintéticos (CFR 1920x1080@30fps y un caso VFR construido concatenando segmentos a 15/30/60fps) mediante la CLI real (`python -m src.ingest.run`); no probado aún contra una grabación real de OBS de 1-2h |
| transcribe         | ✅ | ❌ | Implementado con faster-whisper (CTranslate2), `word_timestamps=True`, iterando el generador de segmentos de faster-whisper sin chunking manual (ya es streaming) y logueando progreso periódicamente. Resuelve `device: auto` a cpu/cuda explícitamente y elige `compute_type` (float16 en cuda, int8 en cpu) al no haber esa clave en config. Descarta defensivamente palabras sin timestamp válido. Guarda `data/transcripts/<video_id>.json` y registra el estado en `data/pipeline.db` (nuevo `src/common/db.py`, SQLite). Validado end-to-end SOLO con el modelo `tiny` en CPU sobre un clip sintético de 5s sin voz (testsrc2+sine) generado a partir de la etapa de ingesta real; no probado con el modelo `medium` configurado ni con una grabación real con voz |
| detect_cuts        | ❌ | ❌ | Pendiente — combina silencio de audio + optical flow + muletillas de transcripción |
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

Implementar `src/detect_cuts/run.py` (silencios de audio + optical flow +
muletillas de la transcripción -> `data/cuts/<video_id>/cuts.json`).
