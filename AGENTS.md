# AGENTS.md — SnG Intelligence

## Purpose

`SnG_Intelligence` is the AI and intelligent-assistance layer for Steel & Glass management.

Its purpose is to help Management understand the current operational state of SnG Control through natural-language interaction and, later, voice.

This repository is **not** the operational backend of Steel & Glass.

`SnG-Control-Backend` remains the authoritative system of record and the source of truth for business state.

---

# Core architectural rule

## SnG Control is the source of truth

All operational information must come from the official SnG Control API.

SnG Intelligence MUST NOT independently determine or persist business truth such as:

- project state;
- task state;
- SG assignment;
- Leak Time;
- deadlines;
- overdue status;
- area ownership;
- document permissions;
- quotation state;
- workflow transitions;
- user permissions.

If SnG-Control-Backend already calculates or exposes a value, Intelligence must consume that value instead of calculating another version of it.

Examples:

Bad:

```python
if task.deadline_at < datetime.now():
    status = "Overdue"
```

Correct:

```python
status = task.leak_time_status
```

because SnG-Control-Backend already owns Leak Time logic.

---

# Repository boundaries

This repository may contain:

- FastAPI application;
- authentication guard;
- SnG Control HTTP client;
- read-only tools;
- LLM orchestration;
- agent logic;
- conversation orchestration;
- response composition;
- structured references to projects/tasks;
- voice-related integration in future blocks;
- tests for Intelligence behavior.

This repository MUST NOT become:

- a replacement for SnG-Control-Backend;
- a second business backend;
- a database replica;
- a workflow engine;
- a quotation engine;
- a permission authority.

Do not modify other repositories unless explicitly instructed.

Repositories outside this scope include:

```text
SnG-Control-Backend
SnG-Control-Frontend
```

---

# Technology

Primary stack:

```text
Python
FastAPI
Pydantic
httpx
pytest
```

Future agent orchestration may use LangGraph when explicitly introduced.

Do not introduce large dependencies without a concrete requirement.

Avoid dependency proliferation.

---

# Database access

## Direct access to the SnG Control database is forbidden

SnG Intelligence MUST NOT:

- connect directly to PostgreSQL used by SnG Control;
- query operational tables;
- use SQLAlchemy against the SnG Control database;
- execute SQL against SnG Control;
- duplicate SnG Control entities for synchronization;
- maintain a shadow operational database.

Communication with SnG Control must occur through its HTTP API.

Conceptually:

```text
Frontend
   |
   v
SnG Intelligence
   |
   v
SnG-Control-Backend
   |
   v
PostgreSQL
```

Never:

```text
SnG Intelligence
   |
   +------> SnG Control PostgreSQL
```

---

# Read-only principle

SnG Intelligence is currently a **read-only Management assistant**.

Its tools must only expose approved read operations.

Allowed examples include:

```text
GET /api/auth/me
GET /api/management/overview
GET /api/management/tasks
GET /api/projects/{projectId}
GET /api/projects/{projectId}/tasks
GET /api/projects/{projectId}/documents
GET /api/projects/{projectId}/notifications
```

Do not implement operational write tools unless the product requirements explicitly change in the future.

Forbidden examples include:

```text
POST /api/projects
POST /api/projects/{id}/send-to-budgets
PUT /api/projects/{id}/sg-code
POST document uploads
DELETE documents
quotation confirmation
workflow transitions
```

Do not expose generic public methods such as:

```python
client.post(...)
client.put(...)
client.delete(...)
```

for agent usage.

The future LLM must not gain arbitrary write capability accidentally.

Read-only behavior must be enforced architecturally, not merely through prompts.

---

# No generic HTTP tool

Never create an agent tool such as:

```text
request_url
http_request
fetch_any_url
call_backend
proxy_request
```

where the model can supply an arbitrary:

- URL;
- path;
- HTTP method;
- host;
- query.

Agent tools must be explicit and narrowly scoped.

Good examples:

```text
get_management_overview
list_management_tasks
get_project_details
get_project_documents
get_project_activity
```

Bad example:

```text
request(method, url, body)
```

---

# Authentication

The frontend sends the user's Google ID Token as:

```http
Authorization: Bearer <token>
```

SnG Intelligence must not trust user identity fields supplied by the frontend.

Do not authorize using:

- email;
- display name;
- frontend-provided Subject;
- frontend-provided role;
- frontend-provided isManagement.

The official identity check is performed against:

```http
GET /api/auth/me
```

on SnG-Control-Backend using the same Bearer token.

Management access requires:

```text
isManagement == true
```

The backend is the authority.

Do not hardcode:

- Google Subjects;
- emails;
- domains;
- user names.

---

# Credential handling

Google ID Tokens are sensitive credentials.

They MUST NOT be:

- persisted;
- written to disk;
- stored in a database;
- stored in conversation history;
- cached unnecessarily;
- included in logs;
- returned in API responses;
- included in exception messages;
- exposed to the LLM;
- included in prompts;
- included in tracing payloads.

The token should live only for the duration necessary to service the current request.

Authorization headers must never be logged.

---

# LLM isolation

When LLM support is introduced, the model must not receive:

- Google ID Tokens;
- Authorization headers;
- infrastructure secrets;
- database credentials;
- internal API credentials;
- arbitrary environment variables.

The model receives only the minimum business context required to answer the user's question.

Tools execute outside the model.

The model may request a tool invocation using structured arguments, but it must never construct arbitrary backend requests.

---

# Business logic

Do not duplicate business rules owned by SnG-Control-Backend.

Examples of logic that MUST remain authoritative in backend:

- task status;
- Leak Time;
- overdue;
- DueSoon;
- DueToday;
- CompletedOnTime;
- CompletedLate;
- SG validation;
- task transitions;
- area assignments;
- document capabilities;
- workflow permissions;
- quotation calculations.

Intelligence may:

- filter;
- group;
- summarize;
- compare;
- rank based on authoritative fields;
- explain authoritative values.

It must not redefine them.

---

# Hallucination policy

The agent must distinguish clearly between:

1. facts returned by SnG Control;
2. deterministic calculations based on those facts;
3. interpretation or recommendation.

Never invent:

- projects;
- SG codes;
- clients;
- tasks;
- dates;
- deadlines;
- documents;
- financial values;
- workflow states.

If information is unavailable, say that it is unavailable.

Do not infer missing operational facts as certain.

---

# Structured tools

Tool arguments must be explicit and validated.

For example:

```python
get_project_details(project_id: UUID)
```

not:

```python
get_project_details(query: str)
```

when a project UUID is already known.

Project identifiers must be validated as UUIDs.

Do not allow an ID argument to become:

- URL;
- path;
- querystring;
- filesystem path.

---

# Structured references

When the conversational agent is implemented, answers should support structured references when useful.

Examples:

```json
{
  "type": "project",
  "projectId": "...",
  "label": "Casa Pereira",
  "sgCode": "SG0042"
}
```

or:

```json
{
  "type": "task",
  "taskId": "...",
  "projectId": "...",
  "label": "Preparar cotización"
}
```

These references should allow the frontend to create navigable links without parsing arbitrary assistant prose.

Do not encode navigation behavior inside natural-language text alone when a structured reference is appropriate.

---

# RAG

Do not introduce RAG by default.

Current operational information is structured and available from SnG-Control-Backend.

Do not create embeddings for:

- projects;
- tasks;
- Leak Time;
- SG;
- management overview;
- structured workflow state

simply to answer operational questions.

This would create duplicate and potentially stale representations of authoritative data.

RAG may be introduced later only for a concrete unstructured-information requirement, for example selected business documents whose content cannot be queried structurally.

Any future RAG design must define:

- authoritative source;
- indexing lifecycle;
- document versioning;
- access control;
- deletion/update behavior;
- citation strategy.

---

# Documents

Metadata may be consumed through the SnG Control documents API.

Do not download or interpret document bytes unless explicitly required by the current implementation block.

Do not assume document content from:

- file name;
- MIME type;
- project name.

If future Intelligence blocks analyze documents, access must still respect SnG Control permissions.

---

# HTTP client

Use `httpx` asynchronously.

Requirements:

- explicit timeout;
- reusable client lifecycle where appropriate;
- no arbitrary external hosts;
- no unsafe redirect behavior that could forward Authorization elsewhere;
- safe error translation;
- cancellation-friendly async I/O.

`SNG_CONTROL_BASE_URL` must come from trusted server configuration.

The user or model must never be able to replace the backend hostname dynamically.

---

# SSRF protection

All target routes must be generated internally.

Never accept a complete URL from:

- user input;
- LLM tool arguments;
- query parameters;
- request body.

Do not implement a generic HTTP proxy.

---

# Error handling

External infrastructure errors must be translated into safe API errors.

Conceptual mapping:

```text
SnG Control 401 -> Intelligence 401
SnG Control 403 -> Intelligence 403
SnG Control 404 -> appropriate 404
SnG Control timeout -> safe 503/504
SnG Control 5xx -> safe 502/503
```

Never return raw httpx exceptions directly.

Never expose:

- stack traces;
- Authorization headers;
- internal URLs containing secrets;
- environment variables;
- raw infrastructure responses containing sensitive data.

---

# Logging

Logging must be structured and minimal.

Allowed examples:

- request/correlation ID;
- operation name;
- latency;
- status code;
- safe error category.

Never log:

- Google tokens;
- Authorization headers;
- secrets;
- complete sensitive payloads.

Avoid HTTP debug modes that automatically dump request headers.

---

# Health endpoint

`GET /health` should indicate only that the Intelligence application process is alive.

It should not require authentication.

It should not pretend that SnG Control, the LLM provider, or every dependency is healthy unless a dedicated readiness check explicitly verifies those dependencies.

---

# Async behavior

Network I/O should be asynchronous.

Do not block the FastAPI event loop with synchronous HTTP calls or expensive synchronous operations.

When multiple independent read calls are required, concurrency may be used where safe and useful.

Avoid duplicated backend requests.

---

# Conversation state

Do not persist conversation history unless the current development block explicitly introduces conversation persistence.

Do not introduce Redis, PostgreSQL, SQLite, or another state store merely because a chat UI may eventually need history.

Conversation architecture must be decided deliberately.

---

# Voice

Voice is an interface to the same Intelligence agent.

Do not build a separate voice business logic path.

Future architecture should remain conceptually:

```text
Text --------\
              > Intelligence Agent -> Tools -> SnG Control
Voice -------/
```

not:

```text
Text Agent
Voice Agent
```

with duplicated logic.

---

# Frontend independence

SnG Intelligence must expose clear API contracts and should not depend on React/Next.js internals.

Do not make backend behavior dependent on:

- frontend component names;
- DOM state;
- browser-only assumptions.

---

# Testing

Every implementation block must include meaningful automated tests.

Tests should favor observable behavior over fragile implementation inspection.

Important test categories include:

- configuration;
- authentication;
- authorization;
- SnG Control client;
- tool behavior;
- error mapping;
- invalid UUIDs;
- null fields;
- timeout;
- backend failures;
- credential safety;
- read-only behavior.

Tests must not require:

- real Google authentication;
- production SnG Control;
- real LLM calls

unless explicitly running an integration/smoke suite designed for that purpose.

Use mocks/fakes for normal automated tests.

---

# LLM tests

When LLM support is introduced, avoid tests that depend entirely on exact natural-language output.

Test:

- selected tool;
- tool arguments;
- tool result handling;
- permission boundary;
- structured references;
- fallback behavior;
- unsupported requests.

Natural-language assertions should be limited to important contractual behavior.

---

# Code quality

Use:

- clear type hints;
- Pydantic models for external contracts;
- small explicit interfaces;
- descriptive names;
- dependency injection where it improves testability;
- centralized configuration.

Avoid:

- massive service classes;
- deeply generic frameworks;
- unnecessary abstractions;
- speculative infrastructure.

Prefer simple code that clearly enforces the security boundary.

---

# Configuration

Secrets and deployment-specific values must use environment/configuration.

Do not commit:

- tokens;
- credentials;
- production URLs containing secrets;
- `.env`;
- API keys.

Maintain `.env.example` with safe placeholders.

---

# API compatibility

SnG-Control-Backend is an external dependency from the perspective of this repository.

Model its current contracts explicitly.

If backend evolves, update Intelligence deliberately.

Do not silently accept incompatible payloads and invent fallback business meanings.

Be tolerant only where doing so is semantically safe.

---

# Scope discipline

Before implementing a feature, determine whether it belongs to:

```text
SnG-Control-Backend
SnG-Control-Frontend
SnG_Intelligence
```

Examples:

### Backend responsibility

```text
"Is this task overdue?"
```

The backend decides.

### Intelligence responsibility

```text
"Which overdue projects deserve management attention?"
```

Intelligence may summarize/rank authoritative backend information.

### Frontend responsibility

```text
"How is that information displayed?"
```

Frontend decides.

Do not solve cross-repository concerns in the wrong repository for convenience.

---

# Current development phases

Expected progression:

```text
Intelligence 1A
Core + Auth + read-only SnG Control tools

Intelligence 1B
LLM + agent orchestration + tool calling

Intelligence 1C
Frontend conversational integration

Intelligence 1D
Voice interface
```

Do not implement future blocks prematurely unless explicitly instructed.

---

# Git discipline

Unless explicitly requested:

- do not commit;
- do not push;
- do not merge;
- do not modify unrelated repositories.

At the end of substantial work report:

- branch;
- changed files;
- tests;
- lint/typecheck;
- working tree status;
- whether any commit or push occurred.

---

# Documentation discipline

Keep `README.md` synchronized with real behavior.

Do not document planned functionality as already implemented.

Clearly distinguish:

```text
implemented
planned
out of scope
```

Architecture and security rules in this file must be updated deliberately if product requirements change.

---

# Non-negotiable rules

The following rules must not be violated without an explicit architectural decision from the project owner:

1. **SnG-Control-Backend is the source of truth.**
2. **No direct access to the SnG Control database.**
3. **Intelligence is read-only unless explicitly redesigned otherwise.**
4. **No arbitrary HTTP tool or proxy.**
5. **No credentials exposed to the LLM.**
6. **No Google token persistence or logging.**
7. **No duplicated Leak Time or workflow logic.**
8. **No authorization based on email, name or hardcoded Subject.**
9. **No RAG without a concrete unstructured-data requirement.**
10. **No hallucinated operational data.**
11. **No cross-repository modifications unless explicitly requested.**
12. **Security restrictions must be enforced in code architecture, not only in prompts.**

When a requested implementation conflicts with one of these rules, stop that specific implementation path, preserve the safe architecture, and report the conflict clearly.