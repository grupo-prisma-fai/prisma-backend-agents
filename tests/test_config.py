"""Tests de la carga de configuración (prefijo S3 multi-tenant + keys por agente)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from westfield_agent_back_python.shared.config import load_config

BASE_YAML = """
project:
  name: test
s3:
  bucket: test-bucket
  prefix: org={university_code}/agents
"""


def _write_configs(tmp_path: Path, base: str = BASE_YAML) -> str:
    (tmp_path / "base.yaml").write_text(base, encoding="utf-8")
    return str(tmp_path)


def test_prefix_template_se_preserva_para_resolver_por_request(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("S3_PREFIX", raising=False)

    cfg = load_config(_write_configs(tmp_path))
    # El placeholder NO se sustituye al cargar — lo resuelve el registry
    # por request con el segmento de la ruta.
    assert cfg["s3"]["prefix"] == "org={university_code}/agents"


def test_s3_prefix_por_env_pisa_el_yaml(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("S3_PREFIX", "layout/custom")

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["s3"]["prefix"] == "layout/custom"


def test_prefix_default_es_la_convencion_multitenant(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("S3_PREFIX", raising=False)

    base = BASE_YAML.replace("  prefix: org={university_code}/agents\n", "")
    cfg = load_config(_write_configs(tmp_path, base))
    assert cfg["s3"]["prefix"] == "org={university_code}/agents"


# --------------------------------------------- keys dedicadas por agente


def _limpiar_keys_de_agente(monkeypatch) -> None:
    """Aísla del entorno real: el dev puede tener sus propias OPENAI_API_KEY_*."""
    for name in [n for n in os.environ if n.startswith("OPENAI_API_KEY_")]:
        monkeypatch.delenv(name, raising=False)


def test_keys_por_agente_se_recolectan_del_entorno(tmp_path, monkeypatch) -> None:
    _limpiar_keys_de_agente(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY_MAIA", "sk-maia")
    monkeypatch.setenv("OPENAI_API_KEY_STUDENT_SERVICES", "sk-ss")

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["openai"]["agent_api_keys"] == {
        "MAIA": "sk-maia",
        "STUDENT_SERVICES": "sk-ss",
    }


def test_key_global_no_se_cuela_como_key_de_agente(tmp_path, monkeypatch) -> None:
    _limpiar_keys_de_agente(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global")

    cfg = load_config(_write_configs(tmp_path))
    # La global vive en su propia key del config, no en el mapa por agente.
    assert cfg["openai"]["api_key"] == "sk-global"
    assert cfg["openai"]["agent_api_keys"] == {}


def test_otras_vars_openai_no_se_confunden_con_keys_de_agente(tmp_path, monkeypatch) -> None:
    _limpiar_keys_de_agente(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.interno/v1")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL_FALLBACK", "text-embedding-3-large")

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["openai"]["agent_api_keys"] == {}
    assert cfg["openai"]["base_url"] == "https://proxy.interno/v1"


def test_keys_por_agente_vacias_se_ignoran(tmp_path, monkeypatch) -> None:
    _limpiar_keys_de_agente(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY_MAIA", "   ")
    monkeypatch.setenv("OPENAI_API_KEY_WESTY", "sk-westy")

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["openai"]["agent_api_keys"] == {"WESTY": "sk-westy"}


# ------------------------------------------------------ workers uvicorn


def test_workers_default_es_uno(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["service"]["workers"] == 1


def test_workers_del_yaml(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    cfg = load_config(_write_configs(tmp_path, BASE_YAML + "service:\n  workers: 3\n"))
    assert cfg["service"]["workers"] == 3


def test_web_concurrency_pisa_el_yaml(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "4")

    cfg = load_config(_write_configs(tmp_path, BASE_YAML + "service:\n  workers: 3\n"))
    assert cfg["service"]["workers"] == 4


def test_web_concurrency_menor_a_uno_se_acota(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "0")

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["service"]["workers"] == 1


@pytest.mark.parametrize("raw", ["", "   "])
def test_web_concurrency_vacia_cuenta_como_no_seteada(tmp_path, monkeypatch, raw) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", raw)

    cfg = load_config(_write_configs(tmp_path, BASE_YAML + "service:\n  workers: 3\n"))
    assert cfg["service"]["workers"] == 3


@pytest.mark.parametrize("raw", ["abc", "2.5", "dos"])
def test_web_concurrency_invalida_degrada_al_yaml_sin_romper(tmp_path, monkeypatch, raw) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", raw)

    cfg = load_config(_write_configs(tmp_path, BASE_YAML + "service:\n  workers: 3\n"))
    assert cfg["service"]["workers"] == 3


def test_web_concurrency_con_espacios_se_normaliza(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", " 4 ")

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["service"]["workers"] == 4


def test_workers_invalido_en_yaml_degrada_a_uno(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    cfg = load_config(_write_configs(tmp_path, BASE_YAML + "service:\n  workers: muchos\n"))
    assert cfg["service"]["workers"] == 1


# ------------------------------------------------------ ENV normalizado


@pytest.mark.parametrize(("raw", "esperado"), [("Prod", "prod"), (" DEV ", "dev"), ("", "dev")])
def test_env_se_normaliza_una_sola_vez(tmp_path, monkeypatch, raw, esperado) -> None:
    monkeypatch.setenv("ENV", raw)

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["project"]["env"] == esperado


def test_env_en_mayusculas_carga_el_yaml_del_entorno(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENV", "Prod")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    (tmp_path / "prod.yaml").write_text("service:\n  workers: 2\n", encoding="utf-8")

    cfg = load_config(_write_configs(tmp_path))
    assert cfg["service"]["workers"] == 2


def test_env_ausente_es_dev(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ENV", raising=False)

    cfg = load_config(
        _write_configs(tmp_path, BASE_YAML.replace("name: test", "name: test\n  env: prod"))
    )
    # La fuente de verdad es ENV (default dev), igual que para elegir el <env>.yaml.
    assert cfg["project"]["env"] == "dev"
