"""Tests del launcher uvicorn — workers vs reload, sin levantar el server."""

from __future__ import annotations

from typing import Any

import pytest

from westfield_agent_back_python import main_api

CFG: dict[str, Any] = {
    "project": {"env": "prod"},
    "service": {"host": "127.0.0.1", "port": 8000, "workers": 1},
    "rate_limit": {"max_requests": 12},
}


@pytest.fixture
def uvicorn_run(monkeypatch) -> dict[str, Any]:
    """Captura los kwargs de uvicorn.run en vez de arrancar el server."""
    captured: dict[str, Any] = {}

    def _fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(main_api.uvicorn, "run", _fake_run)
    return captured


def _con_workers(monkeypatch, workers: int, env: str = "prod") -> None:
    cfg = {**CFG, "project": {"env": env}, "service": {**CFG["service"], "workers": workers}}
    monkeypatch.setattr(main_api, "load_config", lambda: cfg)


def test_prod_levanta_n_workers_sin_reload(monkeypatch, uvicorn_run) -> None:
    _con_workers(monkeypatch, 4)

    main_api.main()
    assert uvicorn_run["workers"] == 4
    assert uvicorn_run["reload"] is False
    # workers > 1 exige import string: uvicorn re-importa la app en cada proceso.
    assert uvicorn_run["app"] == "westfield_agent_back_python.entrypoints.api:app"


def test_dev_fuerza_un_worker_porque_reload_no_admite_varios(monkeypatch, uvicorn_run) -> None:
    _con_workers(monkeypatch, 4, env="dev")

    main_api.main()
    assert uvicorn_run["reload"] is True
    assert uvicorn_run["workers"] == 1


def test_prod_default_un_worker(monkeypatch, uvicorn_run) -> None:
    _con_workers(monkeypatch, 1)

    main_api.main()
    assert uvicorn_run["workers"] == 1
    assert uvicorn_run["reload"] is False


def test_reload_se_decide_con_project_env_no_con_env_crudo(monkeypatch, uvicorn_run) -> None:
    # Aunque la var cruda diga otra cosa, manda el valor normalizado del config.
    monkeypatch.setenv("ENV", "DEV")
    _con_workers(monkeypatch, 2, env="prod")

    main_api.main()
    assert uvicorn_run["reload"] is False
    assert uvicorn_run["workers"] == 2
