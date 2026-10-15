# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
poetry install                 # deps (incluye boto3 + faiss-cpu)
cp .env.example .env           # OPENAI_API_KEY + credenciales AWS
make run-api                   # uvicorn en localhost:8000 (reload si ENV=dev)
make run-worker                # worker (V1: stub no-op)
make test                      # poetry run pytest -v
make lint                      # ruff check
make format                    # ruff format + ruff check --fix
make docker-up                 # api + worker vía docker compose
```

Un solo test / un solo caso:

```bash
poetry run pytest tests/test_api.py -v
poetry run pytest tests/test_agent_registry.py::test_ttl_expirado_recarga -v
poetry run pytest -k "hard_rules" -v
```

`pytest` está configurado con `asyncio_mode = "auto"` y `pythonpath = ["src"]` — los tests `async def` no necesitan marker y los imports son `westfield_agent_back_python.…` / `tests.fakes`.

Publicar un agente a S3 (no requiere redeploy): `python scripts/publish_agent.py <agent_id> [--only prompt|config] [--dry-run]`.

## Qué es esto

Runtime **multi-agente y multi-tenant**: una sola instancia FastAPI (una sola imagen Docker) atiende N agentes de N universidades. Config, prompt y base vectorial FAISS de cada agente se cargan **desde S3 en runtime** según el `agent_id` y el `university_code` del request. Crear un agente o una universidad nueva = publicar archivos en S3, **sin cambios de código ni deploy**.

La generación de embeddings de documentos está desacoplada: este repo solo embebe la *query* de cada turno. Los vector stores los construye y publica el repo hermano de ingesta (`Westfield_agent_ingest_python`); el contrato entre ambos es el JSON en S3, no código compartido.

Endpoints (convención del gateway de la plataforma):

```
GET  /api/v1/health                                                   # instancia + agentes cacheados
GET  /api/v1/universities/{university_code}/agents                    # discovery
GET  /api/v1/universities/{university_code}/agents/{agent_id}/health  # fuerza la carga de UNO
POST /api/v1/universities/{university_code}/agents/{agent_id}/chat    # un turno
```

## Arquitectura

Hexagonal-lite. La regla de dependencia se respeta estrictamente — si un cambio te obliga a importar `httpx`, `boto3` o `faiss` fuera de `adapters/`, el diseño se está rompiendo.

```
domain/       entidades Pydantic + errores. Sin I/O.
application/  casos de uso. Depende de ports, nunca de adapters concretos.
ports/        Protocols: ChatClient, Embeddings, Retriever, ObjectStorage.
adapters/     implementaciones: openai_*, faiss_retriever, s3_object_storage, llm_factory.
entrypoints/  api.py (composition root) + worker.py.
shared/       config, logger, rate_limit, shutdown.
```

**`entrypoints/api.py::create_app()` es el composition root** — el único lugar donde se instancian adapters concretos. Acepta `storage=` y `config=` inyectados: así los tests montan la app entera sin tocar AWS ni red.

**`application/agent_registry.py` es el corazón del sistema.** Cachea un `AgentRuntime` (config + prompt + chat client + retriever) por clave `"{university_code}:{agent_id}"`, con TTL, caché negativa y un `asyncio.Lock` por clave. Todo el I/O de S3 (boto3, sync) corre en `asyncio.to_thread` y solo en cold-load o expiración de TTL — nunca en el camino caliente.

Flujo de un turno (`application/chat_with_agent.py`): resolver runtime → clamps defensivos → retrieve RAG (best-effort) → `build_system_prompt` (system.md + always_include + chunks + `state`) → LLM → `parse_llm_output` → `apply_hard_rules` → anti-leak → `ChatResponse`.

### Multi-tenancy

`s3.prefix` es un **template** con placeholder: `org={university_code}/agents`. El registry lo resuelve por request (`_prefix_for`). Un prefijo sin placeholder degrada a single-tenant. `university_code` y `agent_id` se validan contra `_SLUG_RE` antes de tocar S3 — eso es la defensa contra path traversal en las keys.

### Aislamiento de fallos (invariante central)

El fallo de un agente **jamás** afecta a otros agentes de la misma instancia. La taxonomía es deliberada; respetala al agregar código:

| Situación | Resultado |
|---|---|
| slug inválido / sin `config.json` | `404` + caché negativa (`AgentNotFoundError`) |
| config corrupta, prompt ausente/vacío, `llm_provider` desconocido | `503` (`AgentLoadError`) — solo ese agente |
| vector store roto/ausente, o sin key de embeddings | `200` con `rag_used: false` → agente **degradado**, no error |
| sin API key del provider, o LLM caído/timeout | `200` con `fallback: true` + `fallback_message` del agente |
| recarga por TTL falla habiendo entrada previa | **sirve stale** y reintenta tras `negative_ttl` (disponibilidad > consistencia) |
| rate limit por IP+tenant+agente | `429` |

Los errores HTTP salen siempre con shape `{"error": "..."}` (hay un handler que sobreescribe el `{"detail": ...}` default de FastAPI).

### Puntos de extensión

- **Proveedor LLM nuevo** (Anthropic, Bedrock, Azure…): implementar `ports.ChatClient` / `ports.Embeddings` en `adapters/`, registrar el builder en `CHAT_PROVIDERS` / `EMBEDDING_PROVIDERS` de [llm_factory.py](src/westfield_agent_back_python/adapters/llm_factory.py), y exponer la API key en `ProviderSettings` (que se arma en `create_app`). Nada más cambia. Semántica: provider no registrado → `AgentLoadError`; registrado pero sin key → `None` (degradado, no error).
- **API key por agente** (`OPENAI_API_KEY_<AGENT_ID>`, para separar el gasto): `shared/config.py` recolecta las env vars con ese prefijo en `cfg["openai"]["agent_api_keys"]`; el registry llama a `ProviderSettings.for_agent(agent_id)` una vez en cold-load y pasa ese settings ya resuelto tanto a `build_chat_client` como a `build_embeddings`. **Los builders no cambian de firma** — ese es el punto del diseño, y por eso `register_fake_provider` sigue funcionando con sus lambdas de aridad fija. Si falta la dedicada se usa la global con un `🟡`. `shared/` no importa de `adapters/`: `config.py` solo junta env vars crudas, la normalización `agent_id → SUFIJO` vive en `agent_env_suffix()`.
- **Mecánicas deterministas por agente**: `hard_rules` en el `config.json`, evaluadas en [hard_rules.py](src/westfield_agent_back_python/application/hard_rules.py). Existen porque el LLM trata las reglas del prompt como sugerencias: lo que DEBE cumplirse se fuerza server-side sobre `structured` después de parsear. Ningún conocimiento de un agente concreto debe vivir en el runtime — si estás por escribir `if agent_id == "maia"`, la lógica va en el prompt o en una hard rule.

## Contratos con S3

`agents/<agent_id>/config.json` es la fuente de verdad de cada agente (shape en [domain/agent.py](src/westfield_agent_back_python/domain/agent.py)). El README documenta el JSON completo. Puntos que se olvidan:

- `vector_store_id`/`vector_store_s3_uri` en `null` → agente **sin RAG**, es un modo válido, no un error.
- `top_k`/`min_similarity` del config **pisan** los `retrieval_defaults` del manifest (tunear retrieval sin re-ingestar).
- `response_format: "json"` → al LLM se le pide `json_object`; el runtime extrae la key convencional `message` y hace passthrough del objeto entero en `ChatResponse.structured`. JSON malformado **degrada a texto**, nunca rompe el turno.
- **Invariante del vector store**: `chunks[i]` ↔ fila `i` de `index.faiss`. El loader valida `index.ntotal == len(chunks)` y `index.d == embedding_dimensions`; cualquier mismatch degrada el agente en vez de servir retrieval inconsistente.
- Las queries se embeben con el `embedding_provider`/`embedding_model` **del manifest**, no de env — así query y chunks viven en el mismo espacio vectorial.
- El índice es `IndexFlatIP` con vectores L2-normalizados, así que el inner product **es** la similitud coseno.
- Los chunks `instructor_only` alimentan el prompt pero se filtran de `ChatResponse.sources`; además hay anti-leak por `leak_markers` sobre el texto de salida.

## Agentes versionados en `agents/`

Cada `agents/<agent_id>/{system.md, config.json}` es la copia editable de lo que vive en S3 — el runtime **no** los lee del disco. Ciclo: editar → `python scripts/publish_agent.py <agent_id>` → el runtime lo toma al expirar el TTL (300s prod / 30s dev) o al reiniciar el servicio. Verificar con el endpoint `/health` del agente (devuelve `prompt_id`, `llm_model`, `vector_store_id` vigentes, y `api_key: "dedicada"|"global"`).

⚠️ **`agents/` hoy está untracked** (`git ls-files agents/` no devuelve nada), así que las únicas copias son el working tree y S3 — no hay historial de los prompts. Vale la pena commitearlos. Ojo también con el otro escritor de `config.json`: el repo de ingesta hace read-modify-write del documento completo (`model_dump()`) e ignora campos desconocidos, así que un campo nuevo agregado solo desde acá se borra en la próxima `ingest run --activate`. Por eso las API keys van por env var y no en el config.

El script publica a `org=${UNIVERSITY_CODE}/agents/<agent_id>/` (default `westfield`). Los agentes actuales: `maia` (único con `response_format: json` + hard rules), `aria`, `gregorio_iii`, `minerva` (+ 3 variantes), `student_services`, `westy`.

## Configuración

`configs/base.yaml` + `configs/<ENV>.yaml` (deep merge) + **env vars, que siempre ganan** ([shared/config.py](src/westfield_agent_back_python/shared/config.py)). Devuelve un `dict` plano a propósito (paridad con el template ESIC), no un objeto Pydantic Settings. Las API keys vienen **solo** de env; las credenciales AWS ni pasan por acá (cadena estándar de boto3).

`ENV=dev` baja el TTL del registry a 30s y activa `reload` de uvicorn.

`WEB_CONCURRENCY` (default 1) levanta N procesos uvicorn ([main_api.py](src/westfield_agent_back_python/main_api.py)); es la palanca de throughput, porque un proceso satura ~1 core alrededor de ~1.100 rpm. Todo el estado es **por proceso** — registry, rate limiter — así que N workers = N cold-loads desde S3 por agente y un rate limit efectivo de hasta N × `RATE_LIMIT_MAX`. Con `ENV=dev` se fuerza 1 worker porque uvicorn no combina `reload` con `workers`. No confundir con `make run-worker` (el worker de fondo, otro proceso).

## Tests

Ningún test toca AWS ni la red. Todo pasa por [tests/fakes.py](tests/fakes.py):

- `FakeObjectStorage` — S3 en memoria, cuenta `get_attempts` por key y permite marcar `fail_keys` (así se asertan caché, locks y degradación).
- `build_agent_fixture(...)` — publica un agente completo, incluyendo un **índice FAISS real serializado** (mismo camino de deserialización que producción). FAISS sí se ejercita de verdad, con índices diminutos.
- `register_fake_provider(monkeypatch, ...)` — registra el provider `"fake"` en la fábrica; es la prueba viva del punto de extensión.
- Los tests de API construyen la app con `create_app(storage=fake, config=TEST_CFG)`.

Al tocar el registry o el use case, cubrí el comportamiento *degradado*, no solo el happy path — es donde vive el valor del diseño.

## Convenciones

- Docstrings, comentarios y mensajes de log en **español**; los logs de arranque/estado usan emojis de estado (🟢 ok, 🟡 degradado, ✗ error, 🧠 agente cargado). Seguí ese registro al agregar código.
- Ruff con `line-length = 100`, reglas `E,F,I,B,UP,SIM,RUF` (`E501` ignorada), `from __future__ import annotations` en todos los módulos. Pre-commit corre ruff + ruff-format.
- El paquete se llama `westfield_agent_back_python` aunque el repo sea `prisma-backend-agents` — es el nombre histórico, no lo renombres a la ligera (aparece en Makefile, Dockerfile, compose y todos los imports).
- El README documenta el bucket `westfield-agent-knowledge`, pero los `config.json` vigentes apuntan a `prisma-agents-knowledge`. El bucket real sale de `S3_BUCKET`; las `s3://` URIs dentro de cada config son absolutas y mandan sobre el default.
