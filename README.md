# SnG Intelligence — Intelligence 1B

Capa conversacional read-only de Gerencia. Agent Core utiliza herramientas de
lectura y un proveedor LLM para responder preguntas. **SnG-Control-Backend es la única
fuente de verdad operacional.**

```text
Gerencia -> Intelligence API -> Agent Core -> ReadTools -> SnG Control
                                    |
                                    +-> AgentModel -> OpenAI
```

No hay acceso a DB, copia persistida de Project/ProjectTask, cálculo de Leak Time,
escrituras, voz, RAG, embeddings ni conversaciones persistidas.

## Estructura

```text
app/
  main.py                    # Aplicación, lifespan, health, session y chat
  agent/                     # Loop, contratos, catálogo/evidencia, prompt y adaptador
  api/dependencies.py        # Credenciales y cliente con alcance de request
  auth/management_guard.py   # Bearer y validación oficial de Gerencia
  clients/sng_control.py     # Ocho operaciones GET cerradas
  clients/transport.py       # Allowlist de método, origen y rutas; sin cookies
  contracts/                # DTO Pydantic del contrato HTTP, sin lógica de negocio
  core/                     # Configuración, errores seguros y correlación
  tools/read_tools.py        # Seis herramientas internas, incluida find_projects
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
| `OPENAI_API_KEY` | Credencial del proveedor, requerida solo para chat. No se valida por red en startup. |
| `SNG_INTELLIGENCE_MODEL` | Modelo obligatorio para chat, sin valor predeterminado. Debe soportar function calling y salida JSON Schema estricta en Responses API. |

`.env.example` contiene únicamente un ejemplo local, sin secretos. Uvicorn carga
`.env` solo al usar `--env-file`; el código consume variables de entorno y valida
durante startup. La importación del módulo no conecta al backend. Una configuración
inválida impide el arranque y produce un mensaje que nombra las variables, sin
imprimir sus valores.

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
| `POST /api/intelligence/chat` | Valida Gerencia, ejecuta una consulta independiente y devuelve `answer` y `references`. |

No existen endpoints de tools, proxy, documentación interactiva ni OpenAPI
público. CORS no está habilitado. Todas las respuestas incluyen `Cache-Control:
no-store` y `X-Request-ID`. La integración posterior decidirá BFF o CORS explícito.

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
resultado. `clientName` es null si no está disponible. Se conserva el orden del backend.
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

La respuesta es `{"answer": "...", "references": [...]}`. Las variantes exactas son:

```text
ProjectReference = {type: "project", projectId: UUID, label: string, sgCode: string | null}
TaskReference    = {type: "task", taskId: UUID, projectId: UUID, label: string}
```

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

La presentación para Gerencia se configura en `app/agent/prompt.py`: español natural,
glosario centralizado de tareas/estados y fechas legibles en `America/Bogota` cuando
el dato incluye zona u offset. Los nombres propios se conservan. Las fechas sin zona
no se asumen UTC y los valores null no se completan. Esta instrucción de generación
no altera los DTO, tool results, deadlines ni referencias, ni aplica reemplazos al
texto final. El contexto aclara que preparar cotización pendiente aún no tiene Leak
Time activo y que Presupuestos lo inicia con la primera asignación del SG; no calcula
atrasos ni sustituye los estados oficiales del backend.

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

## Smoke real del proveedor

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
«¿Qué debería preocuparme hoy?» y una pregunta por un nombre/SG obtenido de datos
reales. Revisar respuestas, tools y referencias sin registrar credenciales.
Al terminar: `Remove-Item Env:OPENAI_API_KEY, Env:SNG_INTELLIGENCE_MODEL,
Env:SNG_SMOKE_GOOGLE_TOKEN -ErrorAction SilentlyContinue`.

## Verificación

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\ruff.exe format --check app tests
.\.venv\Scripts\mypy.exe app
```

La suite no usa SnG Control real. Cubre configuración/startup, auth estricta,
forwarding y rutas exactas, DTO y null, estados nuevos, orden, UUID, metadata,
errores, concurrencia, cleanup, correlación, aislamiento de tokens/cookies,
ausencia de proxy y rechazo efectivo de escrituras, descargas y otros orígenes.
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

## Estado y deuda intencional

**Intelligence 1A:** infraestructura, auth, cliente, contratos, read tools y tests.

**Intelligence 1B implementado:** endpoint chat, AgentModel/OpenAIModel, loop acotado,
seis tools, búsqueda de proyectos y referencias ligadas a evidencia del request.
La verificación con proveedor real depende de disponer de API key, modelo y sesión
Google vigentes; las pruebas automatizadas no sustituyen ese smoke.

**1C:** integración conversacional con el frontend. **1D:** voz sobre el mismo agente.
No se implementaron streaming, memoria, persistencia ni análisis de bytes documentales.
RAG continúa fuera de alcance sin caso de uso explícito de contenido no estructurado.

Referencias OpenAI: [function calling](https://developers.openai.com/api/docs/guides/function-calling)
y [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
