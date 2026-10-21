"""
Adapter: carga secretos de AWS Secrets Manager al entorno del proceso.

Si `SECRETS_MANAGER_SECRET_ID` está definida, los launchers (main_api,
main_worker) llaman a `load_secrets_into_env` ANTES de `load_config()`. El
secreto es un JSON plano `{"OPENAI_API_KEY": "...", "OPENAI_API_KEY_MAIA": ...}`
y cada par se vuelca a `os.environ`; desde ahí `shared/config.py` lo lee igual
que si viniera del `.env`, así que nada aguas abajo cambia.

Por qué en el launcher y no en `config.py`: `shared/` no importa boto3 (la
regla de dependencia lo prohíbe). Y por qué antes de uvicorn: los procesos
worker de uvicorn heredan el entorno del padre, así que se lee el secreto una
sola vez y los secretos viven solo en memoria — ni en disco ni en el
`docker inspect` del contenedor.

Semántica:
  - Sin `secret_id` → no-op (dev local con `.env` sigue igual).
  - Las env vars ya definidas GANAN sobre el secreto (mismo criterio que
    config.py: env siempre gana), así un override puntual sigue siendo posible.
  - Si el secreto no se puede leer o no es un JSON de strings → `SecretsLoadError`
    y el arranque se corta. Arrancar sin keys dejaría a TODOS los agentes en
    fallback sin que nadie se entere; preferimos que el contenedor no levante.
"""

from __future__ import annotations

import json
import os
from collections.abc import MutableMapping
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from westfield_agent_back_python.shared.logger import get_logger

log = get_logger("westfield_agent_back_python.secrets")

SECRET_ID_ENV = "SECRETS_MANAGER_SECRET_ID"

# Reintentos con backoff de botocore para fallos transitorios (red, throttling).
_BOTO_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"})


class SecretsLoadError(RuntimeError):
    """El secreto configurado no se pudo leer o no tiene el formato esperado."""


def load_secrets_into_env(
    secret_id: str | None,
    *,
    region: str | None = None,
    client: Any | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    """
    Vuelca el secreto JSON `secret_id` a `environ` (default `os.environ`).

    Devuelve los nombres de las variables cargadas (nunca los valores).
    raises SecretsLoadError si el secreto no se puede leer o parsear.
    """
    if not secret_id:
        return []

    env = os.environ if environ is None else environ
    sm = client or boto3.client("secretsmanager", region_name=region, config=_BOTO_CONFIG)

    try:
        raw = sm.get_secret_value(SecretId=secret_id)["SecretString"]
    except (ClientError, BotoCoreError, KeyError) as err:
        raise SecretsLoadError(f"no se pudo leer el secreto '{secret_id}': {err}") from err

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise SecretsLoadError(f"el secreto '{secret_id}' no es JSON válido") from err

    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise SecretsLoadError(f"el secreto '{secret_id}' debe ser un objeto JSON de strings")

    loaded = [k for k in data if k not in env]
    overridden = sorted(set(data) - set(loaded))
    for key in loaded:
        env[key] = data[key]

    log.info(f"🟢 {len(loaded)} variables cargadas desde Secrets Manager ('{secret_id}').")
    if overridden:
        log.warning(f"🟡 ya definidas en el entorno, no se pisan: {', '.join(overridden)}")
    return loaded


def load_secrets_from_env_config() -> list[str]:
    """Atajo para los launchers: lee el secret id y la región del propio entorno."""
    return load_secrets_into_env(os.getenv(SECRET_ID_ENV), region=os.getenv("AWS_REGION"))
