# SnG Intelligence — Intelligence 1A

Capa read-only de Gerencia. Este bloque construye infraestructura, autenticación,
contratos y herramientas internas de lectura. **SnG-Control-Backend es la única
fuente de verdad operacional.**

```text
Frontend -> Intelligence (FastAPI) -> SnG Control (ASP.NET Core)
```

No hay acceso a DB, copia persistida de Project/ProjectTask, cálculo de Leak Time,
escrituras, LLM, chatbot, voz, RAG, embeddings ni conversaciones persistidas.

## Estructura

```text
app/
  main.py                    # Aplicación, lifespan y dos endpoints
  api/dependencies.py        # Credenciales y cliente con alcance de request
  auth/management_guard.py   # Bearer y validación oficial de Gerencia
  clients/sng_control.py     # Siete operaciones GET cerradas
  clients/transport.py       # Allowlist de método, origen y rutas; sin cookies
  contracts/                # DTO Pydantic del contrato HTTP, sin lógica de negocio
  core/                     # Configuración, errores seguros y correlación
  tools/read_tools.py        # Cinco herramientas internas
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

No existen endpoints de tools, proxy, chat, documentación interactiva ni OpenAPI
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
| `get_project(project_id)` | `/api/projects/{id}` |
| `list_project_tasks(project_id)` | `/api/projects/{id}/tasks` |
| `list_project_documents(project_id)` | `/api/projects/{id}/documents` |
| `list_project_notifications(project_id)` | `/api/projects/{id}/notifications` |

Las cinco tools internas de `ReadTools` son:

1. `get_management_overview()` — overview oficial.
2. `list_management_tasks()` — lista transversal, en el orden original.
3. `get_project_details(project_id)` — proyecto y tareas en paralelo; cancela y
   espera la llamada hermana si ocurre un error o cancelación.
4. `get_project_documents(project_id)` — metadata, initialHandoff y flags oficiales.
5. `get_project_activity(project_id)` — notificaciones oficiales, sin interpretación.

Los flags documentales como `canModify` son información recibida; no habilitan
acciones en Intelligence. No se consumen `/file`, `current.zip` ni `initial-handoff.zip`.

Para una futura integración FastAPI, `get_read_tools` provee la instancia mediante
`Depends`, sin pedir token, email o subject como argumentos de tool. Las tools
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
- `ReadOnlyTransport` impone GET, origen configurado, siete patrones de ruta y
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
automáticos ni caché de datos operacionales.

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

**Para Intelligence 1B:** integración explícita del LLM con argumentos estructurados
y estas tools; proyección mínima de datos para el modelo; integración y smoke del
frontend con Google ID Token real. Ninguna credencial debe llegar al modelo.

Chat, voz, streaming, conversaciones persistidas y contenido documental quedan
fuera de 1A y requieren alcance posterior. RAG solo se evaluará con un caso de uso
explícito de contenido no estructurado. No se necesita para replicar datos de APIs.
