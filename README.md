# YouTube Long Editor

App local para convertir grabaciones largas de directos (OBS, 1-2h) en un
vídeo editado listo para subir a YouTube: recorta silencios y muletillas
respetando la acción en pantalla, normaliza audio, disimula cortes con
micro-zoom, añade outro, y genera capítulos para la descripción.

Proyecto hermano de `newclips-viral-pipeline`. Ver `CLAUDE.md` para la
arquitectura completa y `status.md` para el estado actual de cada módulo.

## Setup

```bash
python -m venv venv
source venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env   # y rellena tu ANTHROPIC_API_KEY
```

Coloca tu vídeo de outro en `assets/outro/outro.mp4` (mismo códec/resolución
que tus grabaciones de OBS, para que la concatenación en `edit/` no dé
problemas).

## Uso (una vez implementado)

```bash
python -m src.ingest.run --file "C:/ruta/a/tu/grabacion_obs.mp4" --video-id stream_2026_08_02
python -m src.transcribe.run --video-id stream_2026_08_02
python -m src.detect_cuts.run --video-id stream_2026_08_02
python -m src.edit.run --video-id stream_2026_08_02
python -m src.detect_chapters.run --video-id stream_2026_08_02
```

## Estado actual

Esqueleto inicial — todos los módulos son stubs (`NotImplementedError`).
Reutiliza como base el código ya validado en `newclips-viral-pipeline`
(ingest, transcribe, y la lógica de optical flow de detect/).
