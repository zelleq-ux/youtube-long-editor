"""
Etapa 5: Edición final.

Aplica los cortes de data/cuts/<video_id>/cuts.json sobre
data/raw/<video_id>.mp4:

1. Elimina cada tramo marcado (con el margen de seguridad
   config['detect_cuts']['cut_margin_seconds'] ya aplicado en detect_cuts).
2. Durante los tramos de habla continua de
   config['edit']['long_speech_min_seconds'] segundos o más (derivados de
   data/transcripts/<video_id>.json, agrupando palabras consecutivas cuyo
   hueco es menor que config['edit']['long_speech_gap_seconds']), aplica el
   zoom típico de streamer: sube lento y suave (curva coseno, sin saltos)
   desde 1.0 hasta config['edit']['long_speech_zoom_factor'] durante los
   primeros config['edit']['zoom_in_duration_seconds'] del tramo (por
   defecto 4.5s), dirigido hacia config['facecam_region'] (posición
   aproximada de la webcam sobre el frame original, compartida con
   detect_cuts); y CORTA
   SECO a 1.0 (salto instantáneo, sin transición de salida) exactamente en
   el instante en que se completa esa rampa — no se mantiene sostenido el
   resto del tramo de habla, ni el corte espera a que el tramo termine. Si
   el tramo de habla dura menos que zoom_in_duration_seconds no da tiempo
   a completar la rampa, así que no se aplica zoom en absoluto en ese
   tramo. NO hay zoom en los puntos de corte en sí.
3. Normaliza el audio con ffmpeg loudnorm si config['edit']['loudnorm'].
4. Si config['edit']['prepend_intro'], antepone
   data/output/<video_id>/intro.mp4 al PRINCIPIO (antes incluso del primer
   segundo del contenido ya editado) -- ver "Intro grabado aparte" más
   abajo.
5. Si config['edit']['append_outro'], concatena assets/outro/outro.mp4 al
   final.

Guarda en data/output/<video_id>/final.mp4 (orden final: intro + contenido
editado + outro).

Nota de orden: si detect_chapters ya generó timestamps sobre el vídeo
editado, este módulo debe correr ANTES de detect_chapters, o
detect_chapters debe recalcular sus timestamps a partir de los cortes
aplicados aquí — mantener consistencia, ver CLAUDE.md.

Intro grabado aparte (2026-08-10, simétrico al outro): a diferencia del
resto del contenido, data/output/<video_id>/intro.mp4 (opcional) es una
grabación de OBS APARTE, normalmente hecha 1-2 min después del directo en
otra sesión -- puede tener resolución/fps/audio distintos a
data/raw/<video_id>.mp4, así que NO se asume que coincidan (ver
prepend_intro/_glue_extra_clip). Ni el corte de cuts.json ni el zoom hacia
la webcam se aplican al intro (se usa tal cual, completo); solo se ajusta
lo mínimo necesario para poder unirlo al contenido principal sin
recodificar de más -- ver "Unión de clips extra (intro/outro)" más abajo.

Unión de clips extra (intro/outro), tres niveles (2026-08-10, generalizado
a partir del append_outro original para poder anteponer Y posponer clips
-- misma lógica de detección/conversión validada primero de forma manual
en data/work/shift_at_midnight_1/run_pipeline.py, función _smart_concat,
contra un caso real con audio distinto pero mismo vídeo):

_glue_extra_clip(main_path, extra_path, position, ...) une un "clip extra"
(intro o outro) con el clip PRINCIPAL, que NUNCA se recodifica sea cual
sea `position` ("before" o "after") -- puede ser la grabación de horas
del stream (o ya llevar el otro extremo pegado), así que es la que nunca
debe perder calidad recodificándola de más. Tres niveles, del más barato
al más caro (mismo criterio que _same_stream_params original, pero con un
nivel intermedio nuevo):

1. Parámetros de vídeo Y audio idénticos -> concatenación directa sin
   recodificar nada (concat demuxer, -c copy) -- si esto falla pese a
   coincidir los parámetros (el concat demuxer con -c copy NO valida esto
   por su cuenta, ver más abajo), se recae directamente en el nivel 3.
2. Solo el AUDIO difiere (vídeo idéntico) -> se recodifica ÚNICAMENTE el
   audio del clip extra, vídeo copiado sin tocar -- caso nuevo respecto al
   append_outro original (que antes recodificaba vídeo+audio aunque solo
   el audio difiriera), tomado de _smart_concat.
3. El vídeo difiere (resolución y/o fps) -> se recodifica el clip extra
   COMPLETO (vídeo+audio), escalado/rellenado a la resolución y fps del
   clip PRINCIPAL -- SIEMPRE se adapta el clip extra al principal, nunca al
   revés, para no degradar la calidad del contenido principal del stream
   (intro/outro son grabaciones cortas, perder algo de calidad ahí es
   aceptable; el contenido principal no).

IMPORTANTE (heredado del append_outro original): el concat demuxer con
-c copy NO valida que los streams encajen por su cuenta -- si no coinciden
y se le pide igualmente, no falla con un código de error, produce un
archivo "correcto" pero corrupto (fps/duración con valores absurdos). Por
eso _glue_extra_clip comprueba los parámetros explícitamente ANTES de
elegir la vía rápida, en vez de intentarla siempre y fiarse del
returncode.

Por qué solo el clip extra pasa nunca por un filtro `concat` de ffmpeg (y
el principal nunca se recodifica en absoluto): ver "Fuga de frames de
vídeo en el filtro concat de ffmpeg" más abajo -- el mismo bug ya
documentado para el paso de corte se reprodujo en 2026-08-09 contra
`dinoblade_1` real en el append_outro original al pasar el clip principal
COMPLETO por un filtro `concat` junto con el outro; el fix (evitar el
filtro `concat` del todo, no acotarlo) se generaliza aquí a los tres
niveles.

Los tramos de habla continua se identifican sobre los timestamps del
transcript, que son del vídeo ORIGINAL (antes de cortar). Como el zoom se
aplica sobre el vídeo YA CORTADO, cada timestamp se remapea restando la
duración acumulada de los cortes anteriores — el mismo remapeo que
CLAUDE.md documenta como necesario para detect_chapters.

Rendimiento y arquitectura en dos pasos (rediseñado 2026-08-05): con
grabaciones largas y muchos cientos de cortes, un único filter_complex
combinando TODOS los trim/atrim de corte + la concat + el zoom (el diseño
original) genera una cadena de texto que puede superar el límite de
longitud de línea de comandos de Windows (~32767 caracteres vía
CreateProcess; WinError 206 "el nombre del archivo o la extensión es
demasiado largo") — confirmado con un caso real de 1h39m/181 cortes/47
tramos de zoom (filtro de 44051 caracteres). El build de ffmpeg usado en
desarrollo tampoco soporta -filter_complex_script ni el indirect @archivo
para -filter_complex (verificado: ambos devuelven "option not found"), así
que no hay forma de sacar el grafo de filtros de la línea de comandos —
hay que MANTENERLO ACOTADO en caracteres.

apply_cuts_with_zoom se divide por eso en dos pasos independientes:

1. _cut_video: recorta CADA tramo a conservar a su propio archivo aislado
   (_cut_segment: un simple trim de entrada -ss/-to antes de -i, preciso a
   nivel de frame al recodificar) — SIN ningún filtro `concat` ni
   `filter_complex` en absoluto para el corte (ver "Fuga de frames de
   vídeo en el filtro concat de ffmpeg" más abajo para el porqué de
   evitarlo por completo en vez de solo acotar su fan-in, que es lo que se
   intentó primero y no bastó). Los archivos resultantes se pegan después
   con el concat DEMUXER (_glue_video_files, sin inpoint/outpoint, solo
   `file '...'` + `-c copy`) — rápido y exacto porque no recorta dentro de
   ningún archivo, y nunca pasa por el filtro `concat`.

   IMPORTANTE — por qué NO se usa el concat demuxer con inpoint/outpoint
   directamente sobre el vídeo original (el enfoque obvio para "evitar
   filter_complex del todo" desde el principio): se probó empíricamente y
   el inpoint del concat demuxer, al no alinear con un keyframe, NO
   recorta con precisión de frame al re-codificar — salta al keyframe
   anterior e incluye de más TODOS los frames intermedios (hasta un GOP
   entero, ~0.8s en la prueba), exactamente lo que CLAUDE.md prohíbe
   (comerse habla/acción por un corte impreciso). El outpoint sí es exacto
   (solo hay que dejar de leer paquetes); el problema es específico del
   inpoint. Por eso cada tramo se corta con -ss/-to ANTES de -i
   directamente sobre el vídeo ORIGINAL (frame-accurate al recodificar,
   sin depender del demuxer para el recorte en sí), y el concat demuxer
   solo se usa para pegar tramos ya completos.

2. _apply_zoom: aplica el zoom hacia la webcam en un filter_complex
   APARTE, sobre el vídeo YA CORTADO por _cut_video — mucho más corto que
   el combinado anterior porque solo cubre los tramos de habla larga (p.ej.
   47), no los cientos de cortes. El audio no pasa por este filtro (el
   zoom es solo de vídeo) y se copia sin re-codificar. Si aun así hicieran
   falta más tramos de zoom de los que caben en un único filter_complex,
   se encadenan varias pasadas (partición por presupuesto de caracteres,
   ver _plan_zoom_passes), cada una sobre la salida de la anterior.

_cut_video no tiene límite práctico de nº de tramos: cada uno es una
llamada de ffmpeg independiente, corta y sin filter_complex, así que ni el
nº de tramos ni sus caracteres pueden acercar la línea de comandos al
límite de Windows. _apply_zoom sí sigue particionando por presupuesto de
caracteres (_MAX_FILTER_COMPLEX_CHARS) porque su filter_complex crece con
el nº de tramos de zoom — a la escala real del proyecto (decenas) esto
siempre cabe en una única pasada.

Fuga de frames de vídeo en el filtro concat de ffmpeg (encontrada en
producción, 2026-08-08, investigada en dos rondas):

Primera ronda: una ejecución real contra `dinoblade_1` (diseño con shards
de corte particionados SOLO por presupuesto de caracteres, sin límite de
nº de tramos) produjo un `final.mp4` que se congelaba en el reproductor
sobre el minuto 10:29 y saltaba al minuto ~67, sin audio a partir de ahí.
`ffprobe` reveló un salto de PTS de ~3400s en mitad del vídeo, aterrizando
casi exactamente en la duración TOTAL del shard que lo contenía (133
tramos concatenados de un tirón en un único filter_complex). Reproducido
con un test sintético de 1h/400 cortes a 60fps
(tests/scale_test_edit_pipeline.py) pero NO con <=37 tramos de footage
real de dinoblade_1 (1080p60) ni con 133 tramos sintéticos a 30fps/640x360
— se interpretó (de forma incompleta, ver la segunda ronda) como sensible
a fan-in grande + 60fps, y el fix aplicado entonces fue limitar cada shard
a 40 tramos como máximo (_MAX_CONCAT_SEGMENTS_PER_SHARD, muy por debajo
del punto de fallo sintético observado).

Segunda ronda: al revalidar ese fix contra `dinoblade_1` completo (ya con
el límite de 40 tramos/shard), `ffprobe` sobre el `final.mp4` resultante
SEGUÍA mostrando 2 discontinuidades de PTS de vídeo (saltos de 25.6s y
519.5s) — el límite de 40 NO bastaba a la resolución real (1920x1080),
aunque sí bastaba en el test sintético a 640x360. Aislando el shard
problemático (los mismos 40 tramos y el mismo código de producción, sin
el resto del pipeline alrededor) se encontró la causa real: el archivo de
ESE SHARD, por sí solo, tenía 1077.5s de vídeo pero 1587.8s de audio — el
filtro `concat` de ffmpeg estaba perdiendo ~510s de FRAMES DE VÍDEO
silenciosamente dentro de su propio filter_complex (la pista de audio,
con el mismo fan-in, no se veía afectada en absoluto). Es decir: no es
(solo) "el concat pierde la cuenta del PTS acumulado" como se pensó en la
primera ronda, sino un fallo del propio filtro `concat` de ffmpeg al
reunir muchas ramas de vídeo trim+setpts, aparentemente sensible también a
la resolución (1080p reproduce el fallo con 40 tramos, algo que 640x360 no
reproducía ni con 133) — por lo que NINGÚN presupuesto de nº de tramos por
shard es una cota fiable mientras se siga usando el filtro `concat` para
el corte.

Fix definitivo: eliminar el filtro `concat` del paso de CORTE por
completo (no acotar su fan-in — evitarlo). _cut_video ya no arma shards
con filter_complex; corta cada tramo a un archivo aislado con un simple
trim de entrada (_cut_segment, sin ningún filtro) y los pega con el
concat DEMUXER (_glue_video_files) — el mismo mecanismo ya usado y
validado para pegar shards entre sí y para la vía rápida de append_outro,
que opera a nivel de contenedor (paquetes ya completos) y no pasa por el
filtro `concat`. Revalidado contra `dinoblade_1` completo tras el
rediseño: 0 discontinuidades de PTS en todo el archivo (ver status.md
para el detalle). La ruta del zoom (_apply_zoom) nunca usó `concat` (solo
scale/crop condicionados por `between()`) y no mostró el problema en
ningún test, así que no necesitaba ningún cambio.

Renderizado parcial sin pérdida (smart cut, 2026-08-09, idea tomada de
auto-editor de WyattBlue — investigado primero con un prototipo aislado
antes de tocar este módulo, ver status.md para el detalle de esa
investigación):

Cada tramo a conservar se recodificaba ENTERO a crf16/veryfast en
_cut_segment, aunque la inmensa mayoría de su duración no toca ningún
punto de corte — solo sus dos extremos importan para que el corte sea
preciso a nivel de frame. Midiendo contra los `cuts.json` reales de
`dinoblade_1` (147 cortes) e `icarus_1` (549 cortes, mucha más densidad
de cortes) qué fracción de cada tramo a conservar cae entre dos
keyframes reales del vídeo de entrada: 91.2% y 76.0% respectivamente
podría copiarse sin recodificar en vez de recodificarse — la fracción no
copiable se concentra en los tramos más cortos que un intervalo de
keyframe (frecuentes en `icarus_1`), no en el grueso de la duración.

Mecanismo (`_cut_segment_smart`, sustituye al antiguo `_cut_segment` que
solo recodificaba el tramo completo): antes de cortar, `_cut_video`
escanea UNA VEZ los timestamps de todos los keyframes del vídeo de
entrada (`_scan_keyframe_timestamps`, vía `ffprobe -show_entries
packet=pts_time,flags` — solo demuxea paquetes, no decodifica, así que es
barato incluso en grabaciones de 1-2h: ~15-35s medido contra
`dinoblade_1`/`icarus_1` reales, despreciable frente a los 25-55 min del
pipeline completo). Por cada tramo [start, end] a conservar:

1. Busca (bisección sobre la lista ordenada de keyframes) el primer
   keyframe >= start (`kf_start`) y el último keyframe <= end (`kf_end`).
2. Si `kf_end > kf_start` (hay un hueco interior real entre ambos):
   recodifica solo la cabeza [start, kf_start) y la cola [kf_end, end)
   (si no están vacías — con `start`/`end` ya alineados a keyframe no
   haría falta ninguna de las dos) igual que antes (crf16/veryfast), y
   copia SIN RECODIFICAR (`-c copy`) el interior [kf_start, kf_end) —
   válido porque `-ss`/`-to` antes de `-i` con estos timestamps caen
   EXACTAMENTE en keyframes reales, así que ffmpeg no necesita
   redondear al keyframe anterior para arrancar la copia (ver más abajo
   por qué esto SÍ es distinto del problema ya documentado del inpoint
   del concat demuxer).
3. Si no hay hueco útil (tramo más corto que un intervalo de keyframe):
   recodifica el tramo completo, exactamente el comportamiento de
   siempre — fallback sin pérdida de precisión ni de robustez.

Todos los fragmentos resultantes (1 a 3 por tramo) se pegan con el MISMO
concat DEMUXER (`_glue_video_files`) ya usado para pegar tramos
completos — no hace falta ningún mecanismo nuevo de unión.

Por qué esto NO es el mismo problema que el inpoint impreciso del concat
demuxer (documentado más arriba, el motivo por el que cada tramo se
corta con `-ss`/`-to` antes de `-i` en vez de con el demuxer): aquel
problema aparecía porque el inpoint NO coincidía con un keyframe real
(caía a mitad de GOP) y el demuxer redondeaba hacia atrás incluyendo de
más. Aquí `kf_start`/`kf_end` SON keyframes reales (leídos directamente
del vídeo, no arbitrarios), así que no hay nada que redondear — `-ss` cae
justo donde ya hay un keyframe. Verificado explícitamente con un test
sintético (ver tests/test_smart_cut_segments.py): el contenido copiado es
bit-idéntico (hash del frame decodificado) al del vídeo de origen en el
mismo instante.

Efecto secundario menor aceptado: dividir un tramo en cabeza/interior/
cola añade puntos de corte extra, y `-ss`/`-to` redondea cada uno a favor
de conservar un poco más de contenido, nunca menos (mismo tipo de
redondeo ya aceptado en producción — ver los ~2.9s de sobra en los 182
tramos de `dinoblade_1` más arriba) — esto escala con el Nº DE TRAMOS QUE
SE DIVIDEN, no con la duración total, así que es irrelevante en la
práctica (unas pocas fracciones de segundo repartidas en todo el vídeo).

Color range / pix_fmt: los `raw.mp4` reales de este proyecto son
`yuvj420p` (rango de color completo, propagado desde la fuente aunque
`ingest/run.py` solo pide `-pix_fmt yuv420p` sin más). Comprobado
explícitamente que recodificar cabeza/cola con los mismos flags de
siempre (sin ningún `-color_range` adicional) conserva ese mismo rango
completo automáticamente — no hay salto de niveles de negro/blanco en la
costura entre un fragmento copiado y uno recodificado.

Validado con un test sintético dedicado (tests/test_smart_cut_segments.py,
mismo patrón que scale_test_edit_pipeline.py: genera su propio vídeo,
sin tocar data/) que verifica bit-identidad del interior copiado,
continuidad de PTS/DTS (reutilizando check_pts_continuity/
check_av_duration_consistency) y consistencia de color_range/pix_fmt; y
contra un vídeo real (ver status.md para la medición de tiempo real
antes/después del paso de corte).

Solape de audio/vídeo en la costura del renderizado parcial sin pérdida
(bug real en vídeos ya publicados, encontrado e investigado 2026-08-11):

Síntoma reportado: en algunos vídeos ya publicados (dinoblade_1,
icarus_1, shift_at_midnight_1, how_many_dudes_1), un glitch esporádico
(no en todos los cortes) donde la voz repite/tartamudea la última sílaba
justo en un punto de corte -- p.ej. "Hey, dónde cojones-nes está la
pistola?!", con "-nes" como sílaba duplicada. El síntoma aparece EN MEDIO
de habla continua, no necesariamente junto a un silencio recortado -- la
primera pista de que la causa no era el punto de corte de detect_cuts
(que sí ajusta los silencios a su núcleo real, ver detect_cuts/run.py)
sino algo interno al mecanismo de corte de este módulo.

Causa raíz (AUDIO): `_cut_segment_smart` calcula `kf_start`/`kf_end` a
partir de keyframes de VÍDEO únicamente (`_scan_keyframe_timestamps`,
`-select_streams v:0`). El "interior" del tramo se copiaba con
`_cut_segment_copy` usando `-c copy` (vídeo Y audio sin recodificar) en
esos mismos timestamps -- válido para el vídeo (kf_start/kf_end SON
keyframes reales, ver más arriba), pero el AUDIO no comparte esa rejilla:
los paquetes AAC (1024 muestras, ~21.3ms a 48kHz) caen en instantes
propios, independientes del GOP de vídeo. En modo -c copy puro ffmpeg no
puede decodificar+descartar muestras para arrancar exactamente en
`kf_start` (solo puede si va a recodificar, como sí hacen los fragmentos
de cabeza/cola), así que el primer paquete de audio copiado del interior
es el que YA sonaba antes de `kf_start` -- contenido que el fragmento de
cabeza (recodificado con seek preciso) YA incluyó. Al pegar cabeza+
interior con el concat demuxer, ese solape se reproduce como audio
duplicado.

Medido con un vídeo sintético (GOP forzado a 2s, mismos crf/preset que
`ingest/run.py`) en 5 tramos con offsets de inicio distintos: 32.7-46.7ms
de audio duplicado en CADA costura cabeza→interior, sistemático (no una
coincidencia de un offset concreto) -- ver el análisis completo con
números reales en el historial de investigación (status.md). El orden de
magnitud (varias decenas de ms, en medio de una sílaba) encaja
exactamente con el síntoma reportado, y explica por qué es "esporádico":
no ocurre solo en los puntos de corte reales (ahí donde detect_cuts ya
cuida el silencio), sino en CUALQUIER costura interna cabeza/interior
dentro de un tramo a conservar suficientemente largo para activar la
copia sin recodificar (~76-91% de la duración conservada, ver más
arriba) -- la mayoría de esas costuras caen en silencio o soportan el
solape sin notarse; solo ocasionalmente caen encima de una sílaba con
energía suficiente para percibirse como tartamudeo.

Causa raíz (VÍDEO, hallazgo secundario de la misma investigación): por
simetría se comprobó también la costura interior→cola (el límite
`kf_end`, gobernado por `-to` en el `-c copy` original), y con B-frames
activos (`ingest/run.py` no pasa `-bf 0`; el preset `medium` de libx264
las deja activadas por defecto, así que los `raw.mp4` reales las tienen)
`-to` en modo -c copy NO corta con precisión de frame: el reordenamiento
por B-frames obliga a ffmpeg a leer ya varios frames del GOP SIGUIENTE
(los necesita para decodificar los B-frames del final del GOP actual)
antes de que el corte por tiempo tenga ocasión de excluirlos, así que se
"gotean" de más al fragmento interior. Medido: hasta ~130ms/4 frames de
vídeo de más en el mismo vídeo sintético -- un frame congelado/repetido
en la costura interior→cola, el equivalente visual del tartamudeo de
audio. Esto es DISTINTO del "efecto secundario menor aceptado" descrito
más arriba (ese es un redondeo de sub-frame en accurate seek durante
RECODIFICACIÓN, broken down y limitado a fracciones de frame, siempre
documentado como inofensivo); este es un fallo del propio -c copy en
modo stream-copy con B-frames, de una escala mucho mayor (frames enteros,
no fracciones).

Fix aplicado en `_cut_segment_copy` (ver ese docstring para el detalle
completo, incluida una nota importante sobre cómo NO fiarse de `-ss` para
verificar bit-identidad contra vídeos con B-frames): el VÍDEO del interior
se sigue copiando sin recodificar (`-c:v copy`, sigue siendo la parte cara
que este mecanismo evita recodificar), pero ahora en DOS pasadas -- 1)
cortar con `-to` como antes (aceptando el goteo de cola ya documentado)
y 2) un remux aparte que limita el resultado a `-frames:v <N>` (recuento
exacto, calculado a partir de `fps`), sin ningún `-ss`/`-to` de por medio
en esa segunda pasada. Se necesitan las DOS pasadas por separado: combinar
`-frames:v` directamente en la llamada con `-ss`/`-to` sobre un tramo de
varios GOPs sí da el recuento correcto pero CORROMPE el contenido por el
camino (probado y descartado, ver _cut_segment_copy). El AUDIO del
interior pasa a RECODIFICARSE con el mismo `-ss`/`-to` de la primera
pasada (ahora con seek preciso porque SÍ hay decodificación de por medio)
-- barato, la codificación de audio es rápida comparada con la de vídeo,
así que no compromete el ahorro de tiempo que motivó todo este mecanismo.
Revalidado con el mismo vídeo sintético: 0.0ms de solape de audio y
recuento de vídeo exacto (sin frames de más) en las 5 costuras probadas,
y contenido bit-idéntico frame a frame contra el origen en todo el tramo
(no solo el punto medio, verificado con un volcado secuencial completo,
no con `-ss`). Test de regresión dedicado en
tests/test_audio_seam_overlap.py (mide el solape de audio en la costura
cabeza→interior y el recuento de frames del interior directamente contra
las funciones de producción; falla si reaparece un solape apreciable o un
recuento de frames incorrecto).

Fusión de cortes con hueco mínimo insuficiente (bug real reportado tras
publicar witchfire_1 ya con el fix de arriba aplicado, investigado y
arreglado 2026-08-12):

Síntoma reportado: entre 56:00-57:52 de `final.mp4` sonaban muchos cortes
seguidos "en ametralladora" en un tramo de lectura continua (el narrador
del juego leyendo una carta, con la pantalla estática). Investigado a
fondo con datos reales ANTES de tocar código (ver status.md para el
detalle completo de la investigación): la forma de onda no mostraba
discontinuidades objetivas en las costuras individuales -- cada corte
técnicamente limpio, y confirmado explícitamente que el fix de solape de
audio de arriba seguía funcionando bien en ese mismo tramo real (0 solape,
verificado contra el `raw.mp4` real, no un sintético). La causa era
densidad: 22 cortes en ~170s (~2.8x la media del vídeo), varios separados
por menos de 3s -- cada empalme técnicamente correcto, pero encadenarlos
tan seguidos suena mal EN CONJUNTO. El usuario quería mantener intacta la
sensibilidad de detección (esa densidad de cortes es DELIBERADA, le gusta
ese dinamismo) -- el fix no podía tocar `silence_min_seconds` ni
`motion_threshold`.

`merge_short_kept_segments` (`src/common/timeline.py`, no en este módulo
-- es una utilidad compartida, usada también por `subtitles/run.py`, ver
más abajo) funde dos cortes consecutivos cuando el tramo conservado ENTRE
ellos es más corto que `config['edit']['min_kept_segment_seconds']`
(0.6s por defecto), absorbiendo también ese tramo en vez de dejarlo como
una isla de audio casi imperceptible. Valor elegido con datos reales de
witchfire_1 (no arbitrario): de los 81 tramos conservados <2.5s en todo
el vídeo, hasta 0.5s los seis afectados estaban TODOS vacíos de palabras
transcritas (0 pérdida real de contenido); en 0.6s se pierde una única
palabra suelta y poco informativa ("Ahora") a cambio de capturar la única
isla realmente vacía (0.528s) que había dentro de la propia ventana
investigada -- por encima de 0.6s empiezan a perderse reacciones cortas
con contenido real ("¿O qué?", "Vale.", "¡Hostia!"), justo lo que no se
quería tocar.

Aplicado en `apply_cuts_with_zoom` (sobre `cuts` antes de calcular
`keep_segments`, así que también afecta a `detect_long_speech_segments`
de forma consistente) y en `subtitles/run.py` (mismo umbral, sobre el
`cuts` cargado de `cuts.json` antes de `map_to_edited_timeline`) --
IMPRESCINDIBLE en subtítulos también: si `edit/` funde cortes pero
`subtitles/` remapea contra el `cuts.json` sin fundir, su calibración de
deriva (ver más arriba, "Calibración contra el vídeo final real" en
subtitles/run.py) mediría una duración nominal que ya no corresponde a lo
que `edit/` realmente cortó, introduciendo un desajuste de sincronización
real. NO aplicado en `detect_chapters/` a propósito: sus marcadores
toleran `min_chapter_seconds=120`, así que unos segundos de diferencia
por no fundir ahí son completamente imperceptibles -- no vale la pena la
complejidad de tocar ese módulo también.

Micro-crossfade en los empalmes de audio (misma investigación,
2026-08-12): además de la densidad, el usuario pidió suavizar la
SEQUEDAD de cada corte individual -- un crossfade equal-power (curva
coseno/seno, el mismo estándar que implementa `acrossfade curve=qsin` de
ffmpeg, elegido explícitamente en vez de un crossfade LINEAL porque este
último sí produce un bajón de volumen perceptible en la transición) de
`config['edit']['audio_crossfade_ms']` (20ms por defecto) en CADA unión
entre fragmentos al cortar -- tanto interior-copiado como recodificación
completa, sin distinción (`_glue_video_files` ya trata ambos como una
lista plana de fragmentos a unir, así que aplicar el crossfade de forma
uniforme sobre esa misma lista es natural). El VÍDEO se sigue
concatenando exactamente igual que siempre (concat demuxer, `-c copy`,
sin crossfade -- solo el audio se sustituye).

Por qué NO se implementa encadenando el filtro `acrossfade` de ffmpeg
(la opción obvia, descartada tras considerarla): con cientos de
fragmentos por vídeo, encadenar esa cantidad de operaciones en un
filter_complex corre el mismo riesgo de escala ya documentado para el
filtro `concat` más arriba ("Fuga de frames de vídeo..."); y hacerlo en
múltiples pasadas de ffmpeg con AAC como formato intermedio acumularía
generaciones de recodificación con pérdida en el audio que participa de
varios crossfades seguidos. En su lugar (`_decode_audio_float32`,
`_decode_fragment_groups`, `_local_crossfade_concat`,
`_write_crossfaded_audio` en este módulo), el audio de cada fragmento se
decodifica a PCM/NumPy (barato: audio, no vídeo), se funde con la curva
equal-power en Python, y solo se recodifica a AAC UNA vez al final -- todo
el proceso intermedio es sin pérdida.

Corrección de duración (`_resample_to_length`): un crossfade de N
muestras acorta el audio combinado en N muestras por cada unión --
consecuencia matemática inevitable de solapar contenido en vez de
concatenarlo seco (es literalmente lo que hace un crossfade). El vídeo,
sin embargo, no se acorta (se pidió explícitamente que no llevara
crossfade). Sin corregir esto, el audio se habría ido desincronizando
PROGRESIVAMENTE del vídeo a lo largo de todo el vídeo (varios segundos
acumulados en una grabación de 1-2h con cientos de cortes). La primera
versión de esta corrección era un ÚNICO reestiramiento GLOBAL al final
(~0.2% típico en una grabación real) -- ver "Reestirado LOCAL en vez de
global" más abajo para por qué esto resultó ser insuficiente en la
práctica (aunque imperceptible en aislado, un único factor global no
puede seguir una distribución de cortes reales no uniforme) y se
sustituyó por un reestiramiento LOCAL, por `keep_segment`, que es el
mecanismo actual. Validado explícitamente contra el test de escala
completo (1h/varios cientos de cortes, ver scale_test_edit_pipeline.py)
con merge+crossfade ya activados por defecto: sin discontinuidades de PTS
ni desajuste de duración audio/vídeo.

Validado con fragmentos REALES de witchfire_1 (no solo sintéticos, ver
tests/test_merge_short_kept_segments.py y tests/test_audio_crossfade.py):
en las costuras reales con una discontinuidad apreciable en el corte
duro, el crossfade la reduce sustancialmente (p.ej. de 0.068 a 0.020 en
escala -1..1); ninguna costura con crossfade se acerca a escala de click
audible. Clips ANTES/DESPUÉS de la ventana reportada, generados con estas
mismas funciones de producción sobre el `raw.mp4` real (no el
`final.mp4` ya publicado), confirmados de oído por el usuario. No se ha
tocado ningún vídeo ya publicado.

Fronteras internas espurias del renderizado parcial sin pérdida (bug de
desincronización audio/vídeo, encontrado 2026-08-12, arreglado
2026-08-14): la primera versión de este mecanismo aplicaba el crossfade
en TODAS las fronteras de `segment_paths`, sin distinguir las fronteras
REALES (entre dos `keep_segments` distintos, un corte de verdad) de las
fronteras INTERNAS que `_cut_segment_smart` introduce solo para poder
copiar sin recodificar el interior de un mismo tramo (head/mid/tail, ver
"Renderizado parcial sin pérdida" más arriba) -- ahí no hay ningún corte
real, es contenido continuo dividido en varios archivos solo por
rendimiento, y además ya sample-exacto en esa costura gracias al fix de
solape de audio de más arriba. Tratar cada frontera interna como si fuera
real acortaba el audio otros `crossfade_ms` de más sin que hubiera
ninguna discontinuidad que justificara solaparla; medido contra
`shift_at_midnight_2` (348 cortes, 706 fragmentos): de las 705 fronteras
totales, solo 333 eran cortes reales, 372 eran fronteras internas
espurias. `_resample_to_length` seguía corrigiendo la duración TOTAL
correctamente (su objetivo es siempre la duración real medida del vídeo
ya concatenado, no un cálculo derivado del nº de fronteras), pero ese
acortamiento de más estaba repartido de forma muy desigual por la línea
de tiempo (las fronteras internas espurias no se distribuyen
uniformemente -- dependen de cuántos keyframes caen dentro de cada tramo
concreto), así que el reestiramiento uniforme final corregía la duración
AGREGADA sin corregir el desincronismo LOCAL en cada punto -- por eso
ninguna comprobación de duración total o continuidad de PTS lo detectaba
(ver tests/scale_test_edit_pipeline.py más abajo para el nuevo test que
sí lo detecta).

Fix: `_cut_video` construye ahora `boundary_is_real` (lista paralela a
las fronteras de `segment_paths`, `True` únicamente en la frontera entre
el último fragmento de un `keep_segment` y el primero del siguiente) y la
pasa a través de `_write_crossfaded_audio` hasta `_decode_fragment_groups`
(que agrupa los fragmentos por `keep_segment` usando exactamente esta
lista) y `_local_crossfade_concat` (que aplica el crossfade equal-power
SOLO entre grupos, es decir, SOLO en fronteras reales; dentro de un mismo
grupo el audio ya llega concatenado en seco desde `_decode_fragment_groups`,
sin crossfade, sin acortar nada). Al reducirse el nº de crossfades
realmente aplicados (333 en vez de 705 en el caso real), el acortamiento
total a corregir se reduce en la misma proporción.

Reestirado LOCAL en vez de global (segundo bug de desincronización,
encontrado 2026-08-14 revalidando el fix anterior contra
`shift_at_midnight_2` real -- ver status.md para el detalle completo de
la investigación): el fix de fronteras internas de arriba es necesario
pero, se descubrió, NO suficiente por sí solo para bajar el desincronismo
local a escala de ruido de ASR (~50-100ms) en un vídeo real. Medido con
un método de localización de contenido limpio (sin pasar por
`cuts.json` ni por subtítulos -- se busca directamente el frame/audio
real de `data/raw/<id>.mp4` dentro de `final.mp4` por coincidencia de
contenido, así que no hereda ninguna deriva de cálculo de timestamps):
incluso con las fronteras internas ya arregladas, `shift_at_midnight_2`
seguía mostrando hasta ~1.05s de desfase local en la zona 46-55% del
vídeo (vs. ~1.5s con el bug de fronteras internas sin arreglar en esa
misma zona -- el primer fix SÍ ayuda, ~250-475ms según el punto, pero no
basta).

Causa: incluso aplicando el crossfade SOLO en fronteras reales (ya
corregido), `_resample_to_length` seguía aplicándose una única vez,
GLOBALMENTE, sobre el audio ya cruzado -- una única tasa de estiramiento
calculada con la duración TOTAL. Esto es matemáticamente exacto en
AGREGADO (la suma cuadra siempre, por construcción), pero los cortes
REALES de una grabación real NO están uniformemente distribuidos (rachas
de cortes seguidos en tramos de acción/lectura rápida, huecos largos en
tramos de habla continua -- confirmado con `cuts.json` real de
`shift_at_midnight_2`, y reproducido con una simulación analítica pura
-- sin renderizar nada -- del propio mecanismo de reestirado contra ese
mismo `cuts.json`, que predice la MISMA forma no monótona con la MISMA
magnitud que la medición real, incluida una coincidencia casi exacta en
el punto ~95%). Una tasa global termina "prestando" corrección de zonas
con pocos cortes reales a zonas con muchos, y viceversa -- el mismo tipo
de error, a menor escala, que el propio bug de fronteras internas de
arriba (una corrección UNIFORME para un acortamiento distribuido de
forma NO uniforme).

Fix: `_local_crossfade_concat` ya no hace un único reestiramiento global.
Cada `keep_segment`, justo después de aplicar su crossfade con el
siguiente, se reestira INDIVIDUALMENTE de vuelta a su propia longitud
EXACTA -- el acortamiento se corrige exactamente DONDE ocurrió, nunca
repartido sobre el resto del vídeo. `_write_crossfaded_audio` conserva
una única llamada final a `_resample_to_length` sobre el resultado ya
localmente corregido, pero como red de seguridad para una discrepancia
ya minúscula -- no como mecanismo principal.

Vídeo más largo que audio en cada recodificación (TERCER bug de
desincronización, encontrado 2026-08-14 revalidando el fix anterior --
ver status.md para el detalle completo de la investigación, incluida la
medición directa contra fragmentos reales de `shift_at_midnight_2` que lo
confirmó): el fix de reestirado local de arriba usaba, como longitud
"correcta" a la que devolver cada `keep_segment`, la longitud con la que
el AUDIO de ese tramo se había extraído (`len(group)` en
`_decode_fragment_groups`) -- una elección razonable en apariencia (es la
duración real de ESE audio, sin crossfade de por medio), pero que resultó
estar sistemáticamente sesgada: medido con fragmentos reales
(`_cut_segment_smart`/`_cut_segment_recode` de producción, sin ningún
crossfade ni resample de por medio), el VÍDEO de un mismo tramo recodificado
sale sistemáticamente MÁS LARGO que su AUDIO -- entre ~5 y ~26ms más en
los casos medidos, SIEMPRE en la misma dirección -- porque `-ss`/`-to`
antes de `-i` en una recodificación redondea el arranque/fin del VÍDEO a
un límite de FRAME (~16.7ms de granularidad a 60fps), mientras que el
AUDIO, con una granularidad de muestra (~0.02ms), no necesita ese
redondeo y sale prácticamente exacto al nominal. Usar la longitud del
audio como objetivo, por tanto, NO corregía esta discrepancia -- solo
disimulaba el acortamiento del crossfade. Esta discrepancia video-vs-audio
es proporcionalmente MUCHO mayor en tramos CORTOS (recodificados
enteros, sin interior copiado -- toda su duración carga con el sesgo) que
en tramos largos (solo sus bordes recodificados cargan con el sesgo, el
interior copiado es bit-exacto), así que se concentra precisamente en las
zonas con más tramos cortos seguidos -- confirmado como la causa real de
un desfase de varios cientos de ms en una zona así de `shift_at_midnight_2`
que el fix de reestirado local, por sí solo, reducía pero no eliminaba.

Fix (primera versión, INSUFICIENTE por sí sola -- ver más abajo):
`_cut_video` medía la duración REAL DEL VÍDEO de cada `keep_segment` en
FRAMES sobre cada fragmento SIN UNIR (`_count_video_frames`, ffprobe
-count_packets) dividido por `fps` (la tasa nominal declarada por el
contenedor de entrada), y pasaba esas duraciones -- no las del audio
extraído -- a `_local_crossfade_concat` como `target_lengths`. Mejoraba
mucho el desfase real medido (de hasta ~1.5s a ~200-660ms según el
punto), pero no lo eliminaba del todo.

Redondeo del concat demuxer en el vídeo ya unido (causa real del
desfase residual, encontrado 2026-08-15 investigando por qué el fix de
arriba no bastaba -- ver status.md para el detalle completo, incluida la
medición contra `shift_at_midnight_2` real): la sospecha inicial fue que
`fps` NOMINAL (el declarado por el contenedor, p.ej. `r_frame_rate=60/1`
exacto) no coincidía con la tasa REAL de la grabación -- descartado
explícitamente: se midieron los PTS reales de `data/raw/<id>.mp4` en
varias zonas (una con buen resultado, una con el desfase residual, otra
con buen resultado) y las tres dieron exactamente 60.000000fps, sin
ningún jitter de captura. La causa real, aislada reproduciendo el
problema a pequeña escala con fragmentos reales: CADA FRAGMENTO
individual (antes de unirlo a los demás) tiene metadata de duración
exacta (verificado: 0.00ms de diferencia entre su propio `frames/fps` y
su propia duración declarada, en 13 fragmentos reales comprobados) --
pero el concat DEMUXER (`_glue_video_files`) introduce un pequeño
redondeo de PTS en CADA UNIÓN al encadenar muchos fragmentos pequeños
(confirmado con los PTS reales del archivo ya unido, no solo su duración
declarada: la tasa implícita del PTS baja a ~59.97 SOLO después de unir,
nunca antes). Esto es proporcional al Nº DE UNIONES, no al tiempo
transcurrido ni a `fps` -- por eso se concentraba precisamente en las
zonas con más cortes seguidos (más fragmentos, más uniones), exactamente
donde el fix de arriba dejaba más desfase residual.

Fix definitivo: en vez de calcular la duración de cada `keep_segment`
como `frames/fps` sobre sus fragmentos SIN unir, `_cut_video` mide ahora
su posición REAL en el vídeo YA UNIDO -- `_scan_video_pts(cut_path)` lista
los PTS reales (ordenados por presentación) de TODO el archivo ya
concatenado (ffprobe, solo demuxea, barato incluso en 1-2h), y
`_group_video_durations_from_pts` calcula la duración de cada
`keep_segment` como la diferencia entre el PTS de inicio del SIGUIENTE
grupo y el de este grupo (o la duración total menos el PTS de inicio,
para el último) -- captura CUALQUIER redondeo que el concat demuxer haya
introducido, sea cual sea su causa exacta, porque mide directamente sobre
el resultado real en vez de intentar predecirlo. La red de seguridad
final de `_write_crossfaded_audio` (una medición INDEPENDIENTE de la
duración total, no derivada de sumar `target_lengths`) se mantiene de
todos modos: con este fix la suma de duraciones por PTS ya coincide con
la duración total salvo por el PTS del primer frame (normalmente unos
pocos ms, no segundos), así que vuelve a ser una red de seguridad
genuinamente minúscula, no una corrección de magnitud apreciable como con
el fix anterior.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from src.common import db
from src.common.config import REPO_ROOT, load_config
from src.common.face_detection import facecam_region_to_pixels
from src.common.timeline import compute_keep_segments, map_to_edited_timeline, merge_short_kept_segments

logger = logging.getLogger(__name__)

# Objetivos de sonoridad para el paso de loudnorm (dos pasadas: medición +
# aplicación). -14 LUFS integrado / -1.5 dBTP de pico real / 11 LU de rango
# es un objetivo habitual para contenido hablado pensado para YouTube; no
# es una clave de config porque la tarea solo pide activar/desactivar
# loudnorm, no parametrizar el objetivo.
_LOUDNORM_TARGET_I = -14.0
_LOUDNORM_TARGET_TP = -1.5
_LOUDNORM_TARGET_LRA = 11.0

# Presupuesto de caracteres por filter_complex del paso de ZOOM (ver
# _plan_zoom_passes -- el corte ya no usa filter_complex en absoluto, ver
# "Fuga de frames de vídeo en el filtro concat de ffmpeg" en el docstring
# del módulo): Windows limita la línea de comandos de un proceso a ~32767
# caracteres (CreateProcess; por debajo de eso, WinError 206 "el nombre
# del archivo o la extensión es demasiado largo"). El resto de argumentos
# de la llamada a ffmpeg (rutas, flags de códec) apenas ocupan unos
# cientos de caracteres, así que dejar ~12000 de margen es de sobra.
_MAX_FILTER_COMPLEX_CHARS = 20000

# Calidad del vídeo intermedio de corte (_cut_segment_recode): más alta que la
# del vídeo final (_CUT_CRF < _FINAL_CRF) porque este archivo se vuelve a
# recodificar en el paso de zoom -- una calidad baja aquí compondría dos
# generaciones de pérdida en vez de una sola. veryfast porque es un
# intermedio que se borra enseguida, no hace falta optimizar su tamaño.
_CUT_SHARD_CRF = "16"
_CUT_SHARD_PRESET = "veryfast"
_FINAL_CRF = "20"
_FINAL_PRESET = "medium"


def _raw_video_path(video_id: str, config: dict) -> Path:
    raw_dir = (REPO_ROOT / config["paths"]["raw"]).resolve()
    path = raw_dir / f"{video_id}.mp4"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe el vídeo de entrada para '{video_id}': {path}. "
            "Ejecuta primero la etapa de ingesta "
            "(python -m src.ingest.run --file <ruta_al_mp4_de_obs>)."
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


def _transcript_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["transcripts"]).resolve() / f"{video_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe la transcripción para '{video_id}': {path}. "
            f"Ejecuta primero la etapa de transcripción (python -m src.transcribe.run --video-id {video_id})."
        )
    return path


def _output_dir(video_id: str, config: dict) -> Path:
    out_dir = (REPO_ROOT / config["paths"]["output"]).resolve() / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _probe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló analizando {path}:\n{result.stderr[-2000:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe devolvió un JSON inválido para {path}: {exc}") from exc


def _parse_frame_rate(raw: str | None) -> float | None:
    if not raw:
        return None
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            num_f, den_f = float(num), float(den)
        except ValueError:
            return None
        return num_f / den_f if den_f else None
    try:
        return float(raw)
    except ValueError:
        return None


def _video_info(path: Path) -> dict:
    """Devuelve {"duration": float, "width": int, "height": int, "fps": float} de un vídeo."""
    probe = _probe(path)
    video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        raise ValueError(f"{path} no tiene ninguna pista de vídeo.")
    video_stream = video_streams[0]

    duration = None
    for candidate in (video_stream.get("duration"), probe.get("format", {}).get("duration")):
        if candidate is None:
            continue
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if duration is None:
        raise ValueError(f"No se pudo determinar la duración de {path}.")

    fps = _parse_frame_rate(video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")) or 30.0

    audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    audio_stream = audio_streams[0] if audio_streams else {}

    return {
        "duration": duration,
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "fps": fps,
        "sample_rate": int(audio_stream["sample_rate"]) if audio_stream.get("sample_rate") else None,
        "channels": audio_stream.get("channels"),
    }


def _run_ffmpeg(cmd: list[str], *, description: str) -> None:
    logger.info("%s...", description)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falló ({description}):\n{result.stderr[-4000:]}")


def detect_long_speech_segments(transcript: dict, cuts: list[dict], config: dict) -> list[dict]:
    """
    Agrupa palabras consecutivas de transcript['words'] cuyo hueco (gap)
    entre el final de una y el inicio de la siguiente es menor que
    config['edit']['long_speech_gap_seconds'], y devuelve los grupos
    resultantes cuya duración en el vídeo ORIGINAL es >=
    config['edit']['long_speech_min_seconds'].

    Los timestamps devueltos ya están remapeados a la línea de tiempo del
    vídeo EDITADO (después de aplicar `cuts`), listos para usarse
    directamente en el filtro de zoom sobre el clip ya cortado.

    Returns:
        [{"start": float, "end": float}, ...] en la línea de tiempo
        editada, ordenados por tiempo.
    """
    words = transcript.get("words", [])
    if not words:
        return []

    edit_config = config.get("edit", {})
    gap_threshold = float(edit_config.get("long_speech_gap_seconds", 1.2))
    min_seconds = float(edit_config.get("long_speech_min_seconds", 10.0))

    raw_runs: list[tuple[float, float]] = []
    run_start = float(words[0]["start"])
    run_end = float(words[0]["end"])
    for prev_word, word in zip(words, words[1:]):
        gap = float(word["start"]) - float(prev_word["end"])
        if gap <= gap_threshold:
            run_end = float(word["end"])
        else:
            raw_runs.append((run_start, run_end))
            run_start = float(word["start"])
            run_end = float(word["end"])
    raw_runs.append((run_start, run_end))

    sorted_cuts = sorted(cuts, key=lambda c: c["start"])

    long_runs: list[dict] = []
    for start, end in raw_runs:
        if end - start < min_seconds:
            continue
        edited_start = map_to_edited_timeline(start, sorted_cuts)
        edited_end = map_to_edited_timeline(end, sorted_cuts)
        if edited_end <= edited_start:
            continue
        long_runs.append({"start": edited_start, "end": edited_end})

    return long_runs


def _build_facecam_zoom_expr(speech_segments: list[dict], zoom_factor: float, ramp_seconds: float) -> str | None:
    """
    Expresión ffmpeg (evaluable con la variable `t`, en la línea de tiempo
    del clip ya cortado) para el efecto de zoom típico de streamer: durante
    la ventana [start, start+ramp_seconds] de cada tramo de speech_segments
    sube suavemente desde 1.0 hasta zoom_factor (curva coseno alzada, sin
    saltos), y CORTA SECO a 1.0 exactamente en t=start+ramp_seconds — no en
    el final del tramo de habla — porque `between(t,start,start+ramp)` deja
    de cumplirse instantáneamente ahí. El zoom NO se mantiene sostenido
    durante el resto del tramo de habla; su duración visible es siempre
    ramp_seconds. Vale 1.0 fuera de esa ventana.

    Si un tramo dura MENOS que ramp_seconds no da tiempo a completar la
    rampa antes de que el tramo termine, así que se descarta por completo
    (sin zoom en ese tramo) en vez de comprimir la rampa para que quepa —
    un zoom a medio completar que además no llega a mostrarse sostenido
    ni un instante sería más distracción que efecto.

    None si no hay tramos, el factor es <= 1.0, o ramp_seconds <= 0 (con
    corte en start+ramp_seconds, una rampa de 0s no llegaría a ser visible).
    """
    if not speech_segments or zoom_factor <= 1.0 or ramp_seconds <= 0:
        return None

    terms = []
    for seg in speech_segments:
        start, end = seg["start"], seg["end"]
        dur = end - start
        if dur < ramp_seconds:
            continue
        ramp_end = start + ramp_seconds
        level = f"0.5*(1-cos(PI*(t-{start:.6f})/{ramp_seconds:.6f}))"
        terms.append(f"if(between(t,{start:.6f},{ramp_end:.6f}),{level},0)")
    if not terms:
        return None

    nested = terms[0]
    for term in terms[1:]:
        nested = f"max({nested},{term})"
    return f"(1+({zoom_factor}-1)*({nested}))"


def _build_facecam_zoom_filters(
    zoom_expr: str, focus_x: float, focus_y: float, width: int, height: int
) -> tuple[str, str]:
    """
    Devuelve (scale_filter, crop_filter) que juntos implementan el zoom
    hacia la webcam: primero se agranda el frame ENTERO por zoom_expr(t)
    (filtro scale, que sí soporta expresiones dependientes de `t` vía
    eval=frame), y después se recorta una ventana de tamaño FIJO
    (width x height, el tamaño original) cuya posición sigue al punto
    (focus_x, focus_y) ya escalado — el filtro crop no tiene opción `eval`
    y sus parámetros w/h no aceptan `t` en las pruebas hechas contra este
    build de ffmpeg, pero x/y sí, así que el zoom en sí se hace con scale y
    solo el desplazamiento hacia la webcam con crop.

    A zoom=1.0 el frame escalado mide igual que el original, así que la
    ventana de recorte solo cabe en la posición (0,0): sin desplazamiento
    visible, tal y como se espera con zoom desactivado.
    """
    scale_filter = f"scale=w='trunc(iw*({zoom_expr})/2)*2':h='trunc(ih*({zoom_expr})/2)*2':eval=frame"
    crop_x = f"min(max({focus_x:.2f}*({zoom_expr})-{width}/2,0),in_w-{width})"
    crop_y = f"min(max({focus_y:.2f}*({zoom_expr})-{height}/2,0),in_h-{height})"
    crop_filter = f"crop=w={width}:h={height}:x='{crop_x}':y='{crop_y}'"
    return scale_filter, crop_filter


def _partition_by_length(
    items: list, build_filter_fn, max_chars: int, max_items: int | None = None
) -> list[list]:
    """
    Agrupa `items` en particiones consecutivas tal que
    len(build_filter_fn(partition)) no supere max_chars caracteres NI (si
    se indica max_items) el nº de items por partición supere max_items.
    Usado por _plan_zoom_passes -- el corte ya no arma ningún
    filter_complex (ver "Fuga de frames de vídeo en el filtro concat de
    ffmpeg" en el docstring del módulo), así que esta función solo
    particiona tramos de ZOOM hoy. Cada item aporta por sí solo un puñado
    de líneas de longitud acotada (nunca una fracción apreciable de
    max_chars), así que esto siempre progresa: ninguna partición queda
    vacía salvo que `items` lo esté.
    """
    partitions: list[list] = []
    current: list = []
    for item in items:
        candidate = current + [item]
        too_long = len(build_filter_fn(candidate)) > max_chars
        too_many = max_items is not None and len(candidate) > max_items
        if current and (too_long or too_many):
            partitions.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        partitions.append(current)
    return partitions


def _scan_keyframe_timestamps(path: Path) -> list[float]:
    """
    Lista ORDENADA de los timestamps (segundos, pts_time) de todos los
    keyframes de la pista de vídeo de `path`, vía ffprobe -- solo demuxea
    paquetes (lee sus flags), sin decodificar ningún frame, así que es
    barato incluso en grabaciones de 1-2h (ver "Renderizado parcial sin
    pérdida" en el docstring del módulo). Usado por _cut_video para
    decidir qué parte interior de cada tramo a conservar se puede copiar
    sin recodificar.
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló escaneando keyframes de {path}:\n{result.stderr[-2000:]}")
    keyframes: list[float] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 2 or "K" not in parts[1]:
            continue
        try:
            keyframes.append(float(parts[0]))
        except ValueError:
            continue
    keyframes.sort()
    return keyframes


def _count_video_frames(path: Path) -> int:
    """
    Cuenta los frames de la pista de vídeo de `path` sin decodificar
    (solo demuxea paquetes, `ffprobe -count_packets`) -- barato, mismo
    principio que _scan_keyframe_timestamps. Usado por _cut_video para
    medir la duración de vídeo REAL (en frames, no en segundos con
    redondeo de punto flotante) de cada fragmento, y así poder darle a
    `_local_crossfade_concat` un objetivo de audio que coincida con el
    VÍDEO real de ese fragmento -- ver "Vídeo más largo que audio en cada
    recodificación" en el docstring del módulo para el porqué.
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló contando frames de {path}:\n{result.stderr[-2000:]}")
    return int(result.stdout.strip())


def _scan_video_pts(path: Path) -> list[float]:
    """
    Lista ORDENADA (por tiempo de PRESENTACIÓN, no de aparición en el
    archivo) de los PTS de todos los paquetes de la pista de vídeo de
    `path` -- ffprobe, solo demuxea paquetes, sin decodificar, mismo
    principio que _scan_keyframe_timestamps/_count_video_frames (barato
    incluso en grabaciones de 1-2h). Usado por _cut_video para medir la
    posición REAL de cada `keep_segment` en el vídeo YA UNIDO -- ver
    "Redondeo del concat demuxer en el vídeo ya unido" en el docstring
    del módulo para el porqué de necesitar esto en vez de fiarse de
    `frames/fps` sobre los fragmentos SIN unir.
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló escaneando PTS de vídeo de {path}:\n{result.stderr[-2000:]}")
    pts = [float(line) for line in result.stdout.splitlines() if line.strip()]
    pts.sort()
    return pts


def _group_video_durations_from_pts(
    frame_counts: list[int], real_boundary: list[bool], sorted_pts: list[float], total_duration_s: float
) -> list[float]:
    """
    Agrupa `frame_counts` (uno por fragmento de VÍDEO, mismo orden y
    mismo criterio de fronteras que `_decode_fragment_groups` -- ver ese
    docstring) por `keep_segment`, y devuelve la duración REAL de cada
    grupo tal y como quedó en el vídeo YA UNIDO: la diferencia entre el
    PTS del primer frame del grupo SIGUIENTE y el PTS del primer frame de
    ESTE grupo (o `total_duration_s` menos el PTS de inicio, para el
    último grupo) -- `sorted_pts` debe venir de `_scan_video_pts(cut_path)`
    (el archivo YA UNIDO), NO de fragmentos sin unir.

    Por qué NO basta con `frames/fps` sobre los fragmentos sin unir
    (encontrado 2026-08-15, ver "Redondeo del concat demuxer en el vídeo
    ya unido" en el docstring del módulo): cada fragmento individual
    tiene metadata de duración exacta (verificado: 0.00ms de diferencia
    entre su propio `frames/fps` y su propia duración declarada), pero el
    concat DEMUXER introduce un pequeño redondeo de PTS en cada UNIÓN al
    encadenarlos -- proporcional al Nº DE UNIONES, no al tiempo
    transcurrido, así que se concentra en tramos con muchos cortes
    seguidos (muchos fragmentos cortos, y por tanto muchas uniones) en
    vez de repartirse uniformemente. Medir la posición real en el archivo
    YA UNIDO captura este efecto directamente, sea cual sea su causa
    exacta -- no hace falta modelarlo.
    """
    durations: list[float] = []
    cum_frames = 0
    group_start_frame = 0
    total = len(frame_counts)
    for i, fc in enumerate(frame_counts):
        cum_frames += fc
        is_last = i == total - 1
        if is_last or real_boundary[i]:
            start_pts = sorted_pts[group_start_frame]
            end_pts = sorted_pts[cum_frames] if cum_frames < len(sorted_pts) else total_duration_s
            durations.append(end_pts - start_pts)
            group_start_frame = cum_frames
    return durations


def _keyframe_at_or_after(keyframes: list[float], t: float) -> float | None:
    """Primer keyframe >= t de `keyframes` (ya ordenada), o None si no hay ninguno."""
    i = bisect.bisect_left(keyframes, t)
    return keyframes[i] if i < len(keyframes) else None


def _keyframe_at_or_before(keyframes: list[float], t: float) -> float | None:
    """Último keyframe <= t de `keyframes` (ya ordenada), o None si no hay ninguno."""
    i = bisect.bisect_right(keyframes, t) - 1
    return keyframes[i] if i >= 0 else None


def _cut_segment_recode(input_path: Path, start: float, end: float, out_path: Path, description: str) -> None:
    """
    Recodifica [start, end] (timestamps absolutos del vídeo de entrada) a
    out_path -- crf16/veryfast, con un simple trim de entrada (-ss/-to
    antes de -i, preciso a nivel de frame) -- SIN ningún filtro `concat`
    ni `filter_complex` (ver "Fuga de frames de vídeo en el filtro concat
    de ffmpeg" en el docstring del módulo). Usado tanto para el fallback
    de tramo completo como para la cabeza/cola del renderizado parcial
    sin pérdida (ver _cut_segment_smart).
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
        "-i", str(input_path),
        "-c:v", "libx264", "-crf", _CUT_SHARD_CRF, "-preset", _CUT_SHARD_PRESET, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=description)


def _cut_segment_copy(
    input_path: Path, start: float, end: float, fps: float, out_path: Path, description: str
) -> None:
    """
    Copia el VÍDEO de [start, end] SIN recodificar (-c:v copy,
    prácticamente gratis) pero RECODIFICA el AUDIO (barato -- codificar
    audio es rápido comparado con vídeo, que es la parte cara que este
    mecanismo evita recodificar). Solo válido cuando `start` y `end` caen
    EXACTAMENTE en keyframes reales del vídeo de entrada (ver
    _cut_segment_smart y "Renderizado parcial sin pérdida" en el docstring
    del módulo).

    Por qué el audio NO puede copiarse sin más aquí (bug real encontrado en
    producción, 2026-08-11 -- ver "Solape de audio/vídeo en la costura del
    renderizado parcial sin pérdida" en el docstring del módulo para el
    análisis completo): `start`/`end` son keyframes reales del stream de
    VÍDEO, pero el audio (paquetes AAC de 1024 muestras, ~21.3ms a 48kHz)
    tiene su propia rejilla temporal, independiente del GOP de vídeo. En
    modo -c copy puro, ffmpeg no puede decodificar+descartar muestras para
    arrancar exactamente en `start` (solo puede si va a recodificar), así
    que el primer paquete de audio copiado es el que YA sonaba antes de
    `start` -- contenido que el fragmento anterior (recodificado con seek
    preciso) ya incluyó. Medido con un vídeo sintético: ~33-47ms de audio
    duplicado en cada costura de este tipo -- exactamente el "tartamudeo
    de sílaba" reportado en vídeos ya publicados. Recodificar el audio
    aquí (mismo -ss/-to, ahora con seek preciso porque SÍ se decodifica)
    arranca y termina exactamente en start/end -- medido en 0.0ms de
    solape tras el fix.

    Por qué el vídeo tampoco puede fiarse de `-to` a secas para el límite
    superior (segundo hallazgo de la misma investigación): con B-frames
    (el preset por defecto de ingest/run.py los deja activados, no se
    pasa `-bf 0`), `-to` en modo -c copy puede "gotear" varios frames del
    GOP SIGUIENTE más allá de `end` -- el reordenamiento por B-frames hace
    que ffmpeg ya haya leído esos frames (los necesita para decodificar
    los B-frames del final del GOP actual) antes de que el corte por
    tiempo tenga ocasión de excluirlos. Medido: hasta ~130ms/4 frames de
    vídeo de más en el vídeo sintético de prueba -- un frame "congelado y
    repetido" en la costura, el equivalente visual del tartamudeo de
    audio.

    Por qué esto se arregla en DOS pasadas en vez de añadir `-frames:v`
    directamente a la llamada de arriba (probado primero, descartado):
    combinar `-frames:v <N>` con el `-ss`/`-to` que recorta un tramo de
    VARIOS GOPs del vídeo de entrada SÍ da el recuento de frames correcto,
    pero corrompe el CONTENIDO por el camino -- verificado con un vídeo
    sintético de 19 GOPs (comparación de hash por frame, sin usar `-ss`
    para verificar, que resultó ser poco fiable para esto, ver más abajo):
    el recuento final es exacto, pero los frames a partir de cierto punto
    intermedio ya no son los que tocan. Aparentemente `-frames:v` cuenta
    frames de SALIDA mientras el reordenamiento por B-frames sigue en
    marcha, y con varios GOPs de por medio la cuenta se desincroniza del
    contenido real. En dos pasadas SEPARADAS (1: copiar con `-to`, aceptando
    el goteo de la cola; 2: remuxear ese resultado ya con un único límite
    de frames, sin ningún `-ss`/`-to` de por medio) cada pasada solo hace
    una cosa a la vez y no se corrompe nada -- verificado bit-idéntico
    frame a frame contra el origen (todo el tramo, no solo el punto medio).

    Nota sobre cómo se verificó esto (importante si se vuelve a tocar este
    código): comparar frames por tiempo con `ffmpeg -ss T -i archivo
    -frames:v 1` para verificar bit-identidad NO es fiable en este build de
    ffmpeg contra archivos con B-frames -- puede aterrizar en un frame
    vecino sin avisar (confirmado: el mismo archivo, probado con `-ss` vs.
    con un volcado secuencial completo por índice de frame vía
    `-vf select`, da resultados distintos). La verificación real de
    bit-identidad debe hacerse con un volcado secuencial (`-f framemd5`,
    sin `-ss`) de ambos lados y comparar la lista completa -- no probar
    puntos sueltos con `-ss`.
    """
    n_frames = max(1, round((end - start) * fps))
    untrimmed_path = out_path.with_name(out_path.stem + "_untrimmed" + out_path.suffix)
    cmd_cut = [
        "ffmpeg", "-y",
        "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
        "-i", str(input_path),
        "-c:v", "copy",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        str(untrimmed_path),
    ]
    _run_ffmpeg(cmd_cut, description=description)
    cmd_trim = [
        "ffmpeg", "-y",
        "-i", str(untrimmed_path),
        "-c", "copy", "-frames:v", str(n_frames),
        str(out_path),
    ]
    _run_ffmpeg(cmd_trim, description=f"{description} (recorte exacto de frames de vídeo)")
    untrimmed_path.unlink(missing_ok=True)


def _cut_segment_smart(
    input_path: Path, start: float, end: float, keyframes: list[float], fps: float,
    index: int, total: int, out_dir: Path,
) -> list[Path]:
    """
    Recorta el tramo [start, end] (timestamps absolutos del vídeo de
    entrada) a uno o más archivos aislados, aplicando "renderizado
    parcial sin pérdida" (ver docstring del módulo): busca el primer
    keyframe >= start (`kf_start`) y el último keyframe <= end
    (`kf_end`); si hay hueco entre ambos, recodifica solo la cabeza
    [start, kf_start) y la cola [kf_end, end) (omitidas si ya están
    vacías) y copia sin recodificar el interior [kf_start, kf_end) --
    mucho más barato que recodificar el tramo entero. Si el tramo es más
    corto que un intervalo de keyframe (no hay hueco útil), cae al
    comportamiento de siempre: recodificar el tramo completo.

    Returns:
        Lista de 1 a 3 fragmentos, EN ORDEN, listos para pegarse con el
        concat demuxer junto con los del resto de tramos.
    """
    kf_start = _keyframe_at_or_after(keyframes, start)
    kf_end = _keyframe_at_or_before(keyframes, end)
    useful_gap = kf_start is not None and kf_end is not None and kf_end > kf_start

    if not useful_gap:
        out_path = out_dir / f"_cut_seg_{index}_full.mp4"
        _cut_segment_recode(
            input_path, start, end, out_path,
            description=(
                f"Cortando tramo {index + 1}/{total} completo "
                f"({start:.2f}s-{end:.2f}s, sin hueco interior copiable)"
            ),
        )
        return [out_path]

    fragments: list[Path] = []
    if kf_start > start:
        head_path = out_dir / f"_cut_seg_{index}_head.mp4"
        _cut_segment_recode(
            input_path, start, kf_start, head_path,
            description=f"Cortando tramo {index + 1}/{total}, cabeza ({start:.2f}s-{kf_start:.2f}s)",
        )
        fragments.append(head_path)

    mid_path = out_dir / f"_cut_seg_{index}_mid.mp4"
    _cut_segment_copy(
        input_path, kf_start, kf_end, fps, mid_path,
        description=(
            f"Copiando tramo {index + 1}/{total}, interior sin recodificar "
            f"({kf_start:.2f}s-{kf_end:.2f}s)"
        ),
    )
    fragments.append(mid_path)

    if end > kf_end:
        tail_path = out_dir / f"_cut_seg_{index}_tail.mp4"
        _cut_segment_recode(
            input_path, kf_end, end, tail_path,
            description=f"Cortando tramo {index + 1}/{total}, cola ({kf_end:.2f}s-{end:.2f}s)",
        )
        fragments.append(tail_path)

    return fragments


def _glue_video_files(paths: list[Path], out_path: Path) -> None:
    """
    Pega los archivos de `paths` (ya completos, cada uno un fragmento del
    vídeo final -- recodificado por _cut_segment_recode o copiado sin
    recodificar por _cut_segment_copy, ambos con los mismos parámetros de
    stream) con el concat DEMUXER, SIN inpoint/outpoint (solo
    `file '...'` por entrada) y `-c copy`: rápido y exacto porque no
    recorta dentro de ningún archivo a mitad de GOP (ver la nota del
    docstring del módulo sobre por qué NO se usa inpoint/outpoint para
    cortar), y no pasa por el filtro `concat` de ffmpeg -- mismo mecanismo
    ya validado en la vía rápida de append_outro.
    """
    list_path = out_path.with_suffix(".txt")
    list_lines = [f"file '{Path(p).resolve().as_posix()}'" for p in paths]
    list_path.write_text("\n".join(list_lines) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=f"Uniendo {len(paths)} tramo(s) cortado(s)")
    list_path.unlink(missing_ok=True)


# Formato de audio interno fijo para el crossfade -- coincide con lo que
# YA producen _cut_segment_recode/_cut_segment_copy (48kHz estéreo) para
# todos los fragmentos, así que decodificar a este formato nunca hace
# falta re-muestrear ni mezclar canales.
_CROSSFADE_SR = 48000
_CROSSFADE_CHANNELS = 2

# El bucle de decodificación de _decode_fragment_groups lanza un
# proceso ffmpeg por fragmento (pueden ser cientos en una grabación larga)
# sin ninguna otra señal de progreso -- en una ejecución real contra un
# vídeo de ~1h44m/706 fragmentos esto se quedó en silencio el tiempo
# suficiente para que el proceso en background fuera matado externamente
# (mismo síntoma ya documentado para cv2.VideoCapture/librosa: una llamada
# bloqueante larga sin ninguna salida se interpreta como colgada). Log de
# progreso periódico, mismo patrón que compute_motion_timeseries en
# detect_cuts (cada N fragmentos O cada M segundos, lo que llegue antes).
_CROSSFADE_PROGRESS_EVERY_FRAGMENTS = 50
_CROSSFADE_PROGRESS_EVERY_SECONDS = 10.0


def _decode_audio_float32(path: Path) -> "np.ndarray":
    """
    Decodifica el audio de `path` a un array numpy float32
    (n_muestras, _CROSSFADE_CHANNELS) a _CROSSFADE_SR, vía ffmpeg (pipeado
    por stdout, sin archivo temporal intermedio) -- soporta cualquier
    códec/contenedor de entrada, no solo los formatos que soundfile puede
    leer directamente (los fragmentos son .mp4/AAC).
    """
    import numpy as np

    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ar", str(_CROSSFADE_SR), "-ac", str(_CROSSFADE_CHANNELS),
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg falló decodificando audio de {path}:\n"
            f"{result.stderr[-2000:].decode('utf-8', errors='replace')}"
        )
    audio = np.frombuffer(result.stdout, dtype=np.float32)
    if audio.size % _CROSSFADE_CHANNELS != 0:
        audio = audio[: audio.size - (audio.size % _CROSSFADE_CHANNELS)]
    return audio.reshape(-1, _CROSSFADE_CHANNELS)


def _decode_fragment_groups(fragment_paths: list[Path], real_boundary: list[bool]) -> list["np.ndarray"]:
    """
    Decodifica cada fragmento de `fragment_paths` (en orden) y los agrupa
    por `keep_segment`: los fragmentos consecutivos separados por una
    frontera INTERNA espuria (`real_boundary[i] is False`) se concatenan
    en SECO (sin crossfade, mismo criterio que antes) en un único array
    por grupo; cada frontera REAL (`True`) empieza un grupo nuevo. El
    resultado es, para cada `keep_segment`, su audio COMPLETO tal cual se
    extrajo (longitud exacta, sin ningún acortamiento todavía -- eso lo
    aplica después `_local_crossfade_concat`).

    Loguea progreso periódico (mismo motivo que antes: un proceso ffmpeg
    de decodificación por fragmento, pueden ser cientos en una grabación
    larga, sin esto se interpretó como colgado en producción -- ver
    "Bug de fiabilidad... 2026-08-12" en el docstring del módulo).
    """
    import numpy as np

    if len(real_boundary) != max(0, len(fragment_paths) - 1):
        raise ValueError(
            f"real_boundary debe tener {max(0, len(fragment_paths) - 1)} elemento(s) "
            f"(uno por frontera entre fragmentos consecutivos), tiene {len(real_boundary)}"
        )

    groups: list[np.ndarray] = []
    current: list[np.ndarray] = []

    total = len(fragment_paths)
    inicio = time.monotonic()
    ultimo_log = inicio

    for i, path in enumerate(fragment_paths):
        audio = _decode_audio_float32(path)
        current.append(audio)

        ahora = time.monotonic()
        if (i + 1) % _CROSSFADE_PROGRESS_EVERY_FRAGMENTS == 0 or (ahora - ultimo_log) >= _CROSSFADE_PROGRESS_EVERY_SECONDS or i + 1 == total:
            logger.info(
                "Progreso crossfade de audio: %d/%d fragmento(s) decodificado(s), %.1fs transcurridos",
                i + 1, total, ahora - inicio,
            )
            ultimo_log = ahora

        is_last = i == total - 1
        if is_last or real_boundary[i]:
            groups.append(current[0] if len(current) == 1 else np.concatenate(current, axis=0))
            current = []

    return groups


def _local_crossfade_concat(
    groups: list["np.ndarray"], crossfade_ms: float, target_lengths: list[int]
) -> "np.ndarray":
    """
    Aplica un crossfade EQUAL-POWER (curva coseno/seno -- la misma curva
    que implementa el filtro `acrossfade` de ffmpeg con `curve=qsin`, el
    estándar recomendado para crossfades cortos de voz: mantiene la
    potencia percibida constante durante la transición, a diferencia de
    un crossfade LINEAL que sí produce un bajón de volumen perceptible en
    el punto medio) entre cada `keep_segment` de `groups` (en orden;
    TODAS las fronteras entre grupos son reales por construcción, ver
    _decode_fragment_groups) -- ver "Micro-crossfade en los empalmes de
    audio" en el docstring del módulo para el porqué y para por qué esto
    se implementa en numpy en vez de encadenar el filtro `acrossfade` de
    ffmpeg.

    A diferencia de la primera versión de este mecanismo (un único
    reestiramiento GLOBAL al final, ver `_resample_to_length` y
    "Reestirado LOCAL en vez de global" en el docstring del módulo), cada
    `keep_segment` se reestira aquí INDIVIDUALMENTE justo después de
    aplicar su crossfade, de vuelta a `target_lengths[i]` -- el objetivo
    de ESE segmento concreto, en muestras. `target_lengths[i]` debe ser
    la duración REAL DEL VÍDEO de ese `keep_segment` (medida en frames,
    ver `_count_video_frames` y "Vídeo más largo que audio en cada
    recodificación" en el docstring del módulo), NO `len(groups[i])` (la
    longitud tal cual quedó el audio al extraerlo) -- ambas pueden
    diferir unos pocos ms incluso sin crossfade de por medio, porque la
    recodificación de vídeo y la de audio no redondean igual al mismo
    `-ss`/`-to`. Así el acortamiento del crossfade Y cualquier diferencia
    de precisión entre la extracción de audio y la de vídeo se corrigen
    ambos exactamente DONDE ocurren, en vez de repartirse con una única
    tasa global sobre todo el vídeo. La suma final es, por construcción,
    exactamente `sum(target_lengths)` -- si estos vienen de medir el
    vídeo real, esa suma YA es la duración total del vídeo, así que el
    resultado queda sample-exacto sin necesitar ningún reestirado global
    posterior (ver _write_crossfaded_audio, que conserva uno residual
    minúsculo solo como red de seguridad).

    `crossfade_ms` <= 0 desactiva el crossfade (cada grupo se concatena
    tal cual y se reestira a su target_length -- útil como interruptor de
    config y como caso base para verificar que el crossfade en sí es lo
    que cambia el resultado).

    Devuelve un único array numpy float32 (n_muestras, _CROSSFADE_CHANNELS).
    """
    import numpy as np

    if not groups:
        return np.zeros((0, _CROSSFADE_CHANNELS), dtype=np.float32)
    if len(target_lengths) != len(groups):
        raise ValueError(
            f"target_lengths debe tener {len(groups)} elemento(s) (uno por keep_segment), "
            f"tiene {len(target_lengths)}"
        )

    n = max(0, int(round(_CROSSFADE_SR * crossfade_ms / 1000)))
    outputs: list[np.ndarray] = []
    pending = groups[0]
    pending_target_len = target_lengths[0]

    for gi in range(1, len(groups)):
        group = groups[gi]
        this_n = min(n, len(pending), len(group))
        if this_n > 0:
            t = np.linspace(0.0, np.pi / 2, this_n, dtype=np.float32).reshape(-1, 1)
            fade_out = np.cos(t)
            fade_in = np.sin(t)
            blended = pending[-this_n:] * fade_out + group[:this_n] * fade_in
            finalized = blended if len(pending) <= this_n else np.concatenate([pending[:-this_n], blended], axis=0)
            remainder = group[this_n:]
        else:
            # uno de los dos lados no tiene muestras que ofrecer (grupo
            # vacío tras un recorte degenerado) -- no hay nada que
            # mezclar, se concatena en seco en este punto concreto.
            finalized = pending
            remainder = group

        outputs.append(_resample_to_length(finalized, pending_target_len))
        pending = remainder
        pending_target_len = target_lengths[gi]

    outputs.append(_resample_to_length(pending, pending_target_len))
    return np.concatenate(outputs, axis=0)


def _write_crossfaded_audio(
    fragment_paths: list[Path], crossfade_ms: float, real_boundary: list[bool],
    segment_video_durations_s: list[float], total_target_duration_s: float, out_wav_path: Path,
) -> None:
    """
    Agrupa `fragment_paths` por `keep_segment` (_decode_fragment_groups) y
    aplica el crossfade con reestirado LOCAL por segmento
    (_local_crossfade_concat -- ver ese docstring para el porqué), y
    escribe el resultado como WAV (intermedio sin pérdida).

    `segment_video_durations_s` es la duración REAL DEL VÍDEO (medida en
    frames, no derivada del audio) de cada `keep_segment` POR SEPARADO,
    en el MISMO orden que los grupos que produce `_decode_fragment_groups`
    -- ver "Vídeo más largo que audio en cada recodificación" en el
    docstring del módulo para el porqué de medir esto por separado en vez
    de fiarse de la longitud con la que se extrajo el audio de cada
    fragmento. `total_target_duration_s` es la duración TOTAL real del
    vídeo YA UNIDO (`_video_info(cut_path)`, medida de forma
    INDEPENDIENTE, no derivada de sumar `segment_video_durations_s`) --
    IMPORTANTE que sea independiente: si se derivara de la misma suma que
    ya corrige `_local_crossfade_concat`, el reestirado final dejaría de
    ser una red de seguridad de verdad (no podría detectar ninguna
    discrepancia entre medir cada fragmento por separado ANTES de unirlos
    y medir el archivo YA UNIDO, sea cual sea su origen).

    El resultado de _local_crossfade_concat mide, por construcción,
    exactamente `sum(segment_video_durations_s)` -- que DEBERÍA coincidir
    con `total_target_duration_s`, pero el reestirado final a
    `total_target_duration_s` (independiente) sigue aplicándose aquí como
    red de seguridad genuina, no como la corrección principal (esa ya se
    aplicó localmente, por segmento, en _local_crossfade_concat).
    """
    import soundfile as sf

    groups = _decode_fragment_groups(fragment_paths, real_boundary)
    target_lengths = [round(d * _CROSSFADE_SR) for d in segment_video_durations_s]
    audio = _local_crossfade_concat(groups, crossfade_ms, target_lengths)
    total_target_length = round(total_target_duration_s * _CROSSFADE_SR)
    audio = _resample_to_length(audio, total_target_length)
    sf.write(str(out_wav_path), audio, _CROSSFADE_SR, subtype="FLOAT")


def _resample_to_length(audio: "np.ndarray", target_length: int) -> "np.ndarray":
    """
    Estira/comprime `audio` (n_muestras, canales) a EXACTAMENTE
    `target_length` muestras mediante interpolación lineal.

    Por qué hace falta (consecuencia matemática ineludible de cualquier
    crossfade real, no un error): cada crossfade de `crossfade_ms` funde
    2 tramos de N muestras "distintas" (N del final del primero + N del
    principio del segundo) en solo N muestras de salida -- eso ACORTA el
    audio combinado en N muestras por cada empalme, por construcción
    (es literalmente lo que hace un crossfade: solapar contenido en vez
    de concatenarlo seco). El VÍDEO, sin embargo, NO se acorta (sigue
    concatenado tal cual, sin crossfade, tal y como se pide). Sin
    corregir esto, el audio quedaría cada vez más "adelantado" respecto
    al vídeo a medida que se acumulan empalmes (varios segundos en una
    grabación de 1-2h con cientos de cortes) -- un desincronismo
    progresivo real, no cosmético, que además rompería la calibración de
    subtítulos (que asume que la deriva es la de redondeo de keyframes ya
    documentada, mucho más pequeña).

    Usada en DOS sitios con dos objetivos distintos (ver "Reestirado LOCAL
    en vez de global" en el docstring del módulo para la investigación
    completa que llevó a esto):

    1. `_local_crossfade_concat` la llama una vez POR `keep_segment`,
       justo después de aplicarle su crossfade, para devolverlo a su
       propia longitud EXACTA -- el mecanismo PRINCIPAL de corrección,
       LOCAL (cada segmento corrige solo su propio acortamiento, donde
       ocurrió). La magnitud aquí es proporcional a cuánto pesa
       `crossfade_ms` sobre la duración de ESE segmento concreto (más en
       segmentos cortos/tramos con cortes muy seguidos, menos en
       segmentos largos) -- siempre muy por debajo del umbral perceptible
       de cambio de tempo/tono en audio hablado (referencia habitual:
       2-5%) incluso en el caso más corto realista
       (`min_kept_segment_seconds`, 0.6s por defecto: ~3.3% con
       crossfade_ms=20).
    2. `_write_crossfaded_audio` la llama una ÚLTIMA vez, GLOBAL, sobre
       el audio ya localmente corregido -- una red de seguridad para la
       discrepancia MINÚSCULA que pueda quedar frente a la duración real
       del vídeo (redondeo de la propia extracción de audio de cada
       fragmento, del orden de unas pocas muestras acumuladas, no
       segundos) -- normalmente un no-op o casi.

    En ambos casos, una interpolación lineal simple (sin librerías de
    time-stretch con preservación de tono) es suficiente a estas
    magnitudes, no introduce artefactos audibles.
    """
    import numpy as np

    if len(audio) == 0 or target_length <= 0 or len(audio) == target_length:
        return audio
    old_idx = np.linspace(0.0, 1.0, num=len(audio), endpoint=True)
    new_idx = np.linspace(0.0, 1.0, num=target_length, endpoint=True)
    stretched = np.empty((target_length, audio.shape[1]), dtype=np.float32)
    for ch in range(audio.shape[1]):
        stretched[:, ch] = np.interp(new_idx, old_idx, audio[:, ch]).astype(np.float32)
    return stretched


def _replace_audio_track(video_path: Path, wav_path: Path, out_path: Path) -> None:
    """
    Combina el VÍDEO de `video_path` (sin recodificar, `-c:v copy`) con el
    AUDIO de `wav_path` (recodificado a AAC, único punto de todo el
    pipeline de audio-crossfade que pasa por un códec con pérdida --
    justo antes de esto todo ha sido PCM/WAV sin pérdida, así que el
    crossfade en sí y la concatenación de N fragmentos no acumulan
    generaciones de recodificación con pérdida).
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path), "-i", str(wav_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-ar", str(_CROSSFADE_SR), "-ac", str(_CROSSFADE_CHANNELS), "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description="Sustituyendo el audio por la versión con micro-crossfade en los empalmes")


def _cut_video(
    input_path: Path, keep_segments: list[tuple[float, float]], fps: float, out_dir: Path,
    audio_crossfade_ms: float = 0.0,
) -> Path:
    """
    Recorta `keep_segments` de `input_path` aplicando "renderizado
    parcial sin pérdida" (ver docstring del módulo): cada tramo se corta
    con _cut_segment_smart, que copia sin recodificar (-c:v copy) el
    VÍDEO del interior de cada tramo entre dos keyframes reales
    (recodificando el AUDIO de ese mismo interior, con seek preciso -- ver
    "Solape de audio/vídeo en la costura del renderizado parcial sin
    pérdida" y _cut_segment_copy) y solo recodifica vídeo+audio de los
    bordes (o el tramo completo, como fallback, si es más corto que un
    intervalo de keyframe) -- sin `concat` ni `filter_complex` en ningún
    caso (ver "Fuga de frames de vídeo en el filtro concat de ffmpeg").
    Todos los fragmentos resultantes (1 a 3 por tramo) se pegan después
    con el concat DEMUXER (_glue_video_files). Sin límite práctico de nº
    de tramos: cada fragmento es una llamada de ffmpeg independiente y
    corta, así que ni el nº de tramos ni sus caracteres pueden acercar la
    línea de comandos al límite de Windows.

    `fps` (del vídeo de entrada, CFR desde ingest/run.py) se usa para
    calcular el recuento exacto de frames de vídeo a copiar en cada
    interior (`-frames:v`, ver _cut_segment_copy) -- necesario porque
    `-to` en modo -c copy no es fiable como límite superior cuando hay
    B-frames.

    `audio_crossfade_ms` > 0 (ver "Micro-crossfade en los empalmes de
    audio" en el docstring del módulo) sustituye el AUDIO del resultado
    por una versión con crossfade equal-power en CADA unión entre
    fragmentos -- el vídeo se sigue concatenando exactamente igual (sin
    crossfade, ver _glue_video_files). <= 0 desactiva el crossfade
    (comportamiento idéntico al de antes de esta función existir).

    Returns:
        Ruta al vídeo ya cortado, sin zoom (data/output/<video_id>/_cuts.mp4).
    """
    t0 = time.monotonic()
    keyframes = _scan_keyframe_timestamps(input_path)
    logger.info(
        "%d keyframe(s) encontrados en %s (%.1fs, solo demux) para el renderizado parcial sin pérdida",
        len(keyframes), input_path.name, time.monotonic() - t0,
    )

    logger.info(
        "Cortando %d tramo(s) a conservar (interior copiado sin recodificar cuando hay hueco entre "
        "keyframes; recodificación completa como fallback)",
        len(keep_segments),
    )
    segment_paths: list[Path] = []
    # boundary_is_real[i] indica si la frontera entre segment_paths[i] y
    # segment_paths[i + 1] es un corte REAL entre dos keep_segments
    # distintos (True) o una frontera INTERNA espuria entre los
    # sub-fragmentos head/mid/tail de un mismo keep_segment, generada solo
    # por el renderizado parcial sin pérdida (False) -- ver "Fronteras
    # internas espurias del renderizado parcial sin pérdida" en el
    # docstring del módulo. Cada keep_segment aporta como mucho una
    # frontera real (con el anterior) y len(fragments)-1 fronteras
    # internas (consigo mismo).
    boundary_is_real: list[bool] = []
    n_partial = 0
    n_full_recode = 0
    for i, (start, end) in enumerate(keep_segments):
        fragments = _cut_segment_smart(input_path, start, end, keyframes, fps, i, len(keep_segments), out_dir)
        if segment_paths:
            boundary_is_real.append(True)
        boundary_is_real.extend([False] * (len(fragments) - 1))
        segment_paths.extend(fragments)
        if len(fragments) == 1:
            n_full_recode += 1
        else:
            n_partial += 1
    logger.info(
        "%d tramo(s) con interior copiado sin recodificar, %d tramo(s) recodificados por completo "
        "(sin hueco interior útil)",
        n_partial, n_full_recode,
    )

    cut_path = out_dir / "_cuts.mp4"
    if len(segment_paths) == 1:
        shutil.move(str(segment_paths[0]), str(cut_path))
        return cut_path

    _glue_video_files(segment_paths, cut_path)

    if audio_crossfade_ms > 0:
        t_cf = time.monotonic()
        n_real = sum(boundary_is_real)
        n_internal = len(boundary_is_real) - n_real
        logger.info(
            "Aplicando micro-crossfade de audio (%.0fms, equal-power) en %d frontera(s) real(es) entre "
            "tramos distintos (%d frontera(s) interna(s) espuria(s) del renderizado parcial sin pérdida "
            "se concatenan en seco, sin crossfade)...",
            audio_crossfade_ms, n_real, n_internal,
        )

        # Nº de frames de vídeo de cada fragmento -- solo para saber
        # cuántos frames aporta cada uno al conjunto (no su duración: esa
        # se mide directamente del vídeo YA UNIDO más abajo, ver "Redondeo
        # del concat demuxer en el vídeo ya unido" en el docstring del
        # módulo). Log de progreso periódico por el mismo motivo que el
        # bucle de decodificación de audio (cientos de llamadas a ffprobe).
        t_fc = time.monotonic()
        ultimo_log_fc = t_fc
        frame_counts: list[int] = []
        for i, p in enumerate(segment_paths):
            frame_counts.append(_count_video_frames(p))
            ahora = time.monotonic()
            if (i + 1) % _CROSSFADE_PROGRESS_EVERY_FRAGMENTS == 0 or (ahora - ultimo_log_fc) >= _CROSSFADE_PROGRESS_EVERY_SECONDS or i + 1 == len(segment_paths):
                logger.info(
                    "Progreso medición de vídeo real: %d/%d fragmento(s), %.1fs transcurridos",
                    i + 1, len(segment_paths), ahora - t_fc,
                )
                ultimo_log_fc = ahora

        # Duración REAL de cada keep_segment, medida directamente sobre
        # los PTS del vídeo YA UNIDO (`cut_path`) -- NO frames/fps sobre
        # los fragmentos SIN unir. Necesario porque el concat demuxer
        # introduce un pequeño redondeo de PTS en cada unión al pegar los
        # fragmentos (ver "Redondeo del concat demuxer en el vídeo ya
        # unido" en el docstring del módulo) -- frames/fps sobre cada
        # fragmento por separado es exacto para ESE fragmento, pero no
        # refleja ese redondeo acumulado, que se concentra justo en los
        # tramos con más cortes seguidos (más fragmentos, más uniones).
        total_target_duration = _video_info(cut_path)["duration"]
        sorted_pts = _scan_video_pts(cut_path)
        segment_video_durations_s = _group_video_durations_from_pts(
            frame_counts, boundary_is_real, sorted_pts, total_target_duration,
        )

        wav_path = out_dir / "_cuts_crossfade_audio.wav"
        _write_crossfaded_audio(
            segment_paths, audio_crossfade_ms, boundary_is_real,
            segment_video_durations_s, total_target_duration, wav_path,
        )
        crossfaded_path = out_dir / "_cuts_crossfade.mp4"
        _replace_audio_track(cut_path, wav_path, crossfaded_path)
        wav_path.unlink(missing_ok=True)
        cut_path.unlink(missing_ok=True)
        cut_path = crossfaded_path
        logger.info("Crossfade de audio aplicado en %.1fs", time.monotonic() - t_cf)

    for p in segment_paths:
        Path(p).unlink(missing_ok=True)
    return cut_path


def _build_zoom_pass_filter_complex(
    speech_segments: list[dict], zoom_factor: float, ramp_seconds: float,
    focus_x: float, focus_y: float, width: int, height: int,
) -> str | None:
    """
    filter_complex que aplica el zoom hacia la webcam de `speech_segments`
    sobre [0:v] (un único vídeo de entrada -- el ya cortado por
    _cut_video), dejando el resultado en [vout]. Solo trata vídeo: el
    audio no pasa por aquí, se copia sin recodificar (ver _apply_zoom).
    None si _build_facecam_zoom_expr no genera ninguna expresión (ningún
    tramo de este subconjunto llega a completar la rampa).
    """
    zoom_expr = _build_facecam_zoom_expr(speech_segments, zoom_factor, ramp_seconds)
    if not zoom_expr:
        return None
    scale_filter, crop_filter = _build_facecam_zoom_filters(zoom_expr, focus_x, focus_y, width, height)
    return f"[0:v]{scale_filter}[vscaled];[vscaled]{crop_filter}[vout];"


def _plan_zoom_passes(
    speech_segments: list[dict], zoom_factor: float, ramp_seconds: float,
    focus_x: float, focus_y: float, width: int, height: int,
    max_chars: int = _MAX_FILTER_COMPLEX_CHARS,
) -> list[list[dict]]:
    """
    Reparte los tramos de speech_segments que SÍ producen zoom (duración
    >= ramp_seconds, ver _build_facecam_zoom_expr) en pasadas para
    _apply_zoom, tal que ninguna pasada supere max_chars (ver
    _partition_by_length). A la escala real (decenas de tramos) esto cabe
    siempre en una única pasada; la partición solo entraría en juego si
    algún día hubiera cientos de tramos de habla larga en un mismo vídeo.
    """
    if zoom_factor <= 1.0 or ramp_seconds <= 0:
        return []
    usable = [s for s in speech_segments if (s["end"] - s["start"]) >= ramp_seconds]
    if not usable:
        return []

    def build(segs: list[dict]) -> str:
        return (
            _build_zoom_pass_filter_complex(segs, zoom_factor, ramp_seconds, focus_x, focus_y, width, height)
            or ""
        )

    return _partition_by_length(usable, build, max_chars)


def _apply_zoom(cut_path: Path, speech_segments: list[dict], config: dict, width: int, height: int) -> Path:
    """
    Aplica el zoom hacia la webcam sobre `cut_path` (el vídeo ya cortado,
    sin zoom) en uno o más filter_complex APARTE del corte -- mucho más
    corto que el combinado anterior porque solo cubre los tramos de habla
    larga (p.ej. 47), no los cientos de cortes. Si hicieran falta más
    pasadas de las que caben en un único filter_complex (ver
    _plan_zoom_passes), se encadenan: cada pasada parte de la salida de la
    anterior y solo aplica zoom en su subconjunto de tramos (fuera de ellos
    el vídeo pasa sin cambios, ver _build_facecam_zoom_filters). Toma
    posesión de `cut_path`: lo consume (lo renombra o lo borra) en
    cualquier caso, el llamador no necesita limpiarlo aparte.

    Returns:
        Ruta a data/output/<video_id>/_cuts_zoom.mp4 (o `cut_path`
        renombrado sin cambios si no hay ningún tramo de zoom que aplicar).
    """
    edit_config = config.get("edit", {})
    zoom_factor = float(edit_config.get("long_speech_zoom_factor", 1.0))
    ramp_seconds = float(edit_config.get("zoom_in_duration_seconds", 4.5))
    # facecam_region es una fracción 0.0-1.0 del frame (ver
    # src.common.face_detection, cambiado de píxeles absolutos el
    # 2026-08-10 para no depender de la resolución del vídeo); se convierte
    # aquí a píxeles de ESTE clip concreto (width x height, el del vídeo ya
    # cortado) antes de calcular el punto de enfoque del zoom.
    facecam_px = facecam_region_to_pixels(config.get("facecam_region") or {}, width, height)
    focus_x = facecam_px["x"] + facecam_px["w"] / 2
    focus_y = facecam_px["y"] + facecam_px["h"] / 2

    result_path = cut_path.with_name("_cuts_zoom.mp4")
    passes = _plan_zoom_passes(speech_segments, zoom_factor, ramp_seconds, focus_x, focus_y, width, height)
    if not passes:
        # Sin zoom que aplicar: remux barato (sin recodificar) en vez de un
        # simple rename, para que este archivo tenga +faststart igual que
        # si hubiera pasado por una pasada de zoom (ver más abajo) --
        # normalize_audio/append_outro solo lo aplican si de verdad
        # recodifican, y si loudnorm/outro estuvieran desactivados este
        # sería directamente el final.mp4 entregado.
        _run_ffmpeg(
            ["ffmpeg", "-y", "-i", str(cut_path), "-c", "copy", "-movflags", "+faststart", str(result_path)],
            description="Sin tramos de zoom que aplicar; remuxeando el vídeo ya cortado",
        )
        cut_path.unlink(missing_ok=True)
        return result_path

    logger.info(
        "Aplicando zoom hacia la webcam en %d tramo(s) de habla larga en %d pasada(s) de ffmpeg "
        "(factor=%s, rampa=%ss)",
        sum(len(p) for p in passes), len(passes), zoom_factor, ramp_seconds,
    )

    current_input = cut_path
    for i, segs in enumerate(passes):
        is_last = i == len(passes) - 1
        out_path = result_path if is_last else cut_path.with_name(f"_zoom_pass_{i}.mp4")
        filter_complex = _build_zoom_pass_filter_complex(
            segs, zoom_factor, ramp_seconds, focus_x, focus_y, width, height
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(current_input),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "0:a",
            "-c:v", "libx264", "-crf", _FINAL_CRF, "-preset", _FINAL_PRESET, "-pix_fmt", "yuv420p",
            "-c:a", "copy",
        ]
        if is_last:
            # Solo la ÚLTIMA pasada puede acabar siendo el resultado final
            # de apply_cuts_with_zoom (las intermedias se recodifican otra
            # vez enseguida), así que solo ella necesita +faststart.
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(out_path))
        _run_ffmpeg(
            cmd,
            description=f"Zoom hacia la webcam (pasada {i + 1}/{len(passes)}, {len(segs)} tramo(s))",
        )
        Path(current_input).unlink(missing_ok=True)
        current_input = out_path

    return result_path


def apply_cuts_with_zoom(video_id: str, cuts: list[dict], config: dict) -> str:
    """
    Corta los tramos marcados en `cuts` de data/raw/<video_id>.mp4
    (conservando el resto) y aplica el zoom típico de streamer durante los
    tramos de habla continua de config['edit']['long_speech_min_seconds']
    segundos o más (ver detect_long_speech_segments): sube lento hacia
    config['facecam_region'] durante los primeros
    config['edit']['zoom_in_duration_seconds'] del tramo, y corta seco a
    1.0 exactamente al completarse esa rampa — no al terminar el tramo de
    habla (ver _build_facecam_zoom_expr).

    En DOS pasos independientes (ver docstring del módulo para el
    porqué): _cut_video recorta primero (uno o más shards de ffmpeg, según
    haga falta), y _apply_zoom aplica el zoom después sobre el resultado,
    en su propio filter_complex -- mucho más pequeño porque solo cubre los
    tramos de habla larga, no los cortes.

    Returns:
        Ruta al vídeo con los cortes y el zoom ya aplicados
        (data/output/<video_id>/_cuts_zoom.mp4).
    """
    input_path = _raw_video_path(video_id, config)
    info = _video_info(input_path)
    duration, width, height = info["duration"], info["width"], info["height"]

    min_kept_segment_seconds = float(config.get("edit", {}).get("min_kept_segment_seconds", 0.6))
    merged_cuts = merge_short_kept_segments(cuts, min_kept_segment_seconds)
    if len(merged_cuts) < len(cuts):
        logger.info(
            "Fusionados %d corte(s) cuyo tramo conservado entre sí era más corto que "
            "min_kept_segment_seconds=%.2fs (%d -> %d corte(s)); ver 'Fusión de cortes con hueco "
            "mínimo insuficiente' en el docstring del módulo.",
            len(cuts) - len(merged_cuts), min_kept_segment_seconds, len(cuts), len(merged_cuts),
        )
    cuts = merged_cuts

    keep_segments = compute_keep_segments(cuts, duration)
    if not keep_segments:
        raise ValueError(
            f"Los cortes de '{video_id}' eliminan el vídeo entero (duración {duration:.2f}s); "
            "no queda nada que conservar."
        )

    logger.info(
        "%d tramo(s) a conservar de %d corte(s) (duración original %.2fs)",
        len(keep_segments), len(cuts), duration,
    )

    transcript_path = _transcript_path(video_id, config)
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)
    speech_segments = detect_long_speech_segments(transcript, cuts, config)
    if speech_segments:
        logger.info(
            "%d tramo(s) de habla continua >= %.1fs detectado(s) (línea de tiempo editada): %s",
            len(speech_segments),
            float(config.get("edit", {}).get("long_speech_min_seconds", 10.0)),
            ", ".join(f"{s['start']:.2f}s-{s['end']:.2f}s" for s in speech_segments),
        )
    else:
        logger.info("No se ha detectado ningún tramo de habla continua; no se aplicará zoom.")

    out_dir = _output_dir(video_id, config)
    audio_crossfade_ms = float(config.get("edit", {}).get("audio_crossfade_ms", 20.0))
    cut_path = _cut_video(input_path, keep_segments, info["fps"], out_dir, audio_crossfade_ms)
    result_path = _apply_zoom(cut_path, speech_segments, config, width, height)

    return str(result_path)


def _measure_loudness(path: str) -> dict | None:
    """Primera pasada de loudnorm (solo análisis): devuelve las medidas en JSON, o None si no se pudieron parsear."""
    cmd = [
        "ffmpeg", "-i", path, "-vn",
        "-af",
        f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}:print_format=json",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # El bloque JSON de loudnorm no es necesariamente lo último en stderr
    # (ffmpeg suele imprimir un resumen de muxing/tamaño después); se busca
    # el ÚLTIMO bloque {...} en todo el stderr en vez de anclarlo al final.
    matches = re.findall(r"\{[^{}]*\}", result.stderr)
    if not matches:
        logger.warning("No se pudo leer la medición de sonoridad de ffmpeg loudnorm; se omite la normalización.")
        return None
    match = matches[-1]
    try:
        return json.loads(match)
    except json.JSONDecodeError:
        logger.warning("La medición de sonoridad de ffmpeg loudnorm no es JSON válido; se omite la normalización.")
        return None


def normalize_audio(clip_path: str, config: dict) -> str:
    """
    Normaliza el audio de clip_path con ffmpeg loudnorm (dos pasadas: mide
    y luego aplica), si config['edit']['loudnorm'] es true. El vídeo no se
    re-codifica (-c:v copy); solo se transcodifica el audio.

    Returns:
        Ruta al clip con audio normalizado (mismo directorio que
        clip_path), o clip_path sin cambios si loudnorm está desactivado.
    """
    edit_config = config.get("edit", {})
    if not edit_config.get("loudnorm", True):
        logger.info("loudnorm desactivado en config; se omite la normalización de audio.")
        return clip_path

    measured = _measure_loudness(clip_path)
    output_path = str(Path(clip_path).with_name("_normalized.mp4"))

    if measured is None:
        loudnorm_filter = f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}"
    else:
        loudnorm_filter = (
            f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}:"
            f"measured_I={measured.get('input_i')}:measured_TP={measured.get('input_tp')}:"
            f"measured_LRA={measured.get('input_lra')}:measured_thresh={measured.get('input_thresh')}:"
            f"offset={measured.get('target_offset')}:linear=true:print_format=summary"
        )

    cmd = [
        "ffmpeg", "-y", "-i", clip_path,
        "-c:v", "copy",
        "-af", loudnorm_filter,
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd, description="Normalizando audio (loudnorm, dos pasadas)")

    return output_path


def _same_video_params(a: dict, b: dict) -> bool:
    return a["width"] == b["width"] and a["height"] == b["height"] and abs(a["fps"] - b["fps"]) <= 0.01


def _same_audio_params(a: dict, b: dict) -> bool:
    return a["sample_rate"] == b["sample_rate"] and a["channels"] == b["channels"]


def _write_concat_list(list_path: Path, paths: list[Path]) -> None:
    lines = [f"file '{Path(p).resolve().as_posix()}'" for p in paths]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _glue_with_faststart(paths: list[Path], out_path: Path, description: str) -> None:
    """
    Como _glue_video_files (concat DEMUXER, -c copy, nunca el filtro
    `concat`) pero además con -movflags +faststart en la salida (moov al
    principio del archivo) -- a diferencia de _glue_video_files (usado
    para pegar shards/tramos INTERMEDIOS del corte, que se van a seguir
    procesando), aquí la salida puede acabar siendo directamente el
    final.mp4 entregado al usuario, así que interesa moov al principio
    desde ya.
    """
    list_path = out_path.with_suffix(".txt")
    _write_concat_list(list_path, paths)
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", "-movflags", "+faststart",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=description)
    list_path.unlink(missing_ok=True)


def _ordered_for_position(main_path: Path, extra_path: Path, position: str) -> list[Path]:
    if position == "before":
        return [extra_path, main_path]
    if position == "after":
        return [main_path, extra_path]
    raise ValueError(f"position debe ser 'before' o 'after', no {position!r}")


def _match_audio_only(extra_path: Path, main_info: dict, out_path: Path, description: str) -> None:
    """Recodifica SOLO el audio de extra_path para que encaje con main_info -- vídeo copiado (-c:v copy), sin recodificar."""
    channels = main_info["channels"] or 2
    channel_layout = "stereo" if channels >= 2 else "mono"
    cmd = [
        "ffmpeg", "-y", "-i", str(extra_path),
        "-c:v", "copy",
        "-af", f"aformat=sample_rates={main_info['sample_rate']}:channel_layouts={channel_layout}",
        "-c:a", "aac", "-ar", str(main_info["sample_rate"]), "-ac", str(channels), "-b:a", "192k",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=description)


def _match_full(extra_path: Path, main_info: dict, out_path: Path, description: str) -> None:
    """
    Recodifica vídeo+audio de extra_path para que encaje con main_info
    (resolución/fps/sample_rate/canales) -- SIEMPRE se escala el clip
    EXTRA (intro u outro) a los parámetros del clip PRINCIPAL, nunca al
    revés (ver "Unión de clips extra" en el docstring del módulo).
    """
    width, height, fps = main_info["width"], main_info["height"], main_info["fps"]
    sample_rate = main_info["sample_rate"] or 48000
    channels = main_info["channels"] or 2
    channel_layout = "stereo" if channels >= 2 else "mono"
    cmd = [
        "ffmpeg", "-y", "-i", str(extra_path),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}",
        "-af", f"aformat=sample_rates={sample_rate}:channel_layouts={channel_layout}",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", str(sample_rate), "-ac", str(channels), "-b:a", "192k",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=description)


def _glue_via_full_recode(
    main_path: Path, main_info: dict, extra_path: Path, position: str, out_path: Path, extra_label: str,
) -> tuple[Path, float]:
    matched_path = out_path.with_name(f"_{extra_label}_full_matched.mp4")
    _match_full(
        extra_path, main_info, matched_path,
        description=f"Normalizando el {extra_label} a los parámetros del clip principal",
    )
    duration = _video_info(matched_path)["duration"]
    _glue_with_faststart(
        _ordered_for_position(main_path, matched_path, position), out_path,
        description=f"Uniendo el clip principal con el {extra_label} ya normalizado",
    )
    matched_path.unlink(missing_ok=True)
    return out_path, duration


def _glue_extra_clip(
    main_path: Path, extra_path: Path, position: str, out_path: Path, extra_label: str,
) -> tuple[Path, float]:
    """
    Une extra_path (intro o outro) con main_path (el clip principal, que
    NUNCA se recodifica sea cual sea `position`) -- ver "Unión de clips
    extra (intro/outro), tres niveles" en el docstring del módulo para el
    detalle completo de los tres niveles y su motivación.

    Args:
        position: "before" (antepone extra_path, p.ej. intro) o "after"
            (lo pospone, p.ej. outro).
        extra_label: nombre corto para logs/nombres de archivo intermedio
            ("intro"/"outro").

    Returns:
        (out_path, duración en segundos de extra_path tal y como queda
        incorporada -- la del propio archivo si se usó la vía rápida, o la
        del archivo ya recodificado si no).
    """
    main_info = _video_info(main_path)
    extra_info = _video_info(extra_path)
    video_matches = _same_video_params(main_info, extra_info)
    audio_matches = _same_audio_params(main_info, extra_info)

    if video_matches and audio_matches:
        logger.info(
            "Añadiendo %s (concatenación rápida sin recodificar; mismos parámetros de stream)...",
            extra_label,
        )
        list_path = out_path.with_name(f"_{extra_label}_concat_list.txt")
        _write_concat_list(list_path, _ordered_for_position(main_path, extra_path, position))
        fast_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart", str(out_path),
        ]
        result = subprocess.run(fast_cmd, capture_output=True, text=True)
        list_path.unlink(missing_ok=True)
        if result.returncode == 0:
            return out_path, extra_info["duration"]
        logger.warning(
            "La concatenación rápida del %s falló pese a tener los mismos parámetros de stream; "
            "recodificando el %s por completo para unirlo:\n%s",
            extra_label, extra_label, result.stderr[-1000:],
        )
        return _glue_via_full_recode(main_path, main_info, extra_path, position, out_path, extra_label)

    if video_matches:  # solo difiere el audio -- caso nuevo respecto al append_outro original, ver _smart_concat
        logger.info(
            "El %s solo difiere en audio del clip principal (principal=%sHz/%sch, %s=%sHz/%sch); "
            "ajustando SOLO el audio (vídeo copiado sin recodificar) antes de unir.",
            extra_label, main_info["sample_rate"], main_info["channels"],
            extra_label, extra_info["sample_rate"], extra_info["channels"],
        )
        matched_path = out_path.with_name(f"_{extra_label}_audio_matched.mp4")
        _match_audio_only(
            extra_path, main_info, matched_path,
            description=f"Ajustando audio del {extra_label} para unirlo sin recodificar el vídeo",
        )
        duration = _video_info(matched_path)["duration"]
        _glue_with_faststart(
            _ordered_for_position(main_path, matched_path, position), out_path,
            description=f"Uniendo el clip principal con el {extra_label} (audio ya ajustado)",
        )
        matched_path.unlink(missing_ok=True)
        return out_path, duration

    logger.info(
        "El %s no tiene la misma resolución/fps que el clip principal "
        "(principal=%dx%d@%.2ffps, %s=%dx%d@%.2ffps); normalizando SOLO el %s (nunca el clip "
        "principal, que no debe perder calidad) para poder unirlo sin recodificarlo a él.",
        extra_label, main_info["width"], main_info["height"], main_info["fps"],
        extra_label, extra_info["width"], extra_info["height"], extra_info["fps"], extra_label,
    )
    return _glue_via_full_recode(main_path, main_info, extra_path, position, out_path, extra_label)


def append_outro(clip_path: str, config: dict) -> str:
    """
    Concatena assets/outro/outro.mp4 al final de clip_path, si
    config['edit']['append_outro'] es true y el archivo existe -- ver
    _glue_extra_clip (posición "after") para el mecanismo de unión.

    Returns:
        Ruta al clip final con el outro añadido, o clip_path sin cambios
        si append_outro está desactivado o no existe el archivo de outro.
    """
    edit_config = config.get("edit", {})
    if not edit_config.get("append_outro", True):
        logger.info("append_outro desactivado en config; se omite el outro.")
        return clip_path

    outro_path = (REPO_ROOT / config["paths"]["outro"]).resolve()
    if not outro_path.exists() or outro_path.stat().st_size == 0:
        logger.warning(
            "append_outro está activado pero no existe (o está vacío) el archivo de outro en %s; "
            "se continúa sin añadir outro.",
            outro_path,
        )
        return clip_path

    output_path = Path(clip_path).with_name("_with_outro.mp4")
    result_path, _outro_duration = _glue_extra_clip(Path(clip_path), outro_path, "after", output_path, "outro")
    return str(result_path)


def _intro_path(video_id: str, config: dict) -> Path:
    return (REPO_ROOT / config["paths"]["output"]).resolve() / video_id / "intro.mp4"


def prepend_intro(clip_path: str, video_id: str, config: dict) -> str:
    """
    Antepone data/output/<video_id>/intro.mp4 al PRINCIPIO de clip_path, si
    config['edit']['prepend_intro'] es true (default) y el archivo existe
    -- ver _glue_extra_clip (posición "before") para el mecanismo de unión,
    y "Intro grabado aparte" en el docstring del módulo para el contexto.

    Retrocompatible: si el archivo no existe (el caso normal para
    cualquier vídeo procesado antes de este cambio, o cualquiera sin
    intro), devuelve clip_path sin tocarlo -- el pipeline se comporta
    exactamente igual que antes de que existiera este paso.

    Returns:
        Ruta al clip con el intro añadido, o clip_path sin cambios si
        prepend_intro está desactivado o no existe el archivo de intro.
    """
    edit_config = config.get("edit", {})
    if not edit_config.get("prepend_intro", True):
        logger.info("prepend_intro desactivado en config; se omite el intro.")
        return clip_path

    intro_path = _intro_path(video_id, config)
    if not intro_path.exists() or intro_path.stat().st_size == 0:
        logger.info(
            "No existe (o está vacío) %s; se continúa sin anteponer intro.",
            intro_path,
        )
        return clip_path

    output_path = Path(clip_path).with_name("_with_intro.mp4")
    result_path, intro_duration = _glue_extra_clip(Path(clip_path), intro_path, "before", output_path, "intro")
    logger.info("Intro añadido al principio del vídeo (%.2fs, %s).", intro_duration, intro_path.name)
    return str(result_path)


def run(video_id: str, config: dict) -> dict:
    """
    Orquesta apply_cuts_with_zoom -> normalize_audio -> prepend_intro ->
    append_outro y guarda el resultado en data/output/<video_id>/final.mp4.

    Returns:
        dict con {"video_id": str, "output_path": str}
    """
    cuts_path = _cuts_path(video_id, config)
    with open(cuts_path, "r", encoding="utf-8") as f:
        cuts = json.load(f)

    stage_paths: list[str] = []

    clip_path = apply_cuts_with_zoom(video_id, cuts, config)
    stage_paths.append(clip_path)

    normalized_path = normalize_audio(clip_path, config)
    if normalized_path != clip_path:
        stage_paths.append(normalized_path)

    with_intro_path = prepend_intro(normalized_path, video_id, config)
    if with_intro_path != normalized_path:
        stage_paths.append(with_intro_path)

    final_stage_path = append_outro(with_intro_path, config)
    if final_stage_path != with_intro_path:
        stage_paths.append(final_stage_path)

    out_dir = _output_dir(video_id, config)
    output_path = out_dir / "final.mp4"
    shutil.move(final_stage_path, output_path)
    if final_stage_path in stage_paths:
        stage_paths.remove(final_stage_path)

    # Limpia los intermediarios (todo menos final.mp4, que ya se movió).
    for stage_path in stage_paths:
        Path(stage_path).unlink(missing_ok=True)

    logger.info("Vídeo final guardado en %s", output_path)

    db.set_status(video_id, "edited")

    return {"video_id": video_id, "output_path": str(output_path)}


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Editar el vídeo final")
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
