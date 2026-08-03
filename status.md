# Estado del proyecto

| Módulo            | Implementado | Validado end-to-end | Notas |
|--------------------|:---:|:---:|-------|
| ingest             | ❌ | ❌ | Pendiente — reutilizar base de newclips-viral-pipeline (adaptado a --file, sin yt-dlp por ahora ya que la fuente es OBS local) |
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

Implementar `src/ingest/run.py` y `src/transcribe/run.py`, reutilizando el
código ya validado en `newclips-viral-pipeline` como base.
