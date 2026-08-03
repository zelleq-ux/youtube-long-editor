# Estado del proyecto

| Módulo            | Implementado | Validado end-to-end | Notas |
|--------------------|:---:|:---:|-------|
| ingest             | ✅ | ❌ | Implementado con ffmpeg/ffprobe vía subprocess (re-encode H.264/AAC, sin crop/rescale). Fuerza frame rate constante (`-fps_mode cfr` + `-r` con el `r_frame_rate` nominal detectado) para que detect_cuts pueda hacer cortes exactos por frame incluso si la grabación de OBS venía en VFR; si no se detecta un frame rate nominal fiable, cae a `-fps_mode cfr` sin `-r`. Validado con clips sintéticos (CFR 1920x1080@30fps y un caso VFR construido concatenando segmentos a 15/30/60fps) mediante la CLI real (`python -m src.ingest.run`); no probado aún contra una grabación real de OBS de 1-2h |
| transcribe         | ❌ | ❌ | Pendiente — mismo enfoque que newclips-viral-pipeline (faster-whisper) |
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

Implementar `src/transcribe/run.py` (faster-whisper sobre
`data/raw/<video_id>.mp4`), reutilizando el código ya validado en
`newclips-viral-pipeline` como base.
