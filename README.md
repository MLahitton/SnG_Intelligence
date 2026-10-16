# SnG Intelligence — Realtime Voice 2A server support

Capa conversacional read-only de Gerencia. Agent Core utiliza herramientas de
lectura y un proveedor LLM para responder preguntas. **SnG-Control-Backend es la única
fuente de verdad operacional.**

```text
Gerencia -> Intelligence API -> Agent Core -> ReadTools -> SnG Control
                                    |
                                    +-> AgentModel -> OpenAI
```

No hay acceso a DB, copia persistida de Project/ProjectTask, cálculo de Leak Time,
escrituras, RAG, embeddings ni conversaciones persistidas. Voice 1A agrega voz
request/response sobre el mismo agente como fallback. Realtime Voice 2A agrega creación
de sesiones GPT-Live y delegación protegida; el cliente WebRTC queda para otro bloque.

## Estructura

```text
app/
  main.py                    # Aplicación, lifespan, health, session, chat y docs
  agent/                     # Loop, contratos, catálogo/evidencia, prompt y adaptador
  agent/presentation.py      # Metadata construida desde evidencia del request
  agent/rendering.py         # Presentación visual/hablada del mismo contenido
  api/dependencies.py        # Credenciales y cliente con alcance de request
  api/voice.py               # Transcribe, voice query y speech
  api/live.py                # Creación, delegación y revocación local de Live
  live/                      # Contratos, configuración, prompts, provider y registry
  auth/management_guard.py   # Bearer y validación oficial de Gerencia
  clients/sng_control.py     # Ocho operaciones GET cerradas
  clients/transport.py       # Allowlist de método, origen y rutas; sin cookies
  clients/openai.py          # Lifecycle SDK común, TLS nativo y logging seguro
  contracts/                # DTO Pydantic del contrato HTTP, sin lógica de negocio
  core/                     # Configuración, errores seguros y correlación
  tools/read_tools.py        # Seis herramientas internas, incluida find_projects
  voice/                    # Contratos STT/TTS, adapters, configuración y upload acotado
  voice/style.py            # Estilo TTS centralizado por responseType
tests/                      # HTTP mockeado, sin backend real
```

Los contratos se contrastaron con los DTO y controladores del checkout local de
SnG-Control-Backend: AuthController, ManagementContracts, ProjectResponse,
ProjectTaskResponse, DocumentContracts y NotificationContracts. No se modificó ese
repositorio. Los modelos son representaciones transitorias de respuestas HTTP,
no entidades operacionales independientes.

La validación es estricta: campos obligatorios ausentes o tipos incorrectos fallan
con 502. Los campos desconocidos se descartan para no reenviar datos nuevos sin
revisión. Las categorías y estados son strings: nuevos valores del backend se
conservan sin inventar equivalencias. El orden, los null y los valores oficiales
se conservan; no se recalculan totales ni estados. La serialización de los DTO hacia
JSON usa `model_dump(mode="json", by_alias=True)` para mantener camelCase.

## Desarrollo local

Python 3.12 o superior. `uv.lock` fija las versiones verificadas. Con uv instalado:

```powershell
uv sync --locked --cache-dir .uv-cache
Copy-Item .env.example .env
# Editar .env con el origen real del backend y el timeout.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

`uv sync` crea `.venv` e instala las dependencias de desarrollo. Alternativa sin uv:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . --group dev
```

La alternativa requiere una versión de pip compatible con grupos de dependencias;
uv es la vía reproducible con lockfile. Para producción, `uv sync --locked --no-dev`.
FastAPI, Pydantic y httpx implementan el servicio; Uvicorn lo ejecuta y python-dotenv
permite el `--env-file` local. Pytest utiliza el plugin async de AnyIO, incluido por
la pila HTTP. Ruff y mypy verifican formato, lint y tipos.

| Variable | Requisito |
|---|---|
| `SNG_CONTROL_BASE_URL` | Obligatoria. Origen HTTP(S), sin credenciales, path, query ni fragmento. HTTPS fuera de loopback. |
| `SNG_CONTROL_TIMEOUT_SECONDS` | Número finito mayor que cero; por defecto 30. Timeout explícito por fase HTTP. |
| `OPENAI_API_KEY` | Credencial del proveedor, requerida para chat y voz. No se valida por red en startup. |
| `SNG_INTELLIGENCE_MODEL` | Modelo obligatorio para chat, sin valor predeterminado. Debe soportar function calling y salida JSON Schema estricta en Responses API. |
| `SNG_INTELLIGENCE_TRANSCRIPTION_MODEL` | Requerido para `/voice/transcribe` y `/voice`; sin valor predeterminado. |
| `SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE` | Idioma del audio para STT; predeterminado `es`. Código de dos letras ISO-639-1, por ejemplo `es` o `en`; vacío/formato inválido produce un error seguro 503 al solicitar STT. |
| `SNG_INTELLIGENCE_SPEECH_MODEL` | Requerido solo para `/voice/speech`; sin valor predeterminado. |
| `SNG_INTELLIGENCE_VOICE` | Voz integrada requerida solo para `/voice/speech`; sin valor predeterminado. No acepta voces personalizadas. |

`.env.example` contiene únicamente un ejemplo local, sin secretos. Uvicorn carga
`.env` solo al usar `--env-file`; el código consume variables de entorno y valida
durante startup para SnG Control. La importación del módulo no conecta al backend.
Una configuración inválida de SnG Control impide el arranque y produce un mensaje
que nombra las variables, sin imprimir sus valores. La configuración LLM/audio se
comprueba al solicitar cada etapa: su ausencia no impide `/health` ni `/session`.

TLS utiliza un `truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` explícito en el
transporte real de SnG Control, conservando `CERT_REQUIRED` y verificación de
hostname. La confianza proviene del sistema operativo: CryptoAPI en Windows,
OpenSSL en Linux y Security framework en macOS. No se modifica SSL globalmente.
`truststore` es una dependencia directa fijada por `uv.lock`; no se exportan
certificados ni se guardan huellas o bundles PEM locales. El certificado del
backend debe estar administrado y ser confiable para el sistema operativo.
No se desactiva TLS ni se heredan proxies del entorno. El modo HTTP solo
se permite para localhost, 127.0.0.1 o ::1 durante desarrollo local.

## Autenticación y endpoints

El frontend enviará `Authorization: Bearer <Google ID Token>`. Intelligence exige
un único header, scheme Bearer y token no vacío. No decodifica el JWT ni decide
permisos por email, subject o datos del body.

Para la sesión y cada invocación de tool, consulta `GET /api/auth/me` con el mismo
token y exige que `isManagement` sea exactamente el booleano `true`. SnG Control
valida la sesión y determina los permisos. No se cachea esa decisión.

| Endpoint público | Comportamiento |
|---|---|
| `GET /health` | `{"status":"ok"}`. Sin token y sin consultar SnG Control. Liveness del proceso. |
| `GET /api/intelligence/session` | Valida sesión y Gerencia; devuelve únicamente `{"authenticated":true,"isManagement":true}`. |
| `POST /api/intelligence/chat` | Consulta independiente: `responseType`, `answer`, `spokenAnswer`, `references`, `presentation`. |
| `POST /api/intelligence/voice/transcribe` | Audio multipart → `transcript`. |
| `POST /api/intelligence/voice` | Audio multipart → mismo contrato semántico de chat más `transcript`, usando Agent Core. |
| `POST /api/intelligence/voice/speech` | JSON `text` y `responseType` opcional → bytes MP3 inline. |

No existen endpoints de tools ni proxy. Voice 1A habilita `/docs` y `/openapi.json`
públicos para documentar los contratos, incluido Bearer, sin datos ni secretos de
sesión. Las operaciones de negocio y voz requieren Gerencia. CORS no está habilitado.
Todas las respuestas incluyen `Cache-Control: no-store` y `X-Request-ID`.
La integración posterior decidirá BFF o CORS explícito.

Smoke local sin credenciales:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

El smoke autenticado requiere que un cliente envíe un Google ID Token vigente por
header a `/api/intelligence/session`. No ponerlo en URL, archivos, capturas de logs
ni comandos que queden en el historial. La suite automatizada usa tokens ficticios.

## Cliente y tools

El cliente ofrece exclusivamente estas operaciones operacionales:

| Método | Ruta GET de SnG Control |
|---|---|
| `get_current_user()` | `/api/auth/me` |
| `get_management_overview()` | `/api/management/overview` |
| `list_management_tasks()` | `/api/management/tasks` |
| `list_projects()` | `/api/projects` |
| `get_project(project_id)` | `/api/projects/{id}` |
| `list_project_tasks(project_id)` | `/api/projects/{id}/tasks` |
| `list_project_documents(project_id)` | `/api/projects/{id}/documents` |
| `list_project_notifications(project_id)` | `/api/projects/{id}/notifications` |

Las seis tools internas de `ReadTools` son:

1. `get_management_overview()` — overview oficial.
2. `list_management_tasks()` — lista transversal, en el orden original.
3. `get_project_details(project_id)` — proyecto y tareas en paralelo; cancela y
   espera la llamada hermana si ocurre un error o cancelación.
4. `get_project_documents(project_id)` — metadata, initialHandoff y flags oficiales.
5. `get_project_activity(project_id)` — notificaciones oficiales, sin interpretación.
6. `find_projects(query)` — candidatos por nombre, SG, ciudad o cliente disponible.

La búsqueda usa la lista oficial de proyectos, incluidos los que todavía no tienen
tareas, y management tasks para obtener nombres de clientes cuando estén disponibles.
Normaliza mayúsculas/minúsculas y espacios; cada término debe aparecer en los campos
buscables. No hace fuzzy matching, embeddings ni selección automática del primer
resultado. `clientName` es null si no está disponible. Se ordenan las coincidencias por
nombre exacto, nombre parcial, cliente/ciudad y SG alternativo, conservando el orden del
backend dentro de cada grupo. Si hay nombres exactos, se devuelven esos candidatos en
lugar de las coincidencias parciales/auxiliares. Varios nombres exactos iguales siguen
siendo ambiguos. Nunca se selecciona arbitrariamente la primera coincidencia.
Varios candidatos se señalan como ambiguos; dentro de esa ejecución no se permite
consultar detalles de uno de esos candidatos arbitrariamente. El usuario puede precisar
su siguiente pregunta, que será una ejecución independiente.

Los flags documentales como `canModify` son información recibida; no habilitan
acciones en Intelligence. No se consumen `/file`, `current.zip` ni `initial-handoff.zip`.

Las dependencias FastAPI administran el cliente por request; chat lo conecta a
ReadTools mediante el dispatcher cerrado ToolBox. `get_read_tools` también permite
inyectar ReadTools mediante `Depends`, sin argumentos de identidad. Las tools
verifican Gerencia por sí mismas y validan UUID antes de cualquier I/O. El cliente
también valida UUID. No se aceptan URLs, paths o queries en lugar de identificadores.

La instancia de `SnGControlClient` es solo del request. `get_control_client` limpia
sus credenciales en `finally`. Un consumidor interno fuera de FastAPI debe respetar
ese mismo ciclo de vida y llamar `clear_credentials()` en `finally`. No registrar
el cliente, las tools o las credenciales como singleton ni suministrarlos a un LLM.

## Fronteras de seguridad

- Un único `httpx.AsyncClient` vive durante el lifespan y reutiliza conexiones; se
  cierra en shutdown. No tiene Authorization global y rechaza todas las cookies.
- El token vive en un `SecretStr` del cliente por request, sin persistencia ni
  inclusión en DTO, respuestas o logs. Se borra la referencia al finalizar y se
  elimina Authorization del request HTTP saliente tras completar o fallar la llamada.
  Esto reduce retención; Python no garantiza borrado físico de cadenas en memoria.
- `ReadOnlyTransport` impone GET, origen configurado, ocho rutas permitidas y
  ausencia de query/credenciales URL. Bloquea escrituras y descargas incluso si
  otro componente interno intenta construirlas. No hay API pública de HTTP arbitrario.
- Redirects deshabilitados; cualquier 3xx es un error seguro y no se reenvía el token.
- Excepciones HTTP y de validación se sustituyen por errores propios sin encadenar
  sus objetos sensibles ni devolver bodies, headers o mensajes del backend.
- Logging JSON limitado a request ID, operación fija, duración, status y código
  seguro de error. No registra paths crudos, queries, bodies o headers. HTTP debug
  no está habilitado; el comando de arranque desactiva access logs de Uvicorn.
- Se acepta un `X-Request-ID` UUID válido o se genera uno; se devuelve y propaga al
  backend. No requiere cambios en SnG Control. La correlación no concede permisos.

No habilitar dumps de request, captura de variables locales ni instrumentación de
Authorization al integrar observabilidad posteriormente. Las restricciones cubren
esta aplicación; cualquier cambio futuro en código del transporte requiere revisión.

| Fallo | Respuesta Intelligence |
|---|---|
| Bearer ausente/inválido o backend 401 | 401 y `WWW-Authenticate: Bearer` |
| Gerencia false o backend 403 | 403 |
| Backend 404 | 404 |
| Timeout HTTP | 504 |
| Error de conexión/TLS o backend 429 | 503 |
| Backend 5xx, 3xx, status inesperado, JSON/contrato inválido | 502 |
| ID de proyecto inválido | `InvalidArguments` (422 si se integra a HTTP) |

Los errores HTTP propios incluyen solo `error` y `requestId`. No hay retries
automáticos ni caché de datos operacionales. Los errores de validación no reflejan
el body recibido: no se devuelve texto que pudiera contener credenciales.

## Agent Core y contrato de chat

```http
POST /api/intelligence/chat
Authorization: Bearer <Google ID Token>
Content-Type: application/json

{"message":"¿Qué debería preocuparme hoy?"}
```

Solo se acepta `message`, un string no vacío de hasta 4000 caracteres. Se rechazan
campos adicionales como token, email, subject, role, isManagement, history o
conversationId. El guard de Gerencia se ejecuta antes de usar el proveedor, incluso
si este no está configurado. `/health` y `/session` funcionan sin configuración LLM.

La respuesta pública es:

```json
{
  "responseType": "direct",
  "answer": "Tenemos 5 proyectos.",
  "spokenAnswer": "Tenemos 5 proyectos.",
  "references": [],
  "presentation": {
    "kind": "direct",
    "metric": {"key": "total_projects", "label": "Proyectos", "value": 5}
  }
}
```

Ejemplo ilustrativo: la cifra se obtiene del overview real, nunca de una constante.
`/voice` agrega únicamente `transcript` a este mismo contrato. Las variantes exactas
de referencias, conservadas de 1B, son:

```text
ProjectReference = {type: "project", projectId: UUID, label: string, sgCode: string | null}
TaskReference    = {type: "task", taskId: UUID, projectId: UUID, label: string}
```

`TaskReference.label` usa el glosario humano de tareas: Preparar cotización, Revisar
cotización y Presentar cotización al cliente. Tipos desconocidos muestran «Tarea»,
sin inventar su significado ni exponer el enum. IDs y enums internos no cambian.

No contienen URLs. El modelo selecciona claves de un catálogo de evidencia construido
durante esa ejecución; el servidor obtiene IDs, labels y SG exclusivamente de resultados
reales. Una clave no recuperada produce `ungrounded_reference` (502). Las referencias
de documentos/actividad requieren evidencia previa del proyecto, normalmente obtenida
por find_projects o details. El catálogo no se comparte entre requests.

`AgentModel` es un protocolo pequeño e independiente del SDK. `OpenAIModel` implementa
Responses API del SDK oficial con function calling estricto y JSON Schema para la
respuesta final; el modelo es configurable, no hardcodeado. Se usa `store=False`, sin
historial remoto, streaming, tracing, retries ni herramientas alojadas (web/shell/etc.).
La instancia SDK vive durante un chat, reutiliza conexiones entre rondas y se cierra
al terminar. Su origen se fija a la API oficial; no se hereda `OPENAI_BASE_URL`.

El Agent Core controla cada ronda y ejecuta las tools. El adaptador traduce sus
mensajes a `function_call` y `function_call_output`, enlazados por `call_id`, y
configura la respuesta final mediante `text.format`. Los elementos de razonamiento
del proveedor se reenvían entre rondas solo en memoria durante ese request, incluido
su contenido cifrado cuando está presente. No se usa Conversations ni
`previous_response_id`; cada request de chat es independiente, sin persistencia
de conversaciones.

El modelo solo recibe la pregunta, los schemas y resultados proyectados de las seis
tools registradas. Nunca recibe ReadTools, el cliente backend, requests, headers,
variables de entorno o contexto de autenticación. Se omiten identidades Google,
emails de autores/asignados y flags de modificación de los resultados. Un guard
separado rechaza la aparición literal de las credenciales actuales en la pregunta,
argumentos, resultados o respuesta, antes de enviarlos al adaptador o al usuario.
La API key se utiliza exclusivamente para autenticar el SDK.

Los textos del backend se envían como datos de tool, nunca como instrucciones de
sistema. El prompt exige consultar datos operacionales, distinguir hechos de
recomendaciones, informar ausencia de información y pedir precisión ante ambigüedad.
Los controles de tools, argumentos y referencias se aplican en código. El texto
natural sigue siendo generado por el modelo: estas comprobaciones no son una prueba
formal de exactitud de cada afirmación ni eliminan todo riesgo de prompt injection.

La política de Gerencia está en `app/agent/prompt.py`: español natural, breve y
ejecutivo, nombre del proyecto primero y SG como identificador auxiliar. Se evita
repetir «según SnG Control» como coletilla; la fuente se menciona cuando ayuda a
explicar disponibilidad o cuando el usuario la pide. Hechos y recomendaciones se
distinguen explícitamente, sin inferir productividad desde cantidad de tareas.

`rendering.py` aplica el glosario centralizado y convierte timestamps con offset a
formato humano. En la prosa evita «Leak Time»: usa «plazo» o el estado humano,
por ejemplo «La tarea está en tiempo». La conversión de fechas usa
`America/Bogota`, mediante ZoneInfo y tzdata (también en Windows). No cambia DTO,
evidence, deadlines ni referencias. Protege los nombres recuperados de proyectos,
clientes y ciudades de reemplazos, incluidos nombres que contienen enums o SG.
Fechas sin zona no se asumen UTC; el prompt exige aclarar que falta zona y no
completar nulls. Preparar cotización pendiente no tiene Leak Time activo; Presupuestos
lo inicia con la primera asignación del SG. No se calcula atraso ni se infiere historia
de asignación; Pending sin deadline se puede describir como pendiente de inicio.

### Contrato de presentación y grounding

`responseType` es un enum cerrado. `presentation` es una unión Pydantic discriminada
por `kind`; sus objetos rechazan campos adicionales y el tipo debe coincidir con
`responseType`. No hay metadata libre ni URLs.

| responseType | Uso | Campos de presentation además de kind |
|---|---|---|
| `direct` | Pregunta simple/conteo | `metric: Metric \| null` |
| `management_brief` | Resumen, prioridades y riesgos | `metrics: Metric[]`, `items: (ProjectItem \| TaskItem)[]` |
| `project_status` | Estado de un proyecto resuelto | `project: ProjectItem`, `tasks: TaskItem[]` |
| `list` | Enumeración de elementos | `items: (ProjectItem \| TaskItem)[]` |
| `comparison` | Comparación de carga por área | `areas: AreaItem[]` |
| `clarification` | Ambigüedad/identificación insuficiente | `candidates: ProjectItem[]` |
| `unavailable` | Dato no disponible | `reason: "data_not_available"` |

```text
Metric      = {key: string, label: string, value: int}
ProjectItem = {kind: "project", projectId: UUID, projectName: string,
               sgCode: string | null, clientName: string | null, city: string | null}
TaskItem    = {kind: "task", projectId: UUID, projectName: string, sgCode: string | null,
               taskId: UUID, area: string | null, task: string | null, status: string | null,
               leakTimeStatus: string | null, deadlineAt: datetime | null}
AreaItem    = {area: string | null, pending: int, inProgress: int, completed: int,
               overdue: int, dueToday: int, dueSoon: int}
```

El dispatcher mantiene `available_presentation` junto al catálogo de referencias,
solo durante el request. El modelo selecciona `presentation_keys`, nunca envía IDs,
labels, cifras ni fechas como autoridad. El builder obtiene los objetos desde las
respuestas tipadas de tools: métricas y áreas del overview, proyectos de búsquedas/
detalles y tareas de overview/tasks/details. Una clave ausente o de tipo incompatible
falla con `ungrounded_reference` (502). No se recalculan totales ni estados. Las áreas
reconocidas y los enums usan labels humanos; un valor desconocido se representa con
label null, sin inventar su significado ni cambiar el valor original en evidence.

`project_status` requiere detalles recuperados y contiene todas sus tareas oficiales,
sin adivinar cuál es la actual. El nombre oficial abre la respuesta incluso si se
resolvió por SG. `clarification` incluye todos los candidatos de una búsqueda ambigua,
o una lista vacía si falta identificación. No permite presentar solo el primer candidato.
En chat y fallback no hay memoria: otra llamada con «El primero» no tiene contexto
garantizado; hay que
volver a indicar el nombre/datos que desambigüen.

### answer y spokenAnswer

Ambas vistas parten de un único contenido. El schema estricto del modelo requiere
`responseType`, `answer`, `spokenAnswer`, `reference_keys` y `presentation_keys`.
En este bloque, el modelo debe copiar exactamente `answer` en `spokenAnswer`; el Core
rechaza divergencias con `invalid_model_response`. Luego el renderer produce la voz
desde esa fuente común, quitando viñetas/énfasis simples, presentando fechas habladas
y verbalizando SG cuando aparece fuera de nombres propios protegidos:
`SG005` → «ese ge cero cero cinco». No introduce SG si el texto no lo contiene.

Es una decisión conservadora: no hay una segunda paráfrasis LLM ni comparación
semántica mediante regex. La voz puede diferir en formato y pronunciación, pero no
añadir entidades/cifras/conclusiones. La exactitud factual de la prosa original sigue
dependiendo del modelo y sus tools; estas comprobaciones no prueban cada afirmación
natural. La metadata y las referencias sí se construyen desde evidencia validada.
El léxico centralizado inicial solo incluye SG; no traduce nombres ni añade reemplazos
especulativos. La normalización ocurre al componer `spokenAnswer`, antes del TTS;
`/voice/speech` lee el texto recibido y no vuelve a modificar nombres sin evidencia.

Límites por consulta:

- 6 rondas de tools, 8 invocaciones totales y hasta 7 llamadas al modelo.
- 120 segundos de ejecución del agente; timeout de proveedor 30 segundos por llamada.
- 3000 output tokens por llamada; respuesta validada hasta 12000 caracteres.
- 48 000 bytes UTF-8 por resultado de tool y 128 000 por contenido acumulado de la
  conversación interna. Sin truncamiento silencioso ni paginación simulada: si se
  supera un límite, la solicitud falla de forma segura. La proyección elimina campos
  de identidad, pero no elimina filas ni recalcula estados/totales.

`complete` indica ausencia de truncamiento por Intelligence, no exhaustividad de
una colección del backend. Por ejemplo, un total de atención puede superar los
items incluidos en el overview; el agente debe respetar ambos valores oficiales.

| Error de Agent Core | HTTP |
|---|---|
| `agent_not_configured` | 503 |
| `provider_configuration_error`, `provider_rate_limited`, `provider_unavailable` | 503 |
| `provider_timeout` | 504 |
| `invalid_model_response`, `invalid_tool_call`, `ungrounded_reference` | 502 |
| `agent_limit_exceeded`, `tool_result_too_large` | 503 |
| `sensitive_content_rejected` | 422 |

Los errores de tools conservan la política backend de 1A y abortan la generación,
sin convertir fallos en hechos. Un fallo del proveedor nunca devuelve 401 ni
`WWW-Authenticate`, para evitar confundirlo con la sesión Google. Logs JSON incluyen
request ID, duración, nombres/cantidad de tools, status y categoría segura. No
registran preguntas, respuestas, resultados completos o credenciales. Logs del SDK
no se propagan. `store=False` no sustituye las políticas de retención del proveedor.

## Voice 1A

```text
Audio -> STT -> transcript -> Agent.run -> respuesta semántica + evidencia
Texto ---------------------> Agent.run -> respuesta semántica + evidencia
                                         |                  |
                                  answer/presentation   spokenAnswer
                                                            |
                                              POST /voice/speech -> MP3
```

`SpeechToText` y `TextToSpeech` son protocolos pequeños. `OpenAITranscription` usa
`audio.transcriptions.create`; `OpenAISpeech` usa `audio.speech` del SDK oficial.
STT envía `language` desde configuración, con español (`es`) como valor predeterminado;
la autodetección no es el comportamiento principal. Se usa transcripción, nunca traducción,
y el texto devuelto se conserva sin reescribirlo antes de pasarlo al Agent Core.
Para español y los modelos `gpt-4o-mini-transcribe`, su versión `2025-12-15` y
`gpt-4o-transcribe`, se añade un hint léxico centralizado sobre Steel & Glass,
proyectos, Presupuestos, Comercial, Gerencia, cotización y SG. No autoriza responder
preguntas ni cambiar el significado. Otros modelos o idiomas omiten ese hint.
El idioma es configuración del servidor; los contratos HTTP y el frontend no cambian.
No forman parte de `OpenAIModel`. La factoría de cliente SDK comparte TLS nativo,
origen oficial fijo, timeout finito, ausencia de redirects/proxies y cero retries.
`/chat` y `/voice` obtienen el mismo `Agent` mediante `get_agent` y llaman a `run`;
no se duplicaron prompt, tools, loop, límites ni catálogo de evidencia.

Contratos autenticados con el mismo Google Bearer y guard de Gerencia:

```text
POST /api/intelligence/voice/transcribe
Content-Type: multipart/form-data
file=<audio>
200 application/json: {"transcript":"¿Cómo va Smoke Test Proyecto?"}

POST /api/intelligence/voice
Content-Type: multipart/form-data
file=<audio>
200 application/json: {"transcript":"...","responseType":"...","answer":"...",
                       "spokenAnswer":"...","references":[...],"presentation":{...}}

POST /api/intelligence/voice/speech
Content-Type: application/json
{"text":"Smoke Test Proyecto...","responseType":"project_status"}
200 audio/mpeg: <bytes MP3>
Cache-Control: no-store
```

Los formularios admiten exactamente un archivo `file`, sin campos de identidad,
modelo, voz, URLs ni archivos adicionales. Speech acepta `text` y `responseType`
opcional (por defecto `direct`); rechaza estilos, instrucciones o voces del caller.
Las referencias mantienen el contrato de chat. `/voice` no ejecuta TTS; el futuro
frontend podrá enviar su `spokenAnswer` y `responseType` a `/voice/speech`. No hay base64, URLs de audio,
descargas públicas, multipart/mixed ni `Content-Disposition: attachment`.

Formatos de entrada: `.webm`, `.wav`, `.mp3`, `.m4a` y `.ogg`. Se comprueban extensión,
MIME declarado cuando existe y firma básica del contenedor. No se confía solo en
MIME ni se decodifican codecs localmente; el proveedor decide la validez final del
audio. MIME canónicos: `audio/webm`, `audio/wav`, `audio/mpeg`, `audio/mp4`, `audio/ogg`.
Se admiten también sus alias definidos en `voice/upload.py`, por ejemplo
`audio/x-wav`, `video/webm` y `audio/x-m4a`. Un MIME incompatible se rechaza.
En el smoke y en uploads manuales, enviar los archivos `.m4a` con MIME `audio/mp4`
en la parte multipart `file`; el Content-Type de la petición sigue siendo
`multipart/form-data` con su boundary.

Límites centralizados en `voice/config.py`:

- Audio de entrada: **10 MiB** (10 485 760 bytes); multipart completo: 10 MiB + 64 KiB.
  Se verifica `Content-Length` si existe y se cuentan bytes reales incluso con
  transferencia por chunks o tamaño declarado falso. La lectura ocurre tras auth.
- Upload: **30 segundos**. STT: **60 segundos**. Agent: **120 segundos** existentes.
  TTS: **60 segundos**. STT y Agent se ejecutan secuencialmente y son cancelables.
- Transcript: **4000 caracteres**, igual que el mensaje de chat. Vacío o solo espacios
  falla antes del agente. No se traduce, corrige ni completa el texto reconocido.
- Speech: **4000 caracteres**, no vacíos. Si un `spokenAnswer` supera ese límite, el caller
  deberá solicitar una respuesta más breve: no hay truncamiento ni división silenciosa.
- MP3 de salida: **10 MiB**. La lectura HTTP al proveedor es incremental y acotada,
  pero se completa y valida antes de enviar la respuesta al usuario. No hay streaming
  de audio al cliente, SSE, sesiones realtime ni estimación de duración desde bytes
  en estos endpoints de fallback.

SnG Intelligence no persiste audio de forma durable, no lo almacena como dato de
producto y no conserva archivos después del request. Tampoco guarda transcripts
ni speech generado en DB, Redis, historial o caché de aplicación. El procesamiento
multipart usa el mecanismo de spooling de Starlette, que puede utilizar archivos
temporales internos según sus límites y configuración; no se garantiza que el audio
«nunca toca disco». Los recursos del upload se cierran al finalizar el request,
incluidos fallo, timeout, desconexión y cancelación. El filename se sustituye por
`audio.<extensión>` y el MIME se normaliza antes de enviar STT.
No se envían headers de usuario ni contexto de autenticación a los modelos.
El guard rechaza credenciales actuales presentes literalmente en texto o bytes;
no es un detector semántico de secretos pronunciados dentro de una grabación.

STT usa transcripción, sin translations, y envía el idioma configurado mediante
`SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE` (`es` por defecto). Para otro idioma,
se cambia esa variable en el servidor. TTS recibe únicamente `text` y una
instrucción base centralizada en `voice/config.py` y variantes en `voice/style.py`:
español colombiano natural, cálido y cercano, profesional y conversacional, con
pausas naturales, pequeñas variaciones de ritmo, entonación orgánica, énfasis sutil
y cadencia relajada. La interpretación es solo vocal; no cambia las palabras del
spokenAnswer ni interpreta hechos. Evita tono de asistente virtual, GPS, locutora
o narración institucional, sin dramatizar. Puede conservar un matiz paisa suave,
sin forzar acento ni introducir modismos.
`responseType` solo elige variaciones de estilo: direct breve/segura; management_brief
ejecutiva/pausada; project_status informativa; clarification conversacional/interrogativa;
unavailable neutra y clara; list/comparison con separación oral. No cambia hechos.
Para `tts-1`/`tts-1-hd` se omiten instrucciones, que esos modelos no admiten.
Solo se permiten voces integradas; el modelo y la voz elegida vienen del entorno.

Ejemplos de configuración de desarrollo, sujetos al acceso del proyecto OpenAI:

```dotenv
SNG_INTELLIGENCE_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE=es
SNG_INTELLIGENCE_SPEECH_MODEL=gpt-4o-mini-tts
SNG_INTELLIGENCE_VOICE=marin
```

**Marin es la selección provisional actual**, configurable mediante
`SNG_INTELLIGENCE_VOICE`. `.env.example` la propone; la aplicación no fija una voz
por defecto ni impide elegir otra voz integrada compatible. El modelo TTS continúa
definido por `SNG_INTELLIGENCE_SPEECH_MODEL`, sin un modelo hardcodeado.

La futura interfaz deberá indicar al usuario que la voz es generada por IA.
Los formatos, límites e instrucciones siguen las referencias oficiales de
[transcripción](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
y [síntesis](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create).
La [guía TTS](https://developers.openai.com/api/docs/guides/text-to-speech) describe
la comunicación de voz generada por IA. La ausencia de persistencia durable local no sustituye
las políticas de retención del proveedor; Audio API no recibe un parámetro `store`.

| Error Voice | HTTP |
|---|---|
| `voice_not_configured` | 503 |
| `audio_invalid`, `empty_transcription`, `transcript_too_long` | 422 |
| `audio_unsupported` | 415 |
| `audio_too_large` | 413 |
| `audio_upload_timeout` | 408 |
| `invalid_request` (incluido texto de speech inválido) | 422 |
| `invalid_audio_response` (incluido MP3 vacío, incorrecto o excesivo) | 502 |
| `provider_configuration_error`, `provider_rate_limited`, `provider_unavailable` | 503 |
| `provider_timeout` | 504 |

Errores de auth/backend/Agent conservan sus códigos existentes. Cada etapa falla
explícitamente, sin respuestas inventadas, audio vacío ni fallback. Un error TTS
se devuelve como JSON seguro antes de iniciar el MP3. Los logs solo añaden tamaño,
etapa y duración de STT/Agent/TTS a la metadata existente; no contienen audio,
filename, transcript, answer, bodies multipart ni credenciales.

### Smoke manual Voice

La suite automática usa `FakeSpeechToText`, `FakeTextToSpeech` y HTTP simulado del
SDK, sin llamadas reales. Para el smoke manual:

1. Cargar temporalmente las cinco variables OpenAI/modelos/voz de arriba y
   `SNG_SMOKE_GOOGLE_TOKEN` en el proceso, sin guardar credenciales en `.env`,
   comandos con valores literales ni archivos. Mantener SnG Control en
   `https://localhost:7265` y TLS validado. Si falta configuración, no ejecutar smoke.
2. Iniciar Intelligence con el comando de desarrollo y abrir
   `http://127.0.0.1:8000/docs`. Autorizar con un token temporal de Gerencia y comprobar
   `/health` y `/api/intelligence/session`. Swagger no persiste autorización entre recargas.
3. Usar un audio local fuera del repositorio con «¿Cuántos proyectos tenemos?» en
   `/voice/transcribe`. Revisar el transcript en la respuesta, sin copiarlo a logs.
   Para `.m4a`, asignar explícitamente `audio/mp4` al archivo multipart; aplicar
   el mismo MIME al enviarlo a `/voice`.
4. Obtener un nombre real de datos oficiales. Grabar «¿Cómo va <nombre real>?» fuera
   del repo y enviarlo a `/voice`. Revisar transcript, responseType, answer, spokenAnswer, tools
   invocadas y referencias contra la evidencia real. No asumir que SG001 existe.
5. Enviar `spokenAnswer` como `text` (hasta 4000 caracteres) y `responseType` a
   `/voice/speech`. Reproducir el MP3;
   si se descarga para escuchar, guardar solo un temporal de smoke fuera del repo
   y eliminarlo después. La aplicación no crea ese archivo.
6. Evaluar español entendible, nombres/SG, velocidad natural, tono profesional y
   ausencia de contenido añadido. La suite offline no evalúa fidelidad acústica.
7. Registrar solo duraciones diagnósticas `transcription_duration_ms`,
   `agent_duration_ms` y `speech_duration_ms` de los logs seguros; no constituyen SLA.
8. Cerrar la sesión de Swagger, borrar el temporal de smoke y retirar las variables
   sensibles del proceso. No guardar audio ni credenciales en Git.

## Smoke real del proveedor textual

La suite usa FakeModel y HTTP mockeado; no necesita API key ni consume OpenAI real.
Para el smoke, cargar credenciales solo temporalmente en la consola que iniciará el
proceso. No escribir valores reales en comandos, `.env`, archivos o chat:

```powershell
$secureKey = Read-Host 'OpenAI API key' -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    $secureKey.Dispose()
    Remove-Variable secureKey, keyPointer
}
$env:SNG_INTELLIGENCE_MODEL = Read-Host 'Modelo habilitado para este proyecto OpenAI'
```

El proceso de smoke debe heredar estas variables y `SNG_SMOKE_GOOGLE_TOKEN` desde
esa misma consola. Definirlas en otra terminal no modifica procesos ya abiertos.
Con backend y credenciales disponibles, probar «¿Cuántos proyectos tenemos?»,
«¿Qué debería preocuparme hoy?» y una pregunta por un nombre obtenido de datos
reales. Revisar respuestas, tools y referencias sin registrar credenciales.
Al terminar: `Remove-Item Env:OPENAI_API_KEY, Env:SNG_INTELLIGENCE_MODEL,
Env:SNG_SMOKE_GOOGLE_TOKEN -ErrorAction SilentlyContinue`.

### Smoke Presentation & Voice 1B y comparación manual de voces

Preparado para ejecutar con backend `https://localhost:7265`, TLS verificado y las
variables temporales del procedimiento anterior. Los ejemplos de proyectos deben
existir en los resultados reales; no asumir su existencia ni hardcodear UUIDs.

| Pregunta | Resultado esperado |
|---|---|
| ¿Cuántos proyectos tenemos? | `direct`, total oficial, respuesta corta y spokenAnswer natural |
| ¿Qué debería preocuparme hoy? | `management_brief`, hechos separados de recomendaciones |
| ¿Cómo va Smoke Test Proyecto? | `project_status`, nombre primero, SG secundario, tareas oficiales |
| ¿Cómo va Casa? | `clarification` si hay varios candidatos reales; todos visibles |
| ¿Cuánto facturamos este mes? | `unavailable`, sin inventar cifra ni asumir cero |

Repetir con el audio de Voice 1A «¿Cuántos proyectos tenemos?» y con audio del nombre
real del proyecto. Validar transcript, tipo, ambas vistas, presentation y references
contra las tools. Enviar spokenAnswer a speech y escuchar; `.m4a` usa `audio/mp4`.
No imprimir credenciales ni guardar audio/transcripts/respuestas como datos de producto.

Para A/B, empezar con `SNG_INTELLIGENCE_VOICE=marin`, reiniciar el proceso y sintetizar
el mismo spokenAnswer/responseType. Cambiar temporalmente a `coral`, `nova` o `shimmer`
(si el modelo configurado las admite), reiniciar y repetir la misma frase. Comparar por
escucha claridad, calidez, naturalidad, nombres y pausas; no hay benchmark automático
de género. La preferencia de una voz femenina se decide por escucha, no se deduce del
nombre de la voz. Eliminar temporales de reproducción fuera del repo al terminar.

El usuario confirmó aprobado el smoke real hasta Presentation & Voice 1B: backend,
OpenAI Responses API, grounding, presentación, STT, consulta por voz, TTS, TLS y
`store=false`. El cierre ajusta lenguaje, labels y estilo TTS con pruebas offline;
no repite la batería pagada ni presenta esas pruebas como una nueva escucha acústica.

## Verificación

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\ruff.exe format --check app tests
.\.venv\Scripts\mypy.exe app
git diff --check
```

La suite no usa SnG Control real. Cubre configuración/startup, auth estricta,
forwarding y rutas exactas, DTO y null, estados nuevos, orden, UUID, metadata,
errores, concurrencia, cleanup, correlación, aislamiento de tokens/cookies,
ausencia de proxy y rechazo efectivo de escrituras, descargas y otros orígenes.
Voice cubre autenticación antes del upload, formatos, tamaños reales, cierre sin
rollover, cancelación, contratos SDK, errores seguros, referencias compartidas,
límites de transcripción/síntesis y ausencia de credenciales/contenidos en logs.
Mypy corre en modo strict sobre toda la aplicación.

La versión bloqueada de Starlette emite una advertencia de deprecación de su
TestClient con httpx. Las pruebas funcionan; no se añadió otro cliente HTTP para
ocultarla. En entornos restringidos que impiden crear el caché temporal de pytest,
se puede ejecutar la misma suite con `-p no:cacheprovider`.

Referencias de implementación: [lifespan de FastAPI](https://fastapi.tiangolo.com/advanced/events/),
[clientes y conexiones de HTTPX](https://www.python-httpx.org/advanced/clients/) y
[modelos Pydantic](https://docs.pydantic.dev/latest/concepts/models/).
La confianza nativa sigue las APIs documentadas de
[truststore](https://truststore.readthedocs.io/en/latest/) y
[contextos SSL de HTTPX](https://www.python-httpx.org/advanced/ssl/).

## Realtime Voice 2A: soporte servidor

```text
Navegador futuro <---- WebRTC audio/data ----> OpenAI GPT-Live
                                                |
                                         Responses delegation
                                                |
                                  consult_sng_intelligence(question)
                                                |
                   relay futuro -> /live/delegate -> Agent Core actual
                                                       |
                                              ReadTools -> SnG Control
```

Intelligence no recibe ni retransmite PCM. Live maneja turnos e interrupciones de
forma nativa; no se implementa VAD en Python ni se añaden campos `turn_detection`
de otra API. Live y delegación tienen prompts breves separados del Agent prompt.
Para datos operacionales, la única función disponible es `consult_sng_intelligence`,
con JSON schema estricto (`question`, sin campos adicionales). La delegación usa
`tool_choice=auto`, `parallel_tool_calls=false` y ninguna herramienta alojada.
Las seis tools empresariales permanecen exclusivamente dentro del Agent Core.

La delegación debe resolver el contexto de la sesión en una pregunta autocontenida:
por ejemplo, «la de Ximena» después de una aclaración se convierte en «¿Cómo va Casa
Ximena?». El agente ejecuta una consulta independiente, sin historial ni caché.
Los prompts exigen usar únicamente hechos del resultado, con `spokenAnswer` como
autoridad factual y nombre de proyecto como identidad principal. La respuesta
canónica de `/delegate` conserva el grounding determinista existente. El audio que
Live genera después está guiado por esas instrucciones; este servidor no valida
determinísticamente cada palabra de ese audio.

### Configuración y proveedor

```dotenv
SNG_INTELLIGENCE_LIVE_MODEL=gpt-live-1
SNG_INTELLIGENCE_LIVE_DELEGATION_MODEL=gpt-6-luna
SNG_INTELLIGENCE_LIVE_VOICE=marin
SNG_INTELLIGENCE_LIVE_TIMEOUT_SECONDS=30
SNG_INTELLIGENCE_LIVE_LIFETIME_SECONDS=600
```

Modelos y voz son configurables; Marin es provisional. Se reutiliza `OPENAI_API_KEY`
solo en servidor. Las tres primeras variables son necesarias para crear sesiones;
su ausencia no impide `/health`, chat ni fallback configurados por separado. Timeout
admite 1–60 segundos; lifetime local admite 1–600 segundos. La disponibilidad real
de modelos depende del proyecto OpenAI; una configuración rechazada devuelve error
seguro, nunca cambia de modelo silenciosamente.

El SDK instalado al implementar este bloque (`openai==2.54.0`) no expone Live.
El adapter usa exclusivamente HTTPX async hacia
`POST https://api.openai.com/v1/live/sessions`, con JSON `session` y
`transport: {type: "webrtc", sdp: "..."}`. No acepta URL ni configuración del caller.
TLS valida certificados y hostname con el almacén del sistema; no sigue redirects
ni hereda proxies de entorno. No se añadió otra dependencia.

Se envía `session.store=false`, campo soportado por Live; no se inventa un campo
`delegation.responses.store`. El Agent Core mantiene su propio `store=false` en
Responses API. Esto no afirma ausencia de toda retención operativa del proveedor.
El contexto activo del proveedor es distinto de persistir conversaciones en SnG.
Referencias: [crear sesión Live](https://developers.openai.com/api/reference/resources/live/methods/create),
[delegación](https://developers.openai.com/api/docs/guides/live-delegation) y
[controles de datos](https://developers.openai.com/api/docs/guides/your-data).

### Contratos HTTP y relay del futuro frontend

Los tres endpoints usan `Authorization: Bearer <Google ID Token>` y validan Gerencia
contra `/api/auth/me` en cada request. Solo admiten JSON, sin campos adicionales:

| Endpoint POST | Request | Respuesta |
| --- | --- | --- |
| `/api/intelligence/live/session` | `{"sdp":"<SDP offer>"}` | 201 `{"sessionId":"<opaque>","sdp":"<SDP answer>"}` |
| `/api/intelligence/live/delegate` | `{"sessionId":"<opaque>","callId":"<opaque>","question":"¿Cómo va Casa Ximena?"}` | 200 contrato `ChatResponse` existente |
| `/api/intelligence/live/close` | `{"sessionId":"<opaque>"}` | 200 `{"closed":true}` |

`ChatResponse` contiene exclusivamente `responseType`, `answer`, `spokenAnswer`,
`references` y `presentation`, con la misma construcción desde evidencia de chat.
No se retornan traces, HTTP internals ni credenciales. SDP está limitado a 32 KiB y
se comprueba su estructura de audio/data, ICE y fingerprint; OpenAI realiza la
negociación final. El cuerpo JSON tiene límite 64 KiB y recepción de 10 segundos;
la respuesta del proveedor está limitada a 128 KiB. `question` admite 1–4000
caracteres y rechaza preguntas en blanco; IDs opacos de 1–128 caracteres usan letras,
dígitos, guion o guion bajo. No son URLs ni selectores de rutas.

El futuro navegador deberá:

1. Crear su offer con audio y data channel y enviarlo a `/live/session`; aplicar el
   answer recibido, sin enviar Google token por WebRTC ni recibir la API key.
2. Leer `response.event` y su evento anidado `response.output_item.done`. Para un
   item `function_call`, aceptar únicamente `consult_sng_intelligence`, parsear sus
   argumentos y enviar `sessionId`, `callId` y `question` a `/live/delegate`.
3. Usar la respuesta HTTP canónica para UI/referencias; devolverla por el data channel
   con `response.item.create`, item `{type:"function_call_output",call_id,output}`,
   donde `output` es el JSON serializado de la respuesta. Continuar con
   `response.create`. `response.item.create` no emite confirmación de éxito.
4. En errores no inventar resultados ni reintentar automáticamente la misma llamada.
   Al salir, expirar el plazo local o recibir `session.closed`, limpiar la conexión.
   Para cerrar, enviar `session.close`, procesar `session.closed` y cerrar WebRTC;
   además llamar `/live/close` para revocar autorización local.

Los únicos eventos de cliente permitidos por la configuración del data channel son
`response.item.create`, `response.create` y `session.close`; no se permite
`session.update`. El relay aún no está implementado en Frontend. El servidor valida
ownership y rechaza replay, pero no puede acreditar que un `callId` reenviado por
un navegador provenga realmente de OpenAI. Tampoco puede certificar un tool output
que un cliente altere antes de retransmitirlo. Por ello este canal no es autoridad
de negocio: las operaciones siguen cerradas, read-only y autorizadas por Backend;
las referencias públicas se toman de la respuesta HTTP de Agent Core.

### Registro, cierre, límites y errores

El registry guarda solo ID de sesión, subject oficial, tiempos monotónicos, estado
de ejecución y hasta 64 IDs de llamadas para impedir replay. No guarda credenciales,
SDP, audio, preguntas, transcripts, respuestas ni historia. Presencia equivale a
sesión activa; cierre y expiración eliminan la entrada. Hay limpieza cada 15 segundos,
en accesos y al apagar la aplicación. Reiniciar pierde las sesiones locales.
Desplegar con un solo worker o afinidad de instancia; no hay registry compartido.

Se admiten hasta 128 sesiones por instancia, dos por subject, incluyendo creaciones
pendientes. Una delegación simultánea por sesión; máximo 64 intentos, sin caché de
resultados. Llamadas fallidas también consumen su ID; replay/concurrencia devuelve
409. Si la sesión se cierra o vence durante Agent.run, no se entrega su resultado.

La ventana local de autorización es de 10 minutos como máximo. **No es un parámetro
de duración enviado al proveedor ni un cierre remoto de media.** El navegador debe
aplicar ese límite (o el configurado menor) y cualquier cierre anterior del proveedor.
No se garantiza aquí un máximo WebRTC remoto no publicado: el endpoint oficial de
hangup documenta SIP y no se reutiliza para WebRTC. `/live/close` revoca únicamente
la delegación local; no detiene por sí mismo audio remoto. No hay reconexión automática.

| Situación | HTTP |
| --- | --- |
| Sin sesión Google / no Gerencia | 401 / 403 |
| Sesión Live desconocida, cerrada o expirada / ajena | 404 / 403 |
| Contrato o SDP inválido / cuerpo demasiado grande / MIME incorrecto | 422 / 413 / 415 |
| Replay o llamada concurrente / límite local alcanzado | 409 / 429 |
| Live sin configurar, configuración/modelo rechazado, proveedor 429 | 503 |
| Creación fallida, respuesta inválida o proveedor 5xx | 502 |
| Timeout | 504 |

Los fallos del Agent conservan su traducción segura existente. No se devuelve un
error 401 de Google para un fallo del proveedor. Los logs contienen requestId,
evento created/closed/delegated, duración, contador y categoría segura; no SDP,
subject, sessionId, contenidos ni credenciales. Ninguna grabación se persiste.

`/voice/transcribe`, `/voice` y `/voice/speech` siguen siendo fallback push-to-talk,
con `SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE=es`. Live no llama esos endpoints.
Las pruebas Live usan fake provider y HTTP mock: no necesitan cuentas ni llamadas
pagadas. El smoke de WebRTC, turnos y barge-in queda pendiente del cliente futuro.

## Estado y deuda intencional

**Intelligence 1A:** infraestructura, auth, cliente, contratos, read tools y tests.

**Intelligence 1B implementado:** endpoint chat, AgentModel/OpenAIModel, loop acotado,
seis tools, búsqueda de proyectos y referencias ligadas a evidencia del request.
La verificación con proveedor real depende de disponer de API key, modelo y sesión
Google vigentes; las pruebas automatizadas no sustituyen ese smoke.

**Voice 1A implementado:** STT, consulta por voz al mismo Agent Core y TTS independiente.
**Presentation & Voice 1B implementado:** contrato semántico cerrado, metadata desde
evidence, nombre primero, rendering compartido de answer/spokenAnswer, léxico mínimo
y estilos TTS por responseType. Sin segundo agente ni nueva capacidad operacional.
**Realtime Voice 2A implementado:** soporte servidor de GPT-Live con WebRTC, Responses
delegation y registro efímero de autorización. No incluye implementación del navegador.
**Pendiente en este repositorio/bloque:** cliente WebRTC y smoke de conversación continua,
turnos/interrupciones reales y cierre coordinado con el navegador.
No hay memoria de negocio, persistencia local de conversaciones ni análisis de bytes
documentales. Live mantiene contexto conversacional durante la sesión del proveedor;
Agent Core continúa siendo stateless.
RAG continúa fuera de alcance sin caso de uso explícito de contenido no estructurado.

Referencias OpenAI: [function calling](https://developers.openai.com/api/docs/guides/function-calling)
y [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
