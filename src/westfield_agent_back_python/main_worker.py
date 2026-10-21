"""
Launcher del Worker entrypoint.

Se invoca con `python -m westfield_agent_back_python.main_worker`. Igual que
main_api, carga los secretos de Secrets Manager antes de leer la config.
"""

from __future__ import annotations

from westfield_agent_back_python.adapters.secrets_manager_env import load_secrets_from_env_config
from westfield_agent_back_python.entrypoints.worker import main as worker_main


def main() -> None:
    load_secrets_from_env_config()
    worker_main()


if __name__ == "__main__":
    main()
