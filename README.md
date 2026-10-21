# Westfield Agent — Runtime Multi-Agente (Python)

Backend FastAPI que funciona como **runtime multi-agente** (HU-001): una única
instancia atiende N agentes conversacionales, cargando dinámicamente desde AWS S3
la configuración, el prompt y la base vectorial FAISS de cada agente según el
`agent_id` del request. Crear un agente nuevo **no requiere** ni Docker nuevo,
ni microservicio nuevo, ni cambios de código — solo publicar archivos en S3
(ver repo hermano `Westfield_agent_ingest_python`).

Rutas según la convención del gateway de la plataforma. El segmento `{university_code}` es
**dinámico** (multi-tenant): se mapea al prefijo S3 del tenant (`org={university_code}/agents`)
en cada request — una universidad nueva es una carpeta nueva en el bucket, sin tocar código:

- `GET  /api/v1/health` — estado + agentes cargados en la instancia.
- `GET  /api/v1/universities/{university_code}/agents/{agent_id}/health` — fuerza la carga de un agente y reporta su estado.
- `POST /api/v1/universities/{university_code}/agents/{agent_id}/chat` — un turno de conversación.

La **generación de embeddings está desacoplada**: este runtime solo embebe la
query de cada turno; los documentos los procesa el servicio de ingesta
(repo `Westfield_agent_ingest_python`) que publica las bases vectoriales en S3.

---

## Comandos

```bash
poetry install                 # crea venv e instala deps (incluye boto3 + faiss-cpu)
cp .env.example .env           # editar OPENAI_API_KEY (en prod: SECRETS_MANAGER_SECRET_ID)
make run-api                   # API en localhost:8000
make run-worker                # Worker (V1 stub no-op)
make test                      # pytest -v (sin AWS ni red — todo con fakes)
make lint                      # ruff check
make format                    # ruff format + ruff check --fix
make docker-build              # construir imagen Docker
make docker-up                 # API + worker via docker compose
```

---

## Layout

```
src/westfield_agent_back_python/
├── domain/         # entidades + tipos (no I/O)
│   ├── agent.py            # AgentConfig (shape de config.json en S3)
│   ├── chat.py             # ChatRequest, ChatResponse, ChatTurn, SourceRef
│   ├── rag.py              # VectorStoreManifest, ChunksFile, ChunkMeta, RetrievedChunk
│   └── errors.py           # AgentNotFoundError (404), AgentLoadError (503), ...
├── application/    # casos de uso
│   ├── chat_with_agent.py  # use case principal (un turno)
│   ├── agent_registry.py   # caché lazy por agent_id (TTL, locks, aislamiento)
│   ├── prompt_builder.py   # ensambla system.md + contexto + estado
│   └── sanitizers.py       # parse_llm_output (json/text) + anti-leak
├── ports/          # interfaces (Protocols)
│   ├── chat_client.py      # chat(messages) -> texto crudo
│   ├── embeddings.py       # embed_one(text) -> vector
│   ├── retriever.py        # retrieve(query) -> chunks
│   └── object_storage.py   # get_bytes/get_text (sync; el registry usa to_thread)
├── adapters/       # implementaciones concretas
│   ├── llm_factory.py      # ← punto de extensión de proveedores LLM
│   ├── openai_chat_client.py
│   ├── openai_embeddings.py
│   ├── faiss_retriever.py
│   ├── vector_store_loader.py
│   └── s3_object_storage.py
├── entrypoints/
│   ├── api.py              # FastAPI + composition root (create_app)
│   └── worker.py           # stub no-op
└── shared/         # config, logger, shutdown, rate_limit
```

### Cambiar de modelo / proveedor LLM

Cada agente declara `llm_provider` + `llm_model` en su `config.json`. El
registry construye su `ChatClient` vía [adapters/llm_factory.py](src/westfield_agent_back_python/adapters/llm_factory.py).
Para agregar un proveedor (Anthropic, Azure, Bedrock, ...):

1. Implementar `ports.ChatClient` (y/o `ports.Embeddings`) en `adapters/`.
2. Registrarlo en `CHAT_PROVIDERS` / `EMBEDDING_PROVIDERS` (1 línea).
3. Exponer su API key en `ProviderSettings` (composition root en `api.py`).

La resolución de la key por agente es transparente para el builder: el registry llama a
`ProviderSettings.for_agent(agent_id)` en cold-load y le pasa un settings ya resuelto, así que
el builder solo hace `settings.api_key("mi-provider")` como siempre.

El use case, el registry y los handlers no cambian. Un agente con provider
desconocido devuelve 503 **solo para ese agente** — los demás siguen operando.

---

## Estructura esperada en S3

```
s3://westfield-agent-knowledge/
  agents/
    <agent_id>/
      config.json                  ← fuente de verdad del agente (la escribe la ingesta)
      prompts/
        system.md                  ← prompt base del agente
      docs/                        ← docs fuente (los lee la ingesta, no el runtime)
      vector_store/
        v1/
          index.faiss              ← IndexFlatIP serializado (vectores L2-normalizados)
          chunks.json              ← textos: chunks[i] ↔ fila i del index.faiss
          manifest.json            ← metadata + embedding_model
        v2/ ...                    ← versiones inmutables; config.json apunta a la activa
```

## Contratos JSON (compartidos con la ingesta)

### `agents/<agent_id>/config.json`

```json
{
  "agent_id": "maia",
  "agent_name": "Maia — Tutora Socrática Westfield",
  "prompt_id": "system-v1",
  "prompt_s3_uri": "s3://westfield-agent-knowledge/agents/maia/prompts/system.md",
  "vector_store_id": "v1",
  "vector_store_s3_uri": "s3://westfield-agent-knowledge/agents/maia/vector_store/v1/",
  "llm_provider": "openai",
  "llm_model": "gpt-4o-mini",
  "temperature": 0.6,
  "top_k": 4,
  "min_similarity": 0.2,
  "response_format": "json",
  "max_tokens": 800,
  "fallback_message": "Estoy teniendo un problema técnico. Retomemos en un momento.",
  "leak_markers": ["Notas del instructor", "Vías de Respuesta"],
  "leak_replacement_message": "No puedo compartir material interno del instructor.",
  "metadata": {}
}
```

- Desde `response_format` hacia abajo: opcionales con defaults (`response_format: "text"`, `max_tokens: 800`).
- `vector_store_id`/`vector_store_s3_uri` en `null` → agente **sin RAG** (modo válido).
- `response_format: "json"` → el LLM recibe `response_format=json_object`; el runtime
  extrae la key convencional `message` y hace passthrough del objeto completo en
  `ChatResponse.structured`.
- `top_k`/`min_similarity` del config **pisan** los `retrieval_defaults` del manifest
  (permite tunear retrieval sin re-ingestar).
- `hard_rules` (opcional): mecánicas deterministas que el runtime fuerza server-side
  sobre `structured` — las reglas escritas en el prompt son sugerencias para el LLM,
  estas son garantías. Cada regla: `when` (condiciones AND sobre paths `state.<k>` /
  `structured.<k>` con operadores `eq, ne, gt, gte, lt, lte, in, nin`) y `set`
  (keys a forzar en `structured`). Ejemplo (avance forzado de Maia):
  ```json
  "hard_rules": [
    { "when": { "state.turns_for_current_question": { "gte": 3 },
                 "state.current_question": { "lte": 2 } },
      "set": { "advance_to_next_question": true } }
  ]
  ```

### `vector_store/vN/manifest.json`

```json
{
  "vector_store_id": "v1",
  "agent_id": "maia",
  "embedding_provider": "openai",
  "embedding_model": "text-embedding-3-small",
  "embedding_dimensions": 1536,
  "normalized": true,
  "metric": "inner_product",
  "generated_at": "2026-06-10T12:00:00Z",
  "chunk_count": 142,
  "chunking": { "chunk_size_chars": 1000, "chunk_overlap_chars": 100 },
  "retrieval_defaults": { "top_k": 4, "min_similarity": 0.2 },
  "source_docs": [
    { "doc_file": "caso.pdf", "doc_title": "Caso académico", "doc_tag": "case", "instructor_only": false }
  ]
}
```

El runtime embebe las queries con `embedding_provider`/`embedding_model` del
manifest — garantiza que query y chunks vivan en el mismo espacio vectorial.
Al cargar valida `index.ntotal == len(chunks)` y `index.d == embedding_dimensions`.

### `vector_store/vN/chunks.json`

```json
{
  "chunks": [
    { "id": "caso.pdf#0", "doc_file": "caso.pdf", "doc_title": "Caso académico",
      "doc_tag": "case", "instructor_only": false, "text": "..." }
  ],
  "always_include": [
    { "doc_file": "guia.md", "doc_title": "Guía operativa", "doc_tag": "operating_guide",
      "instructor_only": false, "text": "...doc completo, se inyecta cada turno..." }
  ]
}
```

**Invariante**: `chunks[i]` corresponde a la fila `i` del `index.faiss`.

### Endpoint conversacional

```
POST /api/v1/universities/{university_code}/agents/{agent_id}/chat
```

Request:

```json
{
  "conversation_id": "conv_123",
  "user_id": "student_001",
  "message": "Quiero entender este tema",
  "history": [
    { "role": "assistant", "content": "..." },
    { "role": "user", "content": "..." }
  ],
  "state": { "current_question": 2, "turns_for_current_question": 1 }
}
```

`state` es un dict libre opcional — el prompt builder lo renderiza como bloque
`# Estado actual`. Así un agente con mecánica de turnos (ej. Maia) la conserva
vía prompt, sin lógica específica en el runtime.

Response 200:

```json
{
  "agent_id": "maia",
  "conversation_id": "conv_123",
  "message": "texto visible para el usuario",
  "structured": { "message": "...", "rubric_level": "satisfactorio", "...": "..." },
  "sources": [ { "doc_title": "Caso académico", "doc_tag": "case", "similarity": 0.83 } ],
  "fallback": false,
  "rag_used": true
}
```

| Situación | Respuesta |
|---|---|
| `university_code` con slug inválido o sin agentes | `404 {"error": "..."}` |
| `agent_id` inexistente | `404 {"error": "..."}` |
| agente roto (config corrupta, prompt ausente, provider desconocido) | `503 {"error": "..."}` |
| rate limit (por IP+agente) | `429 {"error": "..."}` |
| vector store roto/ausente | `200` con `rag_used: false` (degradado controlado) |
| LLM caído / sin API key | `200` con `fallback: true` + `fallback_message` del agente |

El fallo de un agente **jamás** afecta a otros agentes de la misma instancia.

---

## Cómo dar de alta un agente (sin tocar código)

1. En el repo `Westfield_agent_ingest_python`:
   `ingest scaffold --agent-id soporte-ti --agent-name "Soporte TI" --model gpt-4o-mini`
   → crea `config.json` + `prompts/system.md` placeholder en S3. El agente ya responde (sin RAG).
2. Editar/subir el `system.md` real y los docs a `agents/soporte-ti/docs/`.
3. `ingest run --agent-id soporte-ti --source s3` → construye y publica el vector store, y
   actualiza `config.json` para apuntar a la versión nueva.
4. El runtime lo recoge solo: al primer request (agente nuevo) o al expirar el TTL
   del registry (`REGISTRY_TTL_SECONDS`, default 300s; 30s en dev).

---

## Variables de entorno

| Var | Default | Notas |
|---|---|---|
| `S3_BUCKET` | `westfield-agent-knowledge` | Bucket de conocimiento. |
| `AWS_REGION` | `us-east-1` | Región del bucket. |
| `S3_PREFIX` | `org={university_code}/agents` | Template multi-tenant; el placeholder se resuelve POR REQUEST con el segmento de la ruta. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | — | Cadena estándar de boto3. En EC2 no se usan: credenciales del rol IAM de la instancia. |
| `SECRETS_MANAGER_SECRET_ID` | (sin default) | Secreto JSON con las `OPENAI_API_KEY*`; se vuelca al entorno al arrancar (no quedan en disco). Si falla la lectura, el proceso no arranca. |
| `REGISTRY_TTL_SECONDS` | `300` (`30` en dev) | TTL del caché de agentes. |
| `REGISTRY_NEGATIVE_TTL_SECONDS` | `30` | Caché negativa de agent_ids inexistentes. |
| `OPENAI_API_KEY` | (sin default) | Key global. Sin esto, agentes openai responden fallback. |
| `OPENAI_API_KEY_<AGENT_ID>` | (sin default) | Key dedicada de un agente (ver abajo). Si falta, usa la global. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Azure / proxies. |
| `OPENAI_EMBEDDING_MODEL_FALLBACK` | `text-embedding-3-small` | Solo si el manifest no declara modelo. |
| `ENV` | `dev` | `dev` o `prod` → carga `configs/<env>.yaml`. |
| `LOG_LEVEL` | `INFO` | `DEBUG` muestra más en dev. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind del FastAPI. |
| `WEB_CONCURRENCY` | `1` | Procesos uvicorn del API (no confundir con el worker stub). Registry y rate limit son por proceso: límite efectivo hasta N × `RATE_LIMIT_MAX`. Se ignora con `ENV=dev` (reload). |
| `WORKER_ENABLED` / `WORKER_INTERVAL_SECONDS` | `true` / `10` | Worker stub. |
| `RATE_LIMIT_WINDOW` / `RATE_LIMIT_MAX` | `60` / `12` | Rate limit por IP+agente. |

### Key dedicada por agente (control de gastos)

Cada agente puede usar **su propia API key**, para que el gasto quede atribuido agente por
agente. La convención es `OPENAI_API_KEY_<AGENT_ID>` en mayúsculas, con los guiones convertidos
a guión bajo:

```bash
OPENAI_API_KEY=sk-global               # la de siempre: sigue siendo el fallback
OPENAI_API_KEY_MAIA=sk-proj-...        # agente maia
OPENAI_API_KEY_STUDENT_SERVICES=sk-... # agente student_services
```

En OpenAI, cada key pertenece a un proyecto y el uso se reporta por proyecto: creando un
proyecto por agente, **platform.openai.com → Usage** separa el gasto solo, sin telemetría
propia. La key cubre tanto el chat como el embedding de la query de RAG de ese agente.

- Las keys **no** van en `config.json` (viajaría a S3 y a la imagen). Solo env vars.
- Un agente sin key propia usa `OPENAI_API_KEY` y avisa con un `🟡` al cargarse.
- Un agente con key propia funciona **aunque no exista** `OPENAI_API_KEY`.
- Al arrancar, el log `🔑` lista qué agentes tienen key dedicada (solo nombres, nunca la key).
- Para verificar sin exponer nada:
  `GET /api/v1/universities/<u>/agents/<agent_id>/health` devuelve `"api_key": "dedicada" | "global"`.
- Agregar o rotar una key requiere actualizar el entorno y reiniciar el servicio (a diferencia
  de los prompts y vector stores, que se toman de S3 sin redeploy).

---

## Testing

```bash
make test                      # pytest -v
```

Ningún test toca AWS ni la red — todo vía fakes (`tests/fakes.py`):

- `test_faiss_retriever.py` — top-k, min_similarity, query vacía, embed roto.
- `test_agent_registry.py` — carga, caché negativa, TTL, serve-stale, locks, degradado.
- `test_chat_with_agent.py` — happy path, structured passthrough, fallback, anti-leak.
- `test_llm_factory.py` — providers, extensión con provider fake.
- `test_api.py` — 404/503/429 y **aislamiento entre agentes** en la misma instancia.
- `test_prompt_builder.py` / `test_sanitizers.py` — funciones puras.

FAISS sí se usa real en tests (índices diminutos construidos en memoria).

---

## Relación con el servicio de ingesta

| Pieza | Runtime (este repo) | Ingesta (`Westfield_agent_ingest_python`) |
|---|---|---|
| Rol | atiende conversaciones | construye y publica bases vectoriales |
| Embeddings | solo la query del turno | todos los documentos (batch) |
| S3 | lee | escribe (versiones inmutables vN) |
| Despliegue | servicio always-on | CLI / job bajo demanda |

El contrato entre ambos es el JSON de S3 (documentado arriba y en el README de
la ingesta) — no comparten código.
