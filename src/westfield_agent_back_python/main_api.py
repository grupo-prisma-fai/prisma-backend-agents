"""
Launcher uvicorn para el API entrypoint.

Se invoca con `python -m westfield_agent_back_python.main_api` (eso es lo
que llama el Makefile, el Dockerfile y el docker-compose). En dev habilita
reload para que los cambios de código se reflejen sin reiniciar manualmente.

Workers (`WEB_CONCURRENCY`): Python atiende ~1 core por proceso por el GIL,
así que el throughput de UNA instancia escala con el número de procesos. Cada
worker es un proceso independiente que importa `entrypoints.api` y arma su
propia app, y con ella su propio estado in-process:
  - AgentRegistry: N cachés → N cold-loads desde S3 por agente.
  - RateLimiter: el kernel reparte las conexiones entre workers, así que el
    límite efectivo por IP+tenant+agente puede llegar a N x RATE_LIMIT_MAX.

`reload` y `workers` son incompatibles en uvicorn: en dev manda reload y se
fuerza un solo worker.
"""

from __future__ import annotations

import uvicorn

from westfield_agent_back_python.adapters.secrets_manager_env import load_secrets_from_env_config
from westfield_agent_back_python.shared.config import load_config
from westfield_agent_back_python.shared.logger import get_logger

log = get_logger("westfield_agent_back_python.main_api")


def main() -> None:
    # Antes de load_config y de uvicorn: los workers heredan el entorno del padre.
    # Si SECRETS_MANAGER_SECRET_ID está y el secreto falla, el arranque se corta.
    load_secrets_from_env_config()
    cfg = load_config()
    # project.env ya viene normalizado por load_config: misma fuente de verdad
    # que elige el <env>.yaml, así ENV=DEV y ENV=dev no divergen.
    reload = cfg["project"]["env"] == "dev"
    workers = cfg["service"]["workers"]

    if reload and workers > 1:
        log.warning(f"🟡 ENV=dev usa reload: se ignora WEB_CONCURRENCY={workers} (1 worker).")
        workers = 1
    elif workers > 1:
        log.info(
            f"🟢 {workers} workers uvicorn — registry y rate limit son por proceso "
            f"(límite efectivo hasta {workers}x{cfg['rate_limit']['max_requests']}/min)."
        )

    uvicorn.run(
        "westfield_agent_back_python.entrypoints.api:app",
        host=cfg["service"]["host"],
        port=cfg["service"]["port"],
        reload=reload,
        workers=workers,
    )


if __name__ == "__main__":
    main()
