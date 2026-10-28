# SnG Intelligence — Realtime Voice 2C — server-side sideband

Capa conversacional read-only de Gerencia. Agent Core utiliza herramientas de
lectura y un proveedor LLM para responder preguntas. **SnG-Control-Backend es la única
fuente de verdad operacional.**

```text
Gerencia -> Intelligence API -> Agent Core -> ReadTools -> SnG Control
                                    |
                                    +-> AgentModel -> OpenAI
```

No hay acceso a la DB operacional, copia persistida de Project/ProjectTask, cálculo de Lead Time,
escrituras de negocio, RAG ni embeddings. Las conversaciones se guardan en PostgreSQL propio
de Intelligence; ese historial no es autoridad operacional. Voice 1A agrega voz
request/response sobre el mismo agente como fallback. Realtime Voice 2C agrega sesiones GPT-Live y ejecución empresarial por sideband
privado; el navegador conserva WebRTC, captions y controles.

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
# Editar .env con el origen real del backend y una DB exclusiva de Intelligence.
# Ejecutar la migración explícita (carga .env sin imprimir su contenido).
.\.venv\Scripts\python.exe -m dotenv run -- .\.venv\Scripts\python.exe -m app.conversation.persistence.migrate
.\.venv\Scripts\python.exe -m uvicorn app.main:app --loop app.core.event_loop:loop_factory --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
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
| `POST /api/intelligence/chat` | Consulta con conversationId opcional: `responseType`, `answer`, `spokenAnswer`, `references`, `presentation`. |
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
su siguiente pregunta. Con conversationId puede usar el contexto anterior para identificar
el referente; la evidencia de cada ejecución sigue siendo nueva.

Los flags documentales como `canModify` son información recibida; no habilitan
acciones en Intelligence. No se consumen `/file`, `current.zip` ni `initial-handoff.zip`.

Las dependencias FastAPI administran el cliente por request; chat lo conecta a
ReadTools mediante el dispatcher cerrado ToolBox. `get_read_tools` también permite
inyectar ReadTools mediante `Depends`, sin argumentos de identidad. Las tools
verifican Gerencia por sí mismas y validan UUID antes de cualquier I/O. El cliente
también valida UUID. No se aceptan URLs, paths o queries en lugar de identificadores.

La instancia de `SnGControlClient` es solo del request o de una ejecución del Agent Live. `get_control_client` limpia
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
  Excepción limitada de Voice 2C: el contexto privado Live puede retener el SecretStr
  hasta 600 s para reautorizar cada ejecución del sideband; se revoca al terminar
  la sesión. No cambia el ciclo de credenciales de chat ni voz básica.
- `ReadOnlyTransport` impone GET, origen configurado, rutas cerradas permitidas y
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

Se acepta `message`, un string no vacío de hasta 4000 caracteres, y opcionalmente
`conversationId`, recuperado antes por POST /conversation/resume. Se rechazan campos como
token, email, subject, role, isManagement o history. El guard de Gerencia se ejecuta antes de usar el proveedor, incluso
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
reales. Una clave no recuperada se rechaza; en una respuesta final se permite un único
repair antes de producir `ungrounded_reference` (502 en chat/voice básico). Las referencias
de documentos/actividad requieren evidencia previa del proyecto, normalmente obtenida
por find_projects o details. El catálogo no se comparte entre requests.

`AgentModel` es un protocolo pequeño e independiente del SDK. `OpenAIModel` implementa
Responses API del SDK oficial con function calling estricto y JSON Schema para la
respuesta final; el modelo es configurable, no hardcodeado. Se usa `store=False`, sin
historial remoto, streaming, tracing, reintentos de transporte ni herramientas alojadas
(web/shell/etc.). El repair final de grounding es explícito y lo controla Agent Core.
La instancia SDK vive durante un chat, reutiliza conexiones entre rondas y se cierra
al terminar. Su origen se fija a la API oficial; no se hereda `OPENAI_BASE_URL`.

El Agent Core controla cada ronda y ejecuta las tools. El adaptador traduce sus
mensajes a `function_call` y `function_call_output`, enlazados por `call_id`, y
configura la respuesta final mediante `text.format`. Los elementos de razonamiento
del proveedor se reenvían entre rondas solo en memoria durante ese request, incluido
su contenido cifrado cuando está presente. No se usa Conversations ni
`previous_response_id`. Sin conversationId el chat es independiente; con ID recibe
una ventana reciente del historial durable propio, sin replay de tools/razonamiento anteriores.

El modelo recibe la pregunta, contexto conversacional acotado si existe, los schemas
y resultados proyectados de las doce
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
formato humano. En la prosa evita «Lead Time»: usa «plazo» o el estado humano,
por ejemplo «La tarea está en tiempo». La conversión de fechas usa
`America/Bogota`, mediante ZoneInfo y tzdata (también en Windows). No cambia DTO,
evidence, deadlines ni referencias. Protege los nombres recuperados de proyectos,
clientes y ciudades de reemplazos, incluidos nombres que contienen enums o SG.
Fechas sin zona no se asumen UTC; el prompt exige aclarar que falta zona y no
completar nulls. Preparar cotización pendiente no tiene Lead Time activo; Presupuestos
lo inicia con la primera asignación del SG. No se calcula atraso ni se infiere historia
de asignación; Pending sin deadline se puede describir como pendiente de inicio.

### Contrato de presentación y grounding

`responseType` es un enum cerrado. `presentation` es una unión Pydantic discriminada
por `kind`; sus objetos rechazan campos adicionales y el tipo debe coincidir con
`responseType`. No hay metadata libre ni URLs.

| responseType | Uso | Campos de presentation además de kind |
|---|---|---|
| `direct` | Pregunta simple/conteo | `metric: Metric \| null` |
| `management_brief` | Resumen y prioridades | `metrics`, `items`, `areas`, `attentionCoverage`, `tasksCoverage` |
| `project_status` | Estado de un proyecto resuelto | `project: ProjectItem`, `tasks: TaskItem[]` |
| `list` | Enumeración de elementos | `items: (ProjectItem \| TaskItem)[]` |
| `comparison` | Áreas o 2–4 proyectos actuales | `areas: AreaItem[]`, `projects: ComparedProjectItem[]` (sin mezclar) |
| `clarification` | Ambigüedad/identificación insuficiente | `candidates: ProjectItem[]` |
| `unavailable` | Dato no disponible | `reason: "data_not_available"` |

```text
Metric      = {key: string, label: string, value: int}
ProjectItem = {kind: "project", projectId: UUID, projectName: string,
               sgCode: string | null, clientName: string | null, city: string | null}
TaskItem    = {kind: "task", projectId: UUID, projectName: string, sgCode: string | null,
               taskId: UUID, area: string | null, task: string | null, status: string | null,
               leadTimeStatus: string | null, deadlineAt: datetime | null}
AreaItem    = {area: string | null, pending: int, inProgress: int, completed: int,
               overdue: int, dueToday: int, dueSoon: int}
```

El dispatcher mantiene `available_presentation` junto al catálogo de referencias,
solo durante el request. El modelo selecciona `presentation_keys`, nunca envía IDs,
labels, cifras ni fechas como autoridad. El builder obtiene los objetos desde las
respuestas tipadas de tools: métricas y áreas del overview, proyectos de búsquedas/
detalles y tareas de overview/tasks/details. Una clave ausente o de tipo incompatible
se rechaza con `ungrounded_reference`, sujeto al mismo repair final acotado.
No se recalculan totales ni estados. Las áreas
reconocidas y los enums usan labels humanos; un valor desconocido se representa con
label null, sin inventar su significado ni cambiar el valor original en evidence.

`project_status` requiere detalles recuperados y contiene todas sus tareas oficiales,
sin adivinar cuál es la actual. El nombre oficial abre la respuesta incluso si se
resolvió por SG. `clarification` incluye todos los candidatos de una búsqueda ambigua,
o una lista vacía si falta identificación. No permite presentar solo el primer candidato.
En chat con conversationId, «El primero» puede resolverse usando referencias ordenadas
del historial reciente. Sin ID y en voz básica no hay contexto garantizado; hay que
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

## Realtime Voice 2C: control server-side

```text
Browser <-- WebRTC audio + captions/UI --> GPT-Live
                                             |
                                    private sideband WebSocket
                                             |
                                      SnG Intelligence
                                             |
                       ephemeral credentials -> Agent Core -> SnG Control
```

El servidor es el único ejecutor de `consult_sng_intelligence`. Responses delegation
recibe contexto de Live y produce preguntas autocontenidas para el Agent con evidencia nueva y contexto opcional.
Las doce tools read-only, grounding, referencias y Presentation no se duplican ni
se exponen directamente a Live. El browser no interpreta llamadas, no consulta
`/live/delegate` y no envía tool outputs ni `response.create`. Ese endpoint fue eliminado.

El SDK instalado (`openai==2.54.0`) no expone Live/sideband. Se mantiene el adapter
HTTPX cerrado para creación y se añade `websockets==15.0.1` para attach async al origen
fijo `wss://api.openai.com/v1/live/sessions/{session_id}/attach`. Conserva el ID
recibido y usa la misma API key de creación. TLS verifica certificado y hostname;
no hay redirects, proxies heredados ni URL elegible por usuarios/modelos.

### Configuración

```dotenv
SNG_INTELLIGENCE_LIVE_MODEL=gpt-live-1
SNG_INTELLIGENCE_LIVE_DELEGATION_MODEL=gpt-6-luna
SNG_INTELLIGENCE_LIVE_VOICE=marin
SNG_INTELLIGENCE_LIVE_TIMEOUT_SECONDS=30
SNG_INTELLIGENCE_LIVE_LIFETIME_SECONDS=600
```

Se reutiliza `OPENAI_API_KEY` solo server-side. Modelos/voz son configurables;
Marin es provisional. Live sigue siendo opcional para startup. Timeout admite
1–60 segundos; lifetime admite 1–600. Se envía `session.store=false`; el Agent
mantiene `store=false` en Responses. No se añade un campo anidado store no soportado.
Esto no afirma ausencia de toda retención operativa del proveedor.

### Contratos y creación atómica

| POST Management-only | Request | Response |
| --- | --- | --- |
| `/api/intelligence/live/session` | `{"sdp":"<offer>"}` | 201 `{"sessionId":"<opaque>","sdp":"<answer>"}` |
| `/api/intelligence/live/close` | `{"sessionId":"<opaque>"}` | 200 `{"closed":true}` |

Se autentica contra `/api/auth/me` antes de capturar la credencial. Tras crear la
sesión remota se registra ownership, se captura el contexto efímero y se adjunta
el sideband **antes** del 201. Ready significa handshake WebSocket autenticado
completado; no hay roundtrip ficticio ni `session.start` repetido. Si attach falla,
se revocan credenciales y registry, se hace cleanup remoto best-effort y no se
entrega una sesión usable. No se devuelven socket URL, claves ni estado interno.

SDP máximo 32 KiB, cuerpo HTTP máximo 64 KiB, respuesta de creación máxima 128 KiB.
Se admiten dos sesiones por subject y 128 por instancia, incluyendo creaciones
pendientes. Cada sesión admite 64 llamadas aceptadas, batch de hasta ocho y un
batch empresarial activo a la vez. No hay caché de resultados ni reintento de tools.

### Eventos y ejecución

El parser Pydantic cerrado reconoce `session.delegation.created`, `response.event`
con `response.created`, `response.output_item.done` y `response.completed`, además
de fallos y cierre. Asocia delegation ID, response ID y call ID. Solo items
`function_call` finalizados con nombre `consult_sng_intelligence` y JSON estricto
`{question: string}` de 1–4000 caracteres pueden ejecutar. Deltas o arguments.done
solos nunca ejecutan. Otros nombres o argumentos inválidos cierran el flujo de forma segura.

Se acumula únicamente el batch pendiente hasta `response.completed` (su output
puede estar vacío). Se ejecuta cada llamada una sola vez por sesión/callId; las
repetidas se ignoran sin reenviar resultados. Cada Agent.run vuelve a validar
Gerencia y subject. Para cada resultado se envía `response.item.create` con item
`{type:"function_call_output",call_id,output}`, usando como output JSON solo
`responseType` y `spokenAnswer`. Después del batch se envía una sola `response.create`.
No se envían trazas, URLs, auth, historial de tools ni Presentation innecesaria a Live.

Los contratos semánticos completos siguen disponibles en chat/fallback; el resultado
del Agent los construye igualmente, pero Live solo necesita el contenido hablado.
El panel Live presenta captions, sin fabricar tarjetas/referencias desde audio ni
recibir resultados empresariales del relay eliminado. Chat y fallback conservan
sus referencias y tarjetas. El audio Live está guiado por prompts; no se afirma
validación determinista de cada palabra pronunciada.

### Contexto efímero de credenciales (excepción autorizada solo para Live)

`LiveCredentialContext` mantiene privadamente `sessionId -> SecretStr + expectedSubject
+ expiresAt`, fuera de LiveEntry, registry y contratos serializables. No es una
política nueva para chat ni voz básica. SecretStr reduce exposición accidental;
**no cifra memoria**. No se guarda en disco, DB, Redis, logs, prompts o proveedor.

El límite es el menor entre vida restante de Live, configuración local y 600 s.
Antes de **cada** Agent.run se consulta `/api/auth/me` con esa credencial: debe
seguir siendo Gerencia y tener exactamente el subject original. 401, 403, subject
distinto o isManagement=false impiden Agent.run, revocan y cierran la sesión.
El cliente Backend limpia su referencia al finalizar cada ejecución. La credencial
no entra en tool arguments/evidence ni en OpenAI; los guards siguen activos.

Cierre, expiración, desconexión, fallo definitivo, rollback y shutdown revocan el
contexto antes de esperar I/O remoto. Las consultas en curso se cancelan y no se
entregan resultados tardíos. Una nueva sesión exige nueva autenticación HTTP;
ningún reattach de cleanup renueva credenciales. Python no garantiza borrado físico
inmediato de cadenas; la garantía es eliminar referencias bajo control de la aplicación.

### Cierre, fallos y recursos

`/live/close` valida owner, revoca, envía `session.close`, espera brevemente
`session.closed`, cancela tasks y cierra WebSocket. Repetir cierre es idempotente
para el mismo owner mientras se conserve su tombstone acotado (máximo 128).
Desconocidos devuelven 404 y otra identidad 403. El navegador también libera mic,
data channel, peer connection y audio; aplica máximo absoluto de 600 s.

Expiración y shutdown siguen el mismo cleanup. Si el socket falla, se intenta una
sola conexión adicional exclusivamente para cierre remoto, con tiempo acotado;
no reanuda tools ni crea una conversación. Si OpenAI es inalcanzable, el cierre
remoto no puede garantizarse: se registra `remote_close_unconfirmed`, se revoca
localmente y el navegador conserva sus límites y manejo de cierre/pérdida de red.
No hay reconexión infinita. El registry es por instancia: usar un worker o afinidad;
reiniciar pierde sesiones y credenciales locales.

El sideband puede recibir copias de audio/transcripts por protocolo; se descartan
sin procesar audio, acumular historia ni persistir. El media principal permanece
en WebRTC. Cada frame tiene límite 256 KiB y la cola de transporte ocho frames.
Los logs del transporte están deshabilitados; observabilidad emite solo requestId,
eventos, duración, contador y categorías seguras. Fallos de configuración/rate limit
son 503, timeout 504, attach/protocolo 502; nunca se expone el error raw del proveedor.

El data channel permite al cliente solo `session.close`; recibe exclusivamente
lifecycle, captions, error y progreso Responses sin items de tools/razonamiento.
La conexión privada conserva permisos de servidor. No hay endpoint público de attach.

`/voice/transcribe`, `/voice` y `/voice/speech` permanecen como fallback independiente,
con transcripción española configurada. No hay VAD Python, cola TTS de Live, DB,
memoria persistente, RAG ni LangGraph. Las pruebas usan providers/sidebands fake y
HTTP mock, sin llamadas pagadas. Smoke acústico real queda para revisión posterior.

Protocolo oficial: [sideband](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live),
[delegación](https://developers.openai.com/api/docs/guides/live-delegation),
[permisos del data channel](https://developers.openai.com/api/reference/resources/live/methods/create).

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
**Realtime Voice 2C implementado:** sideband privado, ejecución server-side y credenciales
efímeras. Frontend autorizado actualizado para UI/captions sin relay empresarial.
**Pendiente:** smoke real de conversación continua, turnos/interrupciones y cierre remoto.
No hay memoria de negocio ni persistencia durable de conversaciones. Management 4B
autoriza análisis transitorio de bytes documentales mediante el servicio aislado. Live mantiene contexto conversacional durante la sesión del proveedor;
Agent Core no almacena memoria propia; Conversation 3A le entrega contexto opcional.
RAG continúa fuera de alcance sin caso de uso explícito de contenido no estructurado.

Referencias OpenAI: [function calling](https://developers.openai.com/api/docs/guides/function-calling)
y [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

## Performance 2D — instrumentación y mejoras locales

Voice 2C se conserva. [PERFORMANCE_2D.md](PERFORMANCE_2D.md) describe las métricas,
configuración Live independiente, benchmark offline/manual y límites de medición.
Cada Agent.run de Live revalida Gerencia una vez; sus tools reutilizan un contexto
privado ligado a esa ejecución, que se invalida al salir. No hay cache entre turnos.
`find_projects` ejecuta en paralelo las dos lecturas independientes y cancela la hermana
ante error/cancelación. Chat y fallback conservan sus comprobaciones por tool.
No se cambió el modelo seleccionado ni se configuró VAD de Realtime en GPT-Live.


## Persistent Conversations V1 — historial durable, Live temporal

PostgreSQL propio de Intelligence conserva conversaciones y todos los turnos validados.
No se accede a la base ni a tablas operacionales de SnG Control. No hay TTL de conversación,
retención automática ni eliminación de turnos. Long Context V1 añade la memoria estructurada
descrita abajo. Cerrar un hilo lo archiva; no borra
su historial. El límite de Live y de sus credenciales sigue siendo independiente: máximo 600 s.

```text
PostgreSQL Intelligence <- ConversationRepository <- ConversationStore/runtime
                                                        |            |
                                                       Chat       Live sideband
                                                        +-- Agent Core --+
                                                               |
                                                     API SnG-Control-Backend
```

### Configuración y migración

`SNG_INTELLIGENCE_DATABASE_URL` es obligatoria y secreta. Usar una base exclusiva,
un rol con acceso únicamente a ella y las opciones TLS apropiadas al despliegue.
No imprimir DSN ni usar la conexión de la base operacional. `.env.example` contiene
solo un ejemplo local sin credenciales. La variable antigua de TTL ya no se consume.

Dependencia: `psycopg[binary,pool]` 3, sin ORM. El pool async tiene timeout 5 s,
1–5 conexiones y espera acotada; queries y locks tienen timeout. SQL vive únicamente
en `app/conversation/persistence/`. No se mantiene una transacción durante el LLM.

Antes de iniciar el API, provisionar la base y ejecutar explícitamente:

```powershell
.\.venv\Scripts\python.exe -m dotenv run -- .\.venv\Scripts\python.exe -m app.conversation.persistence.migrate
```

Si las variables ya están en el entorno, basta `python -m app.conversation.persistence.migrate`.
La migración `001_conversations.sql` crea el schema `intelligence_conversations`, tablas
`conversations` y `turns`. `002_conversation_memory.sql` añade únicamente `conversation_memory`.
El runner descubre migraciones ordenadas, valida checksums de las aplicadas y registra cada
versión en `schema_version`. En una base v1 aplica solo 002. Repetir el comando es idempotente;
rechaza gaps, checksums alterados y versiones desconocidas. No recrea tablas ni borra historial.
El checksum normaliza saltos de línea entre Windows/Linux. Startup verifica versión,
checksum y columnas; falla de forma segura si falta configuración, conexión o migración.
No hay creación automática de tablas en startup. `/health` indica proceso vivo;
`/readiness` valida acceso/schema de conversaciones y devuelve 503 seguro si falla.

En Windows usar `--loop app.core.event_loop:loop_factory` en Uvicorn (mínimo 0.36):
psycopg async requiere un SelectorEventLoop. El runner de migraciones ya lo selecciona.
Ver [compatibilidad async de psycopg](https://www.psycopg.org/psycopg3/docs/advanced/async.html).

### Contratos y continuidad

- `POST /api/intelligence/conversation/resume`, body `{}`: 200 con
  `{conversationId, createdAt, lastActivityAt}`. Retoma el único hilo abierto del subject
  autenticado; si no existe, lo crea. Resumes concurrentes obtienen el mismo ID.
- `POST /api/intelligence/conversation`, body `{}`: 201 con el mismo contrato.
  Es una nueva conversación intencional: archiva el hilo anterior y crea otro vacío.
- `POST /api/intelligence/conversation/close`, body `{conversationId}`: `{closed:true}`.
  Idempotente para su propietario; otro subject recibe 403. No se acepta subject del caller.
- IDs opacos de 43 caracteres, sin `expiresAt`. Chat y Live conservan sus contratos
  y `conversationId` opcional. Chat sin ID y voz básica siguen independientes.
- Frontend monta con `resume`. F5/reabrir navegador/reiniciar Intelligence conserva el ID
  y contexto durable del subject. El historial visual del panel sigue siendo local: V1 no
  agrega una pantalla ni endpoint de listado de turnos; las tarjetas antiguas no se reconstruyen.
- Solo `404 conversation_unavailable` limpia el selector local, hace resume y reintenta
  la operación original una vez. No reintenta otros 404, 409, 429 ni 5xx. En Live se liberan
  tracks/peer provisionales antes del resume y se crea una conexión nueva para ese único retry.
  Los IDs de la versión antigua en RAM no pueden reconstruirse: usan esta misma recuperación.
- Cerrar panel, logout o cambio de identidad libera solicitudes/media/estado local;
  no archiva el hilo durable. Nueva conversación sí archiva el hilo y limpia la UI.
  Expirar/cerrar Live permite reconectar voz en la misma conversación.

### Historial, seguridad y concurrencia

Repository async: create/get/get_current/append_turn/close/list_turns. Persiste texto del
usuario, respuesta semántica validada, modalidad, responseType, timestamp y referencias
mínimas verificadas. No guarda Presentation, tool output/protocol, reasoning, objetos OpenAI,
provider/session IDs, Google tokens, API keys, SDP ni audio. Los guards rechazan credenciales
reconocibles antes de guardarlas; logs contienen solo categorías, contadores y duraciones.

La DB conserva todos los turnos. `build_context` combina memoria estructurada y hasta 16
turnos recientes, dentro de 16000 caracteres renderizados. Descarta turnos completos solo
de la proyección al modelo.
Live mantiene su límite conservador de 6000 bytes UTF-8 de input. Historia y referencias
sirven para resolver nombres/ordinales, nunca como evidencia actual. Cada Agent.run vuelve
a consultar herramientas oficiales con evidencia nueva. Grounding y `store=false` no cambian.

Se guarda únicamente después de Agent success (o unavailable recuperado y validado en Live),
antes de responder/enviar function_call_output. Fallar append produce un error seguro, no éxito
sin persistencia. Versión/ordinal y transacción corta hacen atómico append; una escritura
obsoleta recibe 409, sin retry automático. PK `(conversation_id, ordinal)` evita duplicados.
Un índice único parcial mantiene un solo hilo abierto por subject; create/resume se serializan
con advisory lock de transacción. Cerrar/archivar no elimina los turnos.

Runtime contiene locks, operaciones y bindings Live, sin historial durable ni credenciales.
Máximo 128 runtimes activos y 8 operaciones por hilo. Shutdown cancela operaciones, cierra
Live, revoca credenciales y cierra pool; nunca archiva ni borra conversaciones por reiniciar.
**V1 se despliega con un solo worker/proceso**: la versión DB protege escrituras obsoletas
entre instancias, pero no distribuye locks de ejecución, cancelación ni bindings Live.
Durabilidad y coordinación multirréplica son problemas distintos.

### Validación y smoke de despliegue

Tests offline usan repository de prueba compartido entre instancias y mocks psycopg;
no dependen de la DB del usuario ni llaman proveedores. Cubren restart con turnos anteriores,
lecturas frescas, aislamiento, resume concurrente, CAS, rollback, schema incompatible,
fallo de append sin éxito, contexto acotado sin pérdida de historial y recuperación frontend.

También se verificó PostgreSQL 17 en un clúster temporal exclusivo con datos sintéticos:
migración/repetición, resume concurrente, 20 turnos, ventana de contexto, CAS, aislamiento,
restart de DB/pool/runtime con mismo ID/historial, nuevo hilo y cierre idempotente.
El clúster se detuvo y eliminó; no se usó la base operacional ni la DB real del usuario.

Después de configurar/migrar una DB de desarrollo, ejecutar el smoke manual: resume y dos
turnos sobre un proyecto real; reiniciar Uvicorn con la misma DB; F5 y resume; comprobar el
mismo ID y una consulta referencial con lectura fresca. Repetir al reabrir navegador y al
reconectar Live tras restart/expiry. La sesión WebRTC anterior no sobrevive al reinicio.
No confundir los tests offline con aprobación de este smoke real.

Fuera de V1: títulos, lista/búsqueda de conversaciones, rename/delete UI, resúmenes,
compacción, sincronización realtime entre dispositivos y renovación Live transparente.


## Management Intelligence 4A — análisis ejecutivo

Implementado sobre el mismo Agent Core de texto/Live y Conversation 3A. Backend sigue
siendo la única autoridad operacional. No cambian HTTP routes, WebRTC, sideband,
autenticación, credenciales, `store=false`, TLS ni STT/TTS. No hay estado empresarial nuevo.

### Snapshot gerencial

`get_management_snapshot()` añade una tool cerrada al catálogo (ocho tools en 4A; nueve con 4B; once con el dossier confirmado; doce con portfolio;
se conservan las seis anteriores). Autoriza primero y obtiene overview y management tasks
concurrentemente. Cancela y espera todas las lecturas pendientes ante fallo/cancelación.
El contrato contiene:

- `overview`: totales, counts de tareas/plazos y áreas oficiales; attention mantiene el
  orden del Backend, con hasta 8 items.
- `attentionCoverage`: `{total, returned, hasMore}` usando el total oficial de attention.
- `tasks`: hasta 12 tareas con campos operativos compactos, sin identidad de usuario ni
  permisos. Fuera de attention, selección estable por estado oficial: Overdue, DueToday,
  DueSoon, Pending/InProgress, otros. Nunca compara el reloj para decidir estados.
- `tasksCoverage`: `{total, returned, hasMore}` sobre la lista de tareas recuperada.
- Catálogos run-local de referencias/presentation. `complete=false` si cualquier slice
  tiene más elementos. Totales y áreas no se calculan a partir de una muestra.

Los límites son explícitos; `list_management_tasks` mantiene el camino de listado completo.
Siguen vigentes 48 000 bytes por resultado y 128 000 por transcript interno: un exceso falla
seguro, nunca se elimina información silenciosamente. El snapshot es una lectura actual de
endpoints independientes, no una transacción atómica ni un snapshot histórico persistido.

### Brief, atención y áreas

El prompt prioriza situación general, atención inmediata, carga por áreas, próximos
vencimientos y una recomendación breve identificada como tal. El builder preserva el orden
Backend de los items de atención seleccionados, aunque el modelo los pida en otro orden.
`management_brief` admite `areas`, `attentionCoverage` y `tasksCoverage` además de metrics/items;
el Frontend muestra cobertura parcial sin confundir datos recuperados con elementos visibles.

Los counts por área sustentan concentración de trabajo, no saturación del personal,
productividad ni capacidad. Un posible cuello de botella debe expresarse como hipótesis
sustentada; sin evidencia suficiente, se indica esa limitación. No se generan risk scores,
semáforos, progreso porcentual ni pronósticos. Pendiente sin fecha no equivale a atrasado.

### Comparación actual de proyectos

`compare_projects(project_ids, include_documents, include_activity)` requiere 2–4 UUID
distintos presentes en evidencia de ese run, sin ambigüedad pendiente. Ambos booleanos
son obligatorios en el schema estricto; el prompt indica false salvo petición explícita.
Normalmente se resuelven nombres/SG con `find_projects`, luego se comparan los proyectos.
La validación de todo el batch rechaza IDs inventados antes de hacer lecturas.

Los detalles/tareas de todos los proyectos se consultan en paralelo. Documentos y avisos
solo se consultan cuando se solicitan, también en paralelo; nunca se descargan bytes.
La estructura interna conserva detalles y metadata oficial. Presentation añade:

```text
ComparedProjectItem = {project: ProjectItem, metrics: Metric[], tasks: TaskItem[]}
comparison = {kind: "comparison", areas: [], projects: ComparedProjectItem[2..4]}
```

El servidor cuenta tareas por status/leadTimeStatus oficiales, sin recalcular ninguno.
Solo añade counts de documentos/avisos si se leyeron (documentos devueltos/vigentes devueltos,
no una afirmación sobre contenido ni historia completa). Labels, IDs y fechas salen de evidencia.
El modelo selecciona todas las claves project: del conjunto; no suministra métricas propias.
La respuesta garantiza referencias de todos los proyectos en el orden comparado, aunque
el modelo omita/reordene reference_keys. Las fechas originales se preservan; la prosa usa
America/Bogota. Si faltan deadlines, cualquier comparación de fechas se limita a las conocidas.

El Frontend usa cards con links seguros y métricas semánticas, tareas desplegables,
columnas cuando caben y cards apiladas en móvil; omite campos opcionales ausentes.
No introduce otra página ni deduce tarjetas a partir de captions Live.

### Continuidad, voz y límites analíticos

Conversation 3A conserva referencias ordenadas para «el segundo», «de esos dos» y seguimientos
texto → Live → texto. Cada run vuelve a leer datos actuales; el historial no autoriza IDs
como evidencia nueva ni conserva metrics como verdad. Basic voice sigue independiente.
Live normalmente responde en 2–4 frases, sin recitar tablas. `spokenAnswer` usa el mismo
contenido que `answer`, con rendering determinista; no añade hechos.

Facturación, margen, presupuesto vendido y cambios desde ayer siguen sin estar disponibles.
El prompt exige `unavailable`: conversación previa no es una serie temporal comparable.
Las garantías de contratos/IDs/metadata son deterministas; la exactitud de toda la prosa y
la calidad de recomendaciones siguen requiriendo evaluación del proveedor real.

### Rendimiento y evaluación offline

Se registran `management_snapshot_ms` y `comparison_fetch_ms`, además de `agent_model_ms`,
`backend_tools_ms`, `agent_total_ms`, llamadas y rondas. HTTP usa un scope numérico propio;
Live conserva su scope existente. No se registran nombres, preguntas, respuestas ni IDs.

`tests/test_management_analysis.py` contiene evals offline A–H: brief, atención, áreas,
comparación, pendientes, fecha más próxima cross-modal, cuello de botella insuficiente y
facturación no disponible. Incluye límites, ambigüedad, cancelación, aislamiento y evidencia.
Con un FakeModel guionado, brief usa 1 ronda de tool / 2 llamadas al modelo; comparación
usa resolución + fetch (2 rondas / 3 llamadas al modelo si las búsquedas vienen juntas).
No es una medición de latencia real ni garantía de selección/rondas del proveedor. El adaptador
conserva `parallel_tool_calls=false`; los fetches internos sí son concurrentes.

### Smoke real pendiente (manual, proveedor pagado)

En una misma conversación, con Gerencia autenticada y Backend actual:

1. «Dame un resumen ejecutivo.» — snapshot, métricas oficiales, cobertura y recomendación.
2. «¿Qué debería preocuparme hoy?» — attention en orden Backend, sin score inventado.
3. «¿Dónde tenemos más carga?» — counts por área, sin atribuir saturación humana.
4. «Compara Casa Ximena con Smoke Test Proyecto.» — resolver nombres reales; aclarar si hay
   ambigüedad/no existen; comparar 2 proyectos y revisar cards/referencias.
5. «¿Cuál de los dos tiene más pendientes?» — conservar referentes, volver a consultar.
6. Por Live: «¿Y cuál vence primero?» — mismos referentes, fechas actuales conocidas,
   Colombia, respuesta oral breve; no inferir vencimiento si no hay fecha.
7. «¿Tenemos un cuello de botella?» — separar evidencia de hipótesis o insuficiencia.
8. «¿Cuánto facturamos este mes?» — unavailable, ninguna cifra inventada.

Comprobar también cards en móvil y «Amplía el segundo proyecto» al volver a texto.
No guardar credenciales, audio, SDP, transcripciones ni resultados empresariales del smoke.
Esta batería no se ejecuta automáticamente desde pytest.


## Management Intelligence 4B — Project Document Intelligence

Implementado: análisis directo, transitorio y **solo para Gerencia** de archivos obtenidos
exclusivamente desde SnG-Control-Backend. Metadata y contenido son dos operaciones distintas.
El Backend sigue siendo la fuente de verdad operacional y de permisos/documentos.

### Flujo y selección

`get_project_documents` resuelve listados, fechas, actuales/históricos y envío inicial sin bytes.
`analyze_project_documents` es la novena tool cerrada: recibe `selections` (1–4 proyectos ya
resueltos en el run), `question` y `compare_versions`. Cada selección exige `project_id`,
`scope`, `document_ids` y `filename` (campos no usados: `[]`/`null`).

- `current`: todos los `isCurrent` oficiales, default semántico cuando no piden históricos.
- `initial_handoff`: exclusivamente `InitialHandoff.documentIds`, no inferidos por fechas.
- `selected`: IDs recuperados en el run o nombre exacto tras consultar metadata.
- `all_versions`: actuales e históricos expuestos oficialmente, solo a petición explícita.

El máximo es **4 archivos en total**, también entre varios proyectos. Si hay más, se muestran
opciones y se pide selección sin tomar cuatro arbitrariamente. Nombres duplicados requieren
aclaración por fecha/actual/histórico/envío inicial. Comparar versiones requiere un conjunto
conectado por `replacedByDocumentId`; el nombre por sí solo no prueba parentesco.

El servicio valida metadata fresca antes de cada descarga, y vuelve a comprobarla después
para rechazar cambios durante la operación. El único transporte de bytes es
`SnGControlClient.download_project_document` → GET
`/api/projects/{projectId}/documents/{documentId}/file`, con Authorization server-side,
streaming acotado, TLS verificado, timeout existente y redirects deshabilitados.
El Backend sirve normalmente `application/octet-stream`; extensión, metadata y firmas mínimas
permiten verificar el formato sin confiar únicamente en ese header.

### Configuración y proveedor

| Variable | Default / comportamiento |
| --- | --- |
| `SNG_INTELLIGENCE_DOCUMENT_MODEL` | Vacío usa `SNG_INTELLIGENCE_MODEL` |
| `SNG_INTELLIGENCE_DOCUMENT_MAX_FILE_BYTES` | 20 000 000 |
| `SNG_INTELLIGENCE_DOCUMENT_MAX_TOTAL_BYTES` | 30 000 000 |
| `SNG_INTELLIGENCE_DOCUMENT_PDF_DETAIL` | `auto`; admite `low`, `auto`, `high` |
| `SNG_INTELLIGENCE_DOCUMENT_REASONING_EFFORT` | `low`; `omit` omite reasoning para modelos sin soporte |
| `SNG_INTELLIGENCE_DOCUMENT_MAX_OUTPUT_TOKENS` | 1600; rango 256–4000 |

Los límites se validan positivos, archivo <= total, y ambos <=49 000 000 bytes, deliberadamente
por debajo del techo de 50 MB. Nunca se truncan bytes. Configuración inválida falla de forma segura
al usar la funcionalidad; no impide que health arranque sin proveedor.

`DocumentAnalysisService` está separado del Agent Core. Utiliza el SDK oficial **Responses API**,
`input_file` con `file_data` base64 inline y nombres controlados `document-N.ext`; las imágenes
usan `input_image`. `store=false`, resultado JSON estricto (`answer`, `source_keys`, `limitations`),
timeout de proveedor 60 s, sin retries automáticos ni tools en esa llamada. No recibe historial,
identidad, credential context ni credencial Google. La API key se usa solo como autenticación SDK,
nunca como contenido. Overrides documentales no cambian el modelo de chat ni el de Live.

No se utiliza Files API, file IDs durables, Conversations, `previous_response_id`, File Search,
RAG, embeddings, vector stores, DB, extracción local ni OCR local.
Según la [guía oficial de file inputs](https://developers.openai.com/api/docs/guides/file-inputs),
PDF puede incluir texto e imágenes de página. `auto` queda bajo la decisión del proveedor y,
para GPT-5.6 o posterior, puede usar `high`: **auto no garantiza bajo coste**. Configura `low`
si necesitas evitar esa selección visual de mayor coste. El modelo elegido debe soportar
los tipos/visión/reasoning requeridos; respuestas incompatibles se traducen a errores seguros.

### Formatos y limitaciones

Formatos admitidos: `.pdf`, `.doc`, `.docx`, `.rtf`, `.odt`, `.csv`, `.tsv`, `.xls`, `.xlsx`,
`.ppt`, `.pptx`, `.txt`, `.md`, `.json`, `.xml`, `.png`, `.jpg`, `.jpeg`, `.webp`.

No se analizan ejecutables, scripts, ZIP/RAR/7z ni binarios arbitrarios aunque existan como metadata.
La inspección mínima de OOXML/ODT mira el directorio ZIP acotado y marcadores de formato;
no extrae ni ejecuta contenido. No es un antivirus ni una garantía de legibilidad del archivo.
No se ejecutan macros, fórmulas, scripts, enlaces ni attachments incrustados.

El proveedor extrae texto de documentos no PDF; imágenes/gráficos incrustados pueden no estar
incluidos. En spreadsheets procesa hasta las primeras 1000 filas por hoja con representación
augmentada: **no garantiza cálculos exhaustivos ni interpretación de toda la hoja**. Esas
limitaciones se añaden a la respuesta cuando corresponden. Archivos corruptos, cifrados o
ilegibles pueden fallar o devolver limitaciones; nunca se inventa contenido ausente.
Los límites previos del Agent (rondas, llamadas, tamaño de tool results y tiempo total) se conservan;
colecciones de metadata que superen ese presupuesto fallan de forma segura, sin truncado silencioso.

### Seguridad, grounding y ciclo de vida

Documentos y nombres son datos no confiables. El prompt ignora instrucciones internas y solicita
paráfrasis breves atribuidas; esto se refuerza aislando un modelo sin tools, HTTP ni credenciales de
usuario. No se presenta el prompt como garantía absoluta contra toda manipulación de la prosa.
Las source keys deben corresponder a archivos efectivamente analizados. IDs, filename, fecha,
actual/histórico, envío inicial y relación de reemplazo provienen exclusivamente de metadata Backend.
El Agent no puede reemplazar el resultado documental validado con una nueva conclusión inventada.
La exactitud semántica de la interpretación todavía requiere comprobar el archivo en el smoke.

«El documento indica que fue aprobado» es un hecho atribuido al archivo, **no** el estado operacional
actual. Para tareas, plazos, permisos y estado actual se consulta Backend con las tools existentes.
No se inventan páginas, riesgos actuales, relaciones entre documentos ni cifras ausentes.

`DownloadedDocument` no es serializable ni tiene contenido en repr. Los buffers se mantienen solo
durante el análisis, sin escribir a disco; se cierran y liberan referencias en éxito, error y
cancelación. Python/SDK no garantizan zeroization física de RAM. No hay almacenamiento durable ni
cache de análisis. Logs y errores no contienen bytes, base64, nombres, respuestas ni credenciales.
`store=false` no es una afirmación sobre todas las políticas de retención del proveedor.

### Presentation, Conversation y Live

`document_analysis` presenta `project`, `relatedProjects` y `sources` verificadas, con `kind: document`,
`documentId`, `projectId`, `filename`, `sizeBytes`, `uploadedAt`, `isCurrent`, `inInitialHandoff`
y `replacedByDocumentId`. No bytes, URLs de archivo ni detalles del modelo. El Frontend muestra
«Fuentes» con fecha de Colombia, estado y tamaño, y navega al detalle del proyecto; no agrega visor.
Las listas de metadata y aclaraciones también admiten documentos. Las referencias `type: document`
solo llevan documentId, projectId y label.

Conversation 3A retiene esas referencias mínimas y la respuesta conversacional normal en su historial
conversacional existente. No guarda archivos, base64, texto extraído completo ni análisis separado.
Cada follow-up resuelve el referente y **vuelve a descargar/analizar** con metadata fresca.
Live usa el mismo Agent/servicio mediante sideband y revalida Gerencia antes de cada ejecución.
No cambia WebRTC, protocolo sideband, lifecycle de credenciales ni ejecutor del browser. El proveedor
Live recibe solo responseType/spokenAnswer del resultado, no archivos. Texto/voz básica muestran
fuentes estructuradas; Live conserva la experiencia de captions/voz existente.

Métricas numéricas nuevas: `document_metadata_ms`, `document_download_ms`,
`document_analysis_provider_ms`, `document_analysis_total_ms`, `documents_count`, `total_bytes`.
No se registran IDs, filenames, contenido ni datos de usuario. Las fases no ejecutadas no se
presentan como mediciones de latencia cero. Los tests usan archivos sintéticos y provider mocks.

### Smoke real 4B preparado (manual, pendiente)

Usar una cuenta de Gerencia y un proyecto real con documentos accesibles; conservar una misma
Conversation para los follow-ups y su paso a Live. Configurar variables documentales y credenciales
en entorno, sin imprimirlas ni guardar respuestas/archivos empresariales en el repositorio.
No ejecutar automáticamente llamadas pagadas.

1. «¿Qué documentos tiene Smoke Test Proyecto?» — metadata, cero descargas.
2. «Resúmeme sus documentos actuales.» — solo current; >4 pide selección.
3. «¿Qué dice <nombre-real-de-un-PDF>?» — usar nombre recuperado; duplicados piden aclaración.
4. «¿Menciona algún requisito de vidrio?» — relectura y fuente del mismo documento.
5. «¿Qué documentos estaban en el envío inicial?» — metadata del snapshot, cero bytes.
6. Si existe replacement oficial: «Compara la versión anterior con la actual.»
7. «¿Y menciona protección UV?» — no inventar si falta o no es legible.
8. Por Live: «Resúmeme lo más importante de ese documento.» — continuidad cross-modal.

Comparar la explicación con el archivo real: fuentes y filenames correctos, sin hechos inventados
ni estados operacionales derivados del documento. Verificar navegación, todos los candidatos
ambiguos, limpieza al cancelar y timings/byte counts sin contenido en logs. Registrar solo el
resultado de la comprobación y métricas no sensibles, no transcripts ni dumps. Smoke real 4B
no ejecutado por la suite offline; requiere esta revisión antes de publicar.

### Identity & Areas V1

El cliente SnG Control envía siempre `X-SnG-Active-Role: Management` en consultas
operacionales y descargas documentales. El rol es fijo, fuera de los argumentos del
modelo. `/api/auth/me` se consulta sin ese header. El guard conserva temporalmente
`isManagement`, que Backend deriva de `roles` y verifica con el Google Subject;
acepta el contrato Auth V2 sin convertir los roles enviados por Browser en autoridad.
La cuenta puede tener varios roles, pero Intelligence opera exclusivamente en Gerencia.


## Live — estabilidad de turnos e interrupciones

El prompt distingue habla clara e intencional de ruido, eco, sonidos breves y audio propio.
Se conserva full-duplex y barge-in; el prompt es una mitigación, no una garantía acústica.
No se cambia modelo, configuración de secretos, grounding ni herramientas del Agent.

El sideband registra categorías `delegation_created`, `response.created`, `response.completed`,
`response.failed`, `response.incomplete`, `provider_error`, `provider_session_closed` y
`session_closed`, con request_id, turn_number, response_number, pending_calls y estado.
Los códigos de proveedor se limitan a una lista conocida; los desconocidos se clasifican como
`other_provider_error`. No se registran IDs del proveedor, mensajes de error crudos, preguntas,
transcripts, prompt, audio, SDP ni credenciales. Los números identifican delegaciones/respuestas
observadas, no turnos acústicos inferidos por RMS.

`response.failed` e `response.incomplete` cierran esta sesión de forma segura: se descartan calls
pendientes, se revoca la credencial, se cancela Agent en curso, se retira registry/binding Live y
se solicita `session.close`. El cierre remoto sigue siendo best-effort. No se reintenta el trabajo
ni se deja una delegación esperando. Conversation conserva sus turnos ya completados y permite
continuar por texto o una sesión Live nueva. Un trabajo cancelado no añade respuesta al historial.
El Frontend maneja los mismos eventos terminales y pérdida de conexión; no muestra Escuchando
para una sesión remota terminada.

### Smoke manual de audio (pendiente)

Con la misma configuración actual, ejecutar cada caso sin guardar audio, transcripts ni SDP:

1. **Audífonos:** hacer una consulta y dejar terminar; luego interrumpir intencionalmente.
   Esperado: respuesta completa y nueva pregunta atendida sin cortes espontáneos.
2. **Altavoces:** repetir al volumen normal. Esperado: la mitigación de eco evita que el audio
   propio corte la respuesta en condiciones normales.
3. **Ruido breve:** producir un sonido corto, sin habla clara, durante la respuesta.
   Esperado: no cancelar la respuesta por ese sonido.
4. **Interrupción real:** decir «espera, otra cosa» mientras habla, completar la pregunta y esperar.
   Esperado: se detiene, escucha y responde al nuevo turno.

Comprobar también mute manual, reactivación sin recrear sesión y recuperación de playback bloqueado.
Anotar solo resultado, navegador/dispositivo, audífonos/altavoces y categorías/timings seguros.
Si falla con altavoces pero pasa con audífonos, registrar esa diferencia: hardware, volumen y soporte
browser influyen en full-duplex. No introducir auto-mute sin esa evidencia. Los tests offline no
simulan acústica real ni prueban que desaparecieron todas las auto-interrupciones.

Referencia: [delegación GPT-Live](https://developers.openai.com/api/docs/guides/live-delegation).
La finalización del backend no significa que el usuario haya oído toda la respuesta; comprobar
ambas cosas en el smoke. Este cambio conserva esa separación.


## Recuperación de grounding en turnos Live

El catálogo de evidencia sigue siendo exclusivo de cada Agent.run. El historial identifica
referentes, pero no respalda referencias ni Presentation del turno actual. Una respuesta global
puede usar `reference_keys: []`; no se obliga a conservar el proyecto del turno anterior.

Si la validación final rechaza referencias o presentación, Agent Core realiza **un único repair**:

- añade la respuesta rechazada y una instrucción developer cerrada al contexto del mismo run;
- solicita una respuesta final con `tools=[]` y `tool_choice="none"`, manteniendo `store=false`;
- conserva el texto `answer` exactamente y exige `spokenAnswer == answer` antes del rendering;
- vuelve a validar responseType, presentation_keys, referencias y presentación completas;
- cuenta la llamada extra en `agent_model_calls`, dentro del timeout original y del presupuesto
  de transcript, sin nuevas rondas ni ejecuciones de herramientas.

No se filtran referencias silenciosamente. El replay del protocolo de tools permanece disponible
para ese repair dentro del mismo adapter; no pasa a ConversationContext ni a otro request.
Si el repair vuelve a fallar, no se reintenta. Los errores de tools anteriores a la respuesta
final no disparan un repair.

Live convierte únicamente los tipos exactos `UngroundedReference` e `InvalidModelResponse`
en una respuesta de servidor `unavailable`: «No pude verificar esa consulta en este momento.»
Comprueba que la sesión y su credencial sigan activas antes de recuperarla. Envía el resultado
por el `function_call_output` y la continuación existentes, manteniendo el sideband abierto.
Esta respuesta válida, sin referencias, puede entrar al historial conversacional; nunca se guarda
la respuesta parcial rechazada. Chat y voice básico conservan sus errores HTTP seguros.

Autenticación, permisos, subject, ownership, expiración/revocación, límites, `InvalidToolCall`,
fallos de conexión/proveedor y corrupción de protocolo siguen siendo fatales: se cierra Live
y se revoca la credencial. No cambian las comprobaciones de Gerencia antes de cada Agent.run.

La telemetría registra `grounding_repair_started`, `grounding_repair_succeeded`,
`grounding_repair_failed` y `turn_recovered`: contadores y categorías seguras, nunca IDs de
referencias, nombres, preguntas, respuestas ni credenciales. `turn_recovered` conserva la
correlación de request/turno del sideband.

### Modelos Live: responsabilidades distintas

| Variable | Responsabilidad |
| --- | --- |
| `SNG_INTELLIGENCE_LIVE_MODEL` | Modelo speech-to-speech de Live. |
| `SNG_INTELLIGENCE_LIVE_DELEGATION_MODEL` | Modelo de delegación configurado en el proveedor Live. |
| `SNG_INTELLIGENCE_LIVE_AGENT_MODEL` | Modelo Responses del Agent Core para la consulta empresarial; si está vacío, usa `SNG_INTELLIGENCE_MODEL`. |
| `SNG_INTELLIGENCE_LIVE_AGENT_REASONING_EFFORT` | Esfuerzo del Agent de Live (default `low`), según la compatibilidad validada del modelo. |
| `SNG_INTELLIGENCE_LIVE_AGENT_MAX_OUTPUT_TOKENS` | Límite de salida del Agent de Live (default 800, máximo 3000). |

Los overrides del Agent Live no alteran chat de texto. Este fix no cambia la selección de
modelos, variables de entorno, voz, micrófono, VAD ni turn_detection.

Las pruebas offline reproducen proyecto → seguimiento con consulta fresca → consulta global
con referencia histórica inválida → cuarta consulta. Cubren reparación a una referencia actual
o a `[]`, repair fallido, unavailable recuperable, continuidad de credenciales/registry/binding,
protocolo Responses, límites y errores fatales. No equivalen a un nuevo smoke acústico real.


## Dossier de cotización confirmada para Gerencia

Dos tools nuevas, en el mismo Agent Core de texto, voice básico y Live:

| Tool | Vista oficial read-only |
| --- | --- |
| `get_budget_quotation(project_id)` | `GET /api/projects/{id}/budget-quotation/view` |
| `get_commercial_quotation(project_id)` | `GET /api/projects/{id}/commercial-quotation/view` |

Requieren un UUID de proyecto resuelto sin ambigüedad en la evidencia del run actual.
El historial identifica «ese proyecto», pero cada turno vuelve a consultar datos oficiales.
La validación de todo el batch impide resolver un proyecto y usar su cotización como si ya
estuviera verificado dentro del mismo batch. Se conserva la autorización de Gerencia y
`X-SnG-Active-Role: Management`. El transporte permite únicamente estas dos rutas GET adicionales;
no permite imágenes, archivos de cotización, escrituras, redirecciones ni hosts arbitrarios.

### Ítems, cantidades y fuente

- `itemCount = len(items)`: renglones/ítems, nunca tareas de workflow.
- `totalQuantity = sum(quantity)`: unidades físicas de los valores confirmados. Por ejemplo,
  cantidades 2, 1 y 4 equivalen a **3 ítems y 7 unidades**; no a siete ítems.
- «Estructuras de vidrio» se expresa prudentemente como unidades cotizadas si no está probada
  la clasificación de todos los ítems. No se crean categorías adicionales.
- Preguntas técnicas generales consultan Commercial primero; solo `available=false` permite
  fallback a Budget. Preguntas explícitas de Presupuestos consultan Budget directamente.
- Precios/cambios de Comercial consultan Commercial directamente, sin leer Budget innecesariamente.
  `unitPrice`, `totalPrice` y `projectTotal` son cifras oficiales: no se estiman ni se recalculan.
  El total de una cotización no acredita facturación, margen o ventas realizadas.
- Los cambios provienen exclusivamente de `technicalDifferences` del Backend. En Commercial
  son Presupuestos → Comercial; en Budget son extracción → confirmación de Presupuestos.
  Intelligence no vuelve a comparar valores ni a calcular diferencias.

Las reglas de selección se proporcionan al modelo mediante el prompt y las descripciones de tools;
el servidor ejecuta solo el dispatcher cerrado y valida argumentos, autorización y evidencia.
Las pruebas scripted comprueban los flujos y contratos; no demuestran selección semántica perfecta
de un proveedor real. Un smoke conversacional real posterior debe verificar esas preguntas.

### Contratos y límites

`app/contracts/quotations.py` modela `CatalogItemValues`, `TechnicalDifference`,
`BudgetQuotationDocument`, `BudgetQuotationItem`, `ConfirmedBudgetQuotation`,
`CommercialQuotationItem` y `ConfirmedCommercialQuotation`. Contratos strict con campos extra
prohibidos, UUID/datetime, medidas/precios Decimal y cantidades enteras positivas. Los enums
cerrados reproducen `Main/Additional`, `Stored/Missing/Ambiguous` y los once TechnicalField
observados en el Backend. Una ampliación incompatible requiere actualizar el contrato.

La proyección incluye `source`, `available`, `confirmedAt`, los dos conteos derivados y todos
los ítems confirmados, con campos técnicos, diferencias y disponibilidad de imagen. Budget añade
su documento/versionNumber; Commercial, precios y projectTotal. Omite IDs de cotización/catálogo/
ítems, valores extracted, storage metadata y bytes. Los Decimal se serializan como strings
exactos para conservar precisión. Campos vacíos permanecen vacíos: no especificado, no inferido.

404 de estas vistas devuelve `available=false`, sin cantidades cero inventadas; no cierra Live.
No distingue por sí solo proyecto eliminado de snapshot ausente, por eso la tool exige evidencia
previa fresca del proyecto y comunica indisponibilidad, no una afirmación de inexistencia.
Si todas las cotizaciones consultadas están ausentes, la validación final exige unavailable o
clarification: rechaza un fallback factual basado en tareas. 401/403, payload inválido, timeouts
y errores de infraestructura conservan su tratamiento seguro; no se convierten en ausencia.

Se conserva `MAX_RESULT_BYTES=48000` y el límite de transcript. Una proyección completa que
exceda el límite falla con `tool_result_too_large`: no se trunca, pagina ni selecciona una muestra
silenciosamente. Pruebas con 13 ítems/16 unidades comprueban el envío completo dentro del límite;
pruebas sobredimensionadas comprueban el rechazo. No se midieron datos empresariales reales.

### Unidades, moneda y presentación

Se revisaron `QuotationReadModels.cs`, `CatalogItemValues.cs`, los converters JSON,
`EfQuotationReadStore.cs` y los tests del parser/API del Backend, sin modificarlos.
`CachedFormula_IsReadWithoutCalculationAndM2IsNotReconstructed` confirma que squareMeters
conserva el valor del archivo y no se reconstruye a partir de ancho/alto. Los contratos revisados
no prueban una regla universal de agregación con quantity. Esta fase muestra m² oficiales por
ítem; no agrega `totalSquareMeters`. La unidad de width/height sigue sin estar declarada. Money Contract V1 incorpora moneda
COP oficial en Commercial, como se describe abajo. No se reparsea XLSX ni se consulta DB.

Se reutiliza `direct` con `presentation_keys=[]` y referencias verificadas al proyecto.
Los ítems de cotización nunca se representan como TaskItem; no hay nuevas rutas ni contratos
públicos de navegación. El nombre oficial del proyecto sigue siendo la identidad principal.
El repair único de grounding se conserva; los IDs internos no se vuelven referencias válidas.
No cambian Frontend, Backend, audio, VAD, modelos Live ni el ciclo de credenciales.


## Long Context V1 — memoria gerencial durable

Hay tres capas separadas: transcript completo durable, memoria estructurada durable y turnos
recientes completos. La compactación nunca borra ni modifica `turns`. No hay memoria entre
conversaciones: un hilo nuevo empieza sin memoria. Cerrar archiva; reiniciar no pierde la memoria.

`ConversationMemoryDraft` contiene `summary` (hasta 1800 caracteres), `topics`, `userDecisions`,
`userAssumptions`, `openThreads` y `priorProposals` (hasta 6 textos de 240 caracteres por categoría).
Los contratos son estrictos, sin campos adicionales ni inputs en errores. La prosa serializada
se limita a 3000 caracteres/3200 bytes UTF-8; la memoria final a 5000 caracteres/4800 bytes.

El compactor conserva decisiones explícitas, distingue propuestas del asistente y escenarios
hipotéticos, e incorpora revocaciones y temas resueltos. No infiere aceptación ni crea campos de
estado, precios, plazos o cotización actuales. Estas reglas semánticas guían al modelo; las pruebas
con respuestas programadas verifican el protocolo, no demuestran calidad semántica del proveedor.

`historicalReferents` se agrega en el servidor desde las referencias verificadas de los turnos
compactados y la memoria anterior. Se deduplica por tipo/IDs, ordena por última aparición y retiene
hasta 16. Si los labels agotan el presupuesto se eliminan referencias antiguas completas, nunca
se cortan labels o IDs. El compactor no puede devolver IDs ni este campo.

**Memoria conversacional no es verdad empresarial.** Entra como datos `assistant`, después de
una regla `developer` estática. No siembra evidencia ni autoriza directamente las tools de
cotización. Incluso con memoria y sin turnos recientes, una respuesta operacional exige lecturas
frescas. Las correcciones recientes prevalecen sobre memoria antigua. No se amplían las categorías
de respuesta ni se introduce recall libre sin provenance, RAG, embeddings o búsqueda semántica.

### Compactación y límites

Antes de construir el siguiente contexto (chat, delegación o apertura de Live), si hay más de
16 turnos posteriores a `compactedThrough`, se compacta un solo lote de hasta 8 turnos antiguos,
intentando dejar al menos 12 recientes. El input incluye memoria previa, ese lote completo y
referentes permitidos; máximo 48000 caracteres/144000 bytes. Nunca se corta un turno. Un turno
individual que exceda el límite deja la memoria sin avanzar y activa fallback explícito.

No hay workers en background. Un historial antiguo de 100 turnos se incorpora gradualmente,
un lote por operación; puede haber un gap temporal entre memoria y ventana reciente. El contexto
incluye una señal de cobertura incompleta, también cuando el límite de tamaño descarta turnos.
El transcript íntegro permanece en PostgreSQL; no se afirma que esa proyección sea exhaustiva.

Se leen memoria/lote y se liberan conexiones antes del proveedor. Después se guarda en una
transacción corta con CAS de `revision` y avance monotónico de `compacted_through`. Un resultado
tardío no sobrescribe memoria nueva. Timeout (20 s del proveedor/25 s de compactación), 429,
indisponibilidad, salida inválida/sensible o conflicto CAS conservan la memoria anterior y dejan
continuar con contexto reciente acotado. Cancelación y fallos de ownership no se ocultan.
Los locks de ejecución siguen siendo locales: se mantiene el despliegue con un worker.

### Provider y Live

`SNG_INTELLIGENCE_MEMORY_MODEL` es opcional; vacío usa `SNG_INTELLIGENCE_MODEL` y la misma
`OPENAI_API_KEY`. Configuración lazy, sin llamadas en startup ni en resume por sí solo.
El adapter usa el SDK oficial, Responses y Structured Outputs estrictos, `store=false`,
sin tools, web, clientes operacionales, `reasoning`, `previous_response_id` ni Conversations.
No recibe autenticación Google ni contexto de infraestructura. Input y output pasan guards.
Ver [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

Live usa el mismo `ConversationContext` y la misma memoria, con input de contenido limitado a
6000 bytes UTF-8; descarta turnos recientes completos si hace falta, no crea otra memoria.
Se conserva reautorización antes de cada Agent.run, grounding local, reparación acotada y
lifecycle de credenciales/sideband. La telemetría agrega solo categorías, cantidades y tiempos:
`conversation_memory_compaction_started/succeeded/failed`, `conversation_memory_compaction_ms`
y `conversation_memory_model_calls`, separados de las rondas del Agent. No registra contenido,
subjects, IDs de conversación, referentes o credenciales.

### Actualización y verificación

`001_conversations.sql` permanece byte-identical. `002` agrega `conversation_memory` con PK/FK
al hilo, `compacted_through`, `revision`, `payload` JSON object y `updated_at`, sin duplicar subject.
El ownership continúa en `conversations`. Las conversaciones existentes no necesitan una fila
de memoria hasta su primera compactación. No se modifica timezone ni historial existente.

Con las variables configuradas, antes de iniciar la versión nueva ejecutar:

```powershell
python -m app.conversation.persistence.migrate
```

Las pruebas normales son offline. Cubren 100 turnos, lotes y límites, fallos, CAS, semántica
programada, aislamiento, contratos reales del SDK mockeados, grounding, 35 turnos y nueva sesión
Live con cotización fresca. El smoke opcional siguiente crea su propio clúster PostgreSQL temporal
con datos sintéticos, prueba v1→v2, repetición, CAS y reinicio DB/pool/runtime, y lo elimina al terminar.
Nunca usa `SNG_INTELLIGENCE_DATABASE_URL` ni la base real del usuario:

```powershell
$env:SNG_TEST_POSTGRES_BIN = 'C:\Program Files\PostgreSQL\17\bin'
pytest tests/test_memory_postgres.py
Remove-Item Env:SNG_TEST_POSTGRES_BIN
```

Sin esa variable, este único smoke se omite. La migración de la base real se ejecuta manualmente
después de publicar; los tests no la migran ni llaman OpenAI real.


## Money Contract V1 — COP oficial y voz

La cotización comercial exige `currencyCode: "COP"` recibido de Backend. Se rechazan moneda
ausente/desconocida e importes negativos, fuera de numeric(20,2) o con más de dos decimales
significativos. No hay inferencia de moneda, conversión FX ni redondeo económico en Intelligence.
La proyección conserva Decimal→string exacto para projectTotal, unitPrice y totalPrice;
Budget y las medidas técnicas mantienen sus contratos.

El prompt solicita en español `$5.513.429,14 COP`. El texto conserva ese formato y el renderer
produce determinísticamente «cinco millones quinientos trece mil cuatrocientos veintinueve pesos
con catorce centavos». Reconoce solamente el patrón explícito COP colombiano; omite centavos
cuando son cero, usa singular cuando corresponde y cubre hasta 999999999999999999.99.
No transforma decimales sueltos, medidas, cantidades ni nombres protegidos. La pronunciación
existente de códigos SG permanece independiente. En inglés se conserva `COP 5,513,429.14`,
sin conversión especializada a palabras. No se añade dependencia para números a palabras.

`spokenAnswer` sigue derivando del mismo `answer`; el modelo no genera otra cifra para voz.
Texto, Basic Voice y Live usan el mismo Agent/renderer. No cambian modelos Live, VAD, sideband,
delegación, grounding ni Long Context V1; no hay migración de la base de Intelligence.

«Ítem más caro» usa totalPrice dentro de una cotización; precio unitario explícito usa unitPrice.
El valor de la cotización usa exclusivamente projectTotal, nunca una suma local. Queda fuera
el histórico completo de versiones. Las consultas globales actuales usan get_quotation_portfolio,
nunca un recorrido individual de proyectos. Las pruebas usan datos sintéticos equivalentes al
hallazgo, incluidas cifras grandes, límites, centavos, contratos inválidos y referencias frescas.

Desplegar junto con Backend Money Contract V1 y su migración monetaria. Los parsers cerrados
operacionales de Frontend también requieren el nuevo campo; se realizó su ajuste contractual
mínimo para evitar 502 por campos adicionales. No se cambió su UI ni sus cálculos.


## Decision Support V1

El mismo Agent permite análisis, prioridades, recomendaciones, alternativas y escenarios cuando
Gerencia los solicita. `answerMode` es un campo obligatorio y cerrado de la salida interna del
modelo: `factual`, `analysis`, `recommendation`, `scenario`, `brainstorm` o `conversation`.
No se expone en ChatResponse, Presentation, historial, memoria ni protocolo Live.

El servidor exige al menos una lectura exitosa del run para analysis/recommendation/scenario,
incluso con memoria antigua. Para factual aplica la misma regla salvo clarification/unavailable.
Brainstorm genérico y conversación no operacional pueden responder sin tools, también después de
otros turnos, únicamente con direct/clarification/unavailable y sin claves de evidencia.
Ideas sobre la operación actual requieren lecturas; el prompt prohíbe eludir esa regla cambiando
el modo. Esta validación es un mínimo estructural: no demuestra por sí sola que cada frase sea
una consecuencia de la evidencia ni detecta toda clasificación semántica incorrecta del modelo.
No hay router por palabras clave ni clasificador adicional.

Las respuestas distinguen hechos oficiales, interpretación («Mi lectura es...»), recomendaciones
(«Yo priorizaría...») y supuestos («Bajo ese supuesto...»). Snapshot sirve para atención global,
respetando coverage; list_management_tasks para exhaustividad; compare_projects para 2–4 proyectos
resueltos en el run. No se consultan documentos/cotizaciones sin necesidad ni se barre el portfolio.
Los conteos no prueban productividad ni cuellos de botella. No hay riskScore, probabilidades,
márgenes inventados ni dependencias asumidas; un retraso hipotético no recalcula fechas oficiales.

Long Context aporta objetivos explícitos, decisiones, propuestas y supuestos históricos, nunca
estado operacional actual. Las correcciones recientes prevalecen y una recomendación puede cambiar
tras nuevas lecturas. Recall histórico sin provenance sigue limitado a clarification/unavailable.
Las referencias y Presentation siguen naciendo de evidencia del run; grounding repair no puede
cambiar answer ni answerMode. Texto, Basic Voice y Live comparten Agent y spokenAnswer derivado.
Live conserva lifecycle y credenciales; recomendaciones simples normalmente ocupan 2–4 frases.

Sigue siendo read-only: no ejecuta recomendaciones, no genera alertas autónomas ni modifica Backend.
No hay cambios de DB, migraciones, HTTP público, tools ni Frontend. Las pruebas son offline y
scripted: cubren contratos, rechazo sin lecturas, comparación, revisión con memoria y tres turnos
Live con nuevas lecturas; no certifican calidad de razonamiento de OpenAI real ni un smoke pagado.

### Decision Support V1.1 — escenarios operacionales

Scenario reasoning is operational-first. Documents are not a generic uncertainty fallback.
Preguntar «¿qué pasa si el cliente se demora más días de lo que debería?» requiere primero
resolver el proyecto y leer su estado/tareas actuales. «Debería», plazo, demora o tiempo no
implican automáticamente un SLA contractual ni una búsqueda documental. Se consulta únicamente
la evidencia suficiente; cotización, actividad y snapshot no se agregan por defecto.

El Agent separa hechos actuales, supuesto, posibles efectos y límites de cuantificación. La falta
de dependencias formales no impide analizar cualitativamente una posible prolongación de la
etapa comercial si los datos actuales sostienen esa premisa. No permite afirmar Producción +3
o Logística +3, inventar etapas ni recalcular fechas. Los supuestos de memoria siguen siendo
hipótesis y requieren fresh reads; las referencias siguen siendo del run actual.

«¿Qué plazo establece el contrato?» o «¿Hay penalización escrita?» sí justifican herramientas
documentales. Tanto get_project_documents como analyze_project_documents exigen una necesidad
documental; no resuelven toda incertidumbre. Un documento grande/no disponible no bloquea un
escenario que no necesita consultarlo. Las preguntas que sí dependen del documento conservan
los límites y errores del flujo 4B, sin inventar su contenido.

El hardening está en prompt y tool descriptions, sin router por palabras clave. Se mantienen
answerMode=scenario interno, guard de lectura actual, responseTypes y lifecycle Live. Tests
scripted cubren escenarios sin documentos, consulta contractual con documentos, memoria obsoleta,
límites cualitativos y tres turnos Live con nuevas lecturas. No prueban la selección semántica
del modelo real: el caso de smoke debe repetirse manualmente en texto y Live.


## Live Evidence Gate + Quotation Source Routing V1.2

GPT-Live es una superficie conversacional, no una fuente de verdad empresarial. La política
explícita `Generate <|SpawnThinking|>` solicita delegación antes de cada nueva respuesta sobre
negocio, incluyendo seguimientos, precios, recomendaciones y escenarios. Saludos, agradecimientos
y aclaraciones breves pueden continuar sin delegación. GPT-Live todavía decide si delega: el
prompt no es un router determinista ni garantiza por sí solo la selección del proveedor.

Una vez delega, `delegation.type=responses` y `delegation.responses.tool_choice=required`
obligan al backend delegado a usar la única función `consult_sng_intelligence`. Esa función
pasa por Agent Core y sus guardas de lecturas frescas. El resultado spokenAnswer es autoridad;
no se permite completar números o conclusiones con historial. La configuración sigue
[Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation) y la
[política de prompting GPT-Live](https://developers.openai.com/api/docs/guides/live-prompting).

El inicio Live tiene una proyección separada: CONTEXT_RULE, memoria estructurada, señal de cobertura
incompleta, preguntas recientes y referentes verificados ordenados. Omite la prosa de respuestas
anteriores del asistente. Conserva el presupuesto de 6000 bytes de contenido y elimina turnos
completos antiguos para ajustarlo. La memoria y el texto del usuario siguen siendo contexto
histórico no confiable, no evidencia actual. `ConversationContext.messages()` no cambia:
Agent Core conserva el historial reciente completo y aplica las lecturas frescas existentes.
No se reescribe ni elimina historial durable.

Para inspeccionar o recomendar énfasis sobre ítems, sistemas, vidrio, acabado, dimensiones,
cantidades, m² por ítem, precios, totales, diferencias técnicas o COP, la fuente primaria es
`get_commercial_quotation` tras resolver el proyecto. Llamar «archivo» o «documento» a esa
cotización no requiere descargar XLSX. Condiciones de pago escritas, cláusulas de garantía y
texto fuera de la vista estructurada siguen usando metadata y análisis documental con sus
límites actuales. La selección sigue en el modelo; no hay matching de frases ni nuevas tools.

Pruebas offline comprueban la proyección, configuración exacta, cotización vs. texto documental
y un flujo Live scripted con precio histórico obsoleto y precio COP actual. No prueban selección
semántica del proveedor real. No se ejecuta smoke pagado. Money Contract, Scenario V1.1,
credenciales, ownership, grounding repair, DB y presupuestos del Agent permanecen intactos.


## Quotation Portfolio Analytics V1

`get_quotation_portfolio()` consulta una sola vez `GET /api/management/quotation-portfolio`, con
Management validado. Backend agrega las cotizaciones comerciales confirmadas actualmente
almacenadas; no representa ventas, ingresos, facturación, márgenes ni un archivo completo de
versiones históricas. No incluye series temporales ni mezcla por sistema/vidrio.

El contrato cerrado exige COP y Price decimal numeric(20,2), conteos no negativos y rankings
máximo 10 con total/returned/hasMore consistentes. quotationCount cuenta cotizaciones;
itemCount renglones; totalQuantity unidades; totalQuotedValue suma los ProjectTotal oficiales,
no los ítems del top. Intelligence conserva importes como strings Decimal y no suma ni ordena.
El primer highestValueItems corresponde al mayor TotalPrice, highestUnitPriceItems al mayor
UnitPrice y highestValueProjects al mayor ProjectTotal. El top permite conocer el máximo global,
pero no afirmar que se muestran todos si hasMore=true. Vacío es un resultado válido sin ganador.

Consultas globales y seguimientos «¿y de todos los proyectos?» usan esta tool; preguntas sobre
un proyecto concreto siguen usando get_commercial_quotation. No se simula portfolio con N+1.
Referencias de proyecto se construyen y deduplican desde rankings frescos; no hay referencias
de ítem ni nuevos responseTypes. V1 utiliza direct y presentation_keys=[] incluso para listados.
Recomendaciones por valor declaran ese criterio; mayor valor no implica mayor riesgo ni margen.
Long Context no sustituye una lectura nueva. Live usa el mismo Agent sin cambios de protocolo,
SpawnThinking, required, proyección de inicio ni credenciales. Money rendering se reutiliza.

Desplegar junto con el endpoint Backend correspondiente. No cambia Frontend ni hay migración.
Pruebas offline de contratos, cobertura, autorización, lectura única, referencias y seguimiento
Live son scripted; no equivalen a smoke pagado o prueba semántica del modelo real.
