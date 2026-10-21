"""Tests del loader de Secrets Manager — con un cliente fake, sin tocar AWS."""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from westfield_agent_back_python import main_api
from westfield_agent_back_python.adapters import secrets_manager_env
from westfield_agent_back_python.adapters.secrets_manager_env import (
    SecretsLoadError,
    load_secrets_into_env,
)


class FakeSecretsClient:
    """Imita get_secret_value de boto3; cuenta llamadas y puede fallar."""

    def __init__(self, secret: Any = None, *, error_code: str | None = None) -> None:
        self._secret = secret
        self._error_code = error_code
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
        if self._error_code:
            raise ClientError({"Error": {"Code": self._error_code}}, "GetSecretValue")
        raw = self._secret if isinstance(self._secret, str) else json.dumps(self._secret)
        return {"SecretString": raw}


def test_sin_secret_id_es_noop() -> None:
    client = FakeSecretsClient({"OPENAI_API_KEY": "sk-x"})
    env: dict[str, str] = {}

    assert load_secrets_into_env(None, client=client, environ=env) == []
    assert env == {}
    assert client.calls == []


def test_carga_las_keys_al_entorno() -> None:
    client = FakeSecretsClient({"OPENAI_API_KEY": "sk-global", "OPENAI_API_KEY_MAIA": "sk-maia"})
    env: dict[str, str] = {}

    loaded = load_secrets_into_env("prisma/test", client=client, environ=env)

    assert sorted(loaded) == ["OPENAI_API_KEY", "OPENAI_API_KEY_MAIA"]
    assert env == {"OPENAI_API_KEY": "sk-global", "OPENAI_API_KEY_MAIA": "sk-maia"}
    assert client.calls == ["prisma/test"]


def test_env_ya_definida_gana_sobre_el_secreto() -> None:
    client = FakeSecretsClient({"OPENAI_API_KEY": "sk-secreto", "OPENAI_API_KEY_ARIA": "sk-aria"})
    env = {"OPENAI_API_KEY": "sk-override"}

    loaded = load_secrets_into_env("prisma/test", client=client, environ=env)

    assert loaded == ["OPENAI_API_KEY_ARIA"]
    assert env["OPENAI_API_KEY"] == "sk-override"


def test_secreto_inaccesible_corta_el_arranque() -> None:
    client = FakeSecretsClient(error_code="AccessDeniedException")

    with pytest.raises(SecretsLoadError, match="no se pudo leer"):
        load_secrets_into_env("prisma/test", client=client, environ={})


@pytest.mark.parametrize("secret", ["no-es-json", json.dumps(["lista"]), {"KEY": 123}])
def test_secreto_con_formato_invalido_corta_el_arranque(secret: Any) -> None:
    client = FakeSecretsClient(secret)
    env: dict[str, str] = {}

    with pytest.raises(SecretsLoadError):
        load_secrets_into_env("prisma/test", client=client, environ=env)
    assert env == {}


def test_main_api_carga_secretos_antes_de_la_config(monkeypatch) -> None:
    orden: list[str] = []
    monkeypatch.setattr(main_api, "load_secrets_from_env_config", lambda: orden.append("secrets"))

    def _load_config() -> dict[str, Any]:
        orden.append("config")
        raise SystemExit  # cortamos acá: no hace falta llegar a uvicorn

    monkeypatch.setattr(main_api, "load_config", _load_config)

    with pytest.raises(SystemExit):
        main_api.main()
    assert orden == ["secrets", "config"]


def test_main_api_no_arranca_si_el_secreto_falla(monkeypatch) -> None:
    monkeypatch.setenv(secrets_manager_env.SECRET_ID_ENV, "prisma/test")
    monkeypatch.setattr(
        secrets_manager_env.boto3,
        "client",
        lambda *a, **kw: FakeSecretsClient(error_code="ResourceNotFoundException"),
    )
    monkeypatch.setattr(main_api, "load_config", lambda: pytest.fail("no debió leer la config"))

    with pytest.raises(SecretsLoadError):
        main_api.main()
