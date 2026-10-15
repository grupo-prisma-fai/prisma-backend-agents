"""
Carga de configuración: base.yaml + <env>.yaml + override por env vars.

Extiende el patrón del template ESIC con las secciones del runtime
multi-agente: `s3` (bucket de conocimiento), `registry` (caché de agentes)
y `openai` (settings del proveedor; las API keys vienen SOLO de env).

Las env vars siempre ganan sobre el YAML. Eso permite:
  - dev local: editar configs/dev.yaml
  - prod: setear S3_BUCKET, OPENAI_API_KEY, PORT, etc. desde el orchestrator
  - .env (vía python-dotenv): conveniencia local

Las credenciales AWS no pasan por acá: boto3 usa su cadena estándar
(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / perfil / rol IAM).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Carga .env si existe en el cwd (no falla si no está).
load_dotenv()


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Merge profundo: las keys de `b` sobreescriben a las de `a`."""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _collect_prefixed_env(prefix: str) -> dict[str, str]:
    """
    Recolecta las env vars `<prefix><SUFIJO>` en {SUFIJO: valor}.

    Ignora la var exacta sin sufijo, los sufijos vacíos y los valores vacíos.
    No interpreta el sufijo: quien lo resuelve contra un agent_id es
    adapters/llm_factory.py — esta capa no conoce agentes.
    """
    out: dict[str, str] = {}
    for name, value in os.environ.items():
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :].strip().upper()
        if suffix and value.strip():
            out[suffix] = value.strip()
    return out


def load_config(config_dir: str = "configs") -> dict[str, Any]:
    """
    Lee base.yaml + <ENV>.yaml (si existe) y aplica overrides por env vars.

    Devuelve un dict simple — los entrypoints leen las keys directo. No usamos
    Pydantic Settings acá porque el template ESIC es YAML-first y queremos
    mantener paridad de estructura entre servicios.
    """
    env = os.getenv("ENV", "dev").lower()
    base_path = Path(config_dir) / "base.yaml"
    env_path = Path(config_dir) / f"{env}.yaml"

    with base_path.open("r", encoding="utf-8") as f:
        cfg: dict[str, Any] = yaml.safe_load(f) or {}

    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as f:
            env_cfg: dict[str, Any] = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, env_cfg)

    # Garantizar las secciones por si el YAML viene incompleto.
    cfg.setdefault("project", {})
    cfg.setdefault("service", {})
    cfg.setdefault("worker", {})
    cfg.setdefault("s3", {})
    cfg.setdefault("registry", {})
    cfg.setdefault("openai", {})
    cfg.setdefault("rate_limit", {})

    # ---- project ----
    cfg["project"]["env"] = os.getenv("ENV", cfg["project"].get("env", "dev"))
    cfg["project"]["log_level"] = os.getenv(
        "LOG_LEVEL", cfg["project"].get("log_level", "INFO")
    )

    # ---- service ----
    cfg["service"]["host"] = os.getenv("HOST", cfg["service"].get("host", "0.0.0.0"))
    cfg["service"]["port"] = int(os.getenv("PORT", cfg["service"].get("port", 8000)))
    cfg["service"].setdefault("cors_origins", ["*"])
    # Procesos uvicorn. WEB_CONCURRENCY es la convención estándar (uvicorn,
    # gunicorn, Heroku…). Cada worker tiene su propio GIL, así que el techo de
    # rpm escala con N; ver main_api.py para lo que eso implica.
    cfg["service"]["workers"] = max(
        1, int(os.getenv("WEB_CONCURRENCY", cfg["service"].get("workers", 1)))
    )

    # ---- worker ----
    cfg["worker"]["enabled"] = _env_bool(
        "WORKER_ENABLED", bool(cfg["worker"].get("enabled", True))
    )
    cfg["worker"]["interval_seconds"] = int(
        os.getenv("WORKER_INTERVAL_SECONDS", cfg["worker"].get("interval_seconds", 10))
    )

    # ---- s3 (bucket de conocimiento multi-agente) ----
    cfg["s3"]["bucket"] = os.getenv(
        "S3_BUCKET", cfg["s3"].get("bucket", "westfield-agent-knowledge")
    )
    cfg["s3"]["region"] = os.getenv("AWS_REGION", cfg["s3"].get("region", "us-east-1"))
    # Template multi-tenant: el placeholder {university_code} lo resuelve el
    # AgentRegistry POR REQUEST con el segmento de la ruta.
    cfg["s3"]["prefix"] = os.getenv(
        "S3_PREFIX", cfg["s3"].get("prefix", "org={university_code}/agents")
    )

    # ---- registry (caché de runtimes de agente) ----
    cfg["registry"]["ttl_seconds"] = int(
        os.getenv("REGISTRY_TTL_SECONDS", cfg["registry"].get("ttl_seconds", 300))
    )
    cfg["registry"]["negative_ttl_seconds"] = int(
        os.getenv(
            "REGISTRY_NEGATIVE_TTL_SECONDS",
            cfg["registry"].get("negative_ttl_seconds", 30),
        )
    )

    # ---- openai (proveedor LLM; key solo por env) ----
    openai_cfg = cfg["openai"]
    openai_cfg["api_key"] = os.getenv("OPENAI_API_KEY")  # sin default — None = fallback
    openai_cfg["base_url"] = os.getenv(
        "OPENAI_BASE_URL", openai_cfg.get("base_url", "https://api.openai.com/v1")
    )
    openai_cfg["embedding_model_fallback"] = os.getenv(
        "OPENAI_EMBEDDING_MODEL_FALLBACK",
        openai_cfg.get("embedding_model_fallback", "text-embedding-3-small"),
    )
    # Keys dedicadas por agente: OPENAI_API_KEY_<AGENT_ID>. Una key = un
    # proyecto de OpenAI = una línea separada en la factura, que es lo que
    # permite atribuir el gasto agente por agente. Un agente sin key propia
    # cae a la global (con warning al cargarse).
    openai_cfg["agent_api_keys"] = _collect_prefixed_env("OPENAI_API_KEY_")

    # ---- rate_limit ----
    cfg["rate_limit"]["window_seconds"] = int(
        os.getenv("RATE_LIMIT_WINDOW", cfg["rate_limit"].get("window_seconds", 60))
    )
    cfg["rate_limit"]["max_requests"] = int(
        os.getenv("RATE_LIMIT_MAX", cfg["rate_limit"].get("max_requests", 12))
    )

    return cfg
