import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# 1. Define variáveis de ambiente ANTES de importar o módulo da aplicação
os.environ.setdefault("URL", "https://pokeapi.co/api/v2/pokemon")
os.environ.setdefault("MEU_USUARIO", "admin")
os.environ.setdefault("MINHA_SENHA", "secret123")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as api_module
from app import Base, PokemonDB, app, autenticacao_de_usuario, sessao_db


# ---------------------------------------------------------------------------
# STUBS: objetos substitutos simples que fornecem respostas previsíveis
# ---------------------------------------------------------------------------

class StubRedis:
    """Stub de Redis em memória para simular cache hit/miss e TTL."""

    def __init__(self, initial_data: dict[str, str] | None = None):
        self.store: dict[str, str] = dict(initial_data or {})
        self.saved_ttl: dict[str, int] = {}

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value
        self.saved_ttl[key] = ttl
        return True

    def delete(self, key: str):
        return self.store.pop(key, None) is not None


class StubHTTPResponse:
    """Stub de resposta HTTP compatível com o uso em httpx."""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def make_stub_async_client(response: StubHTTPResponse):
    """Cria um stub de httpx.AsyncClient compatível com `async with`."""
    get_mock = AsyncMock(return_value=response)

    client_instance = MagicMock()
    client_instance.get = get_mock
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    client_cls = MagicMock(return_value=client_instance)
    return client_cls, get_mock


# ---------------------------------------------------------------------------
# FIXTURES: banco SQLite em memória, overrides de dependências e TestClient
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def stub_redis(monkeypatch):
    stub = StubRedis()
    monkeypatch.setattr(api_module, "redis_client", stub)
    return stub


@pytest.fixture()
def client(db_session, stub_redis):
    def _override_db():
        yield db_session

    def _override_auth():
        return SimpleNamespace(username="admin")

    app.dependency_overrides[sessao_db] = _override_db
    app.dependency_overrides[autenticacao_de_usuario] = _override_auth

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# TESTES DE GET /pokemons
# ---------------------------------------------------------------------------

def test_get_pokemons_parametros_invalidos_retorna_400(client):
    res = client.get("/pokemons?page=0&limit=20")

    assert res.status_code == 400
    assert res.json()["detail"] == "page ou limit com valores inválidos!"


def test_get_pokemons_cache_hit_nao_chama_http(client, stub_redis, monkeypatch):
    cached_payload = {
        "page": 1,
        "limit": 2,
        "offset": 0,
        "total": 1302,
        "next": "/pokemons?page=2&limit=2",
        "previous": None,
        "pokemons": [{"name": "bulbasaur", "url": "https://pokeapi.co/api/v2/pokemon/1/"}],
    }
    stub_redis.store["pokemons:page=1&limit=2"] = json.dumps(cached_payload)

    # Mock que explode se o código tentar abrir httpx.AsyncClient
    async_client_mock = MagicMock(side_effect=AssertionError("Não deveria chamar API externa"))
    monkeypatch.setattr(api_module.httpx, "AsyncClient", async_client_mock)

    res = client.get("/pokemons?page=1&limit=2")

    assert res.status_code == 200
    assert res.json() == cached_payload
    async_client_mock.assert_not_called()


def test_get_pokemons_cache_miss_busca_api_e_salva_no_redis(client, stub_redis, monkeypatch):
    pokeapi_stub_response = StubHTTPResponse(
        payload={
            "count": 4,
            "next": "https://pokeapi.co/api/v2/pokemon?offset=2&limit=2",
            "previous": None,
            "results": [
                {"name": "bulbasaur", "url": "https://pokeapi.co/api/v2/pokemon/1/"},
                {"name": "ivysaur", "url": "https://pokeapi.co/api/v2/pokemon/2/"},
            ],
        }
    )
    client_cls_mock, get_mock = make_stub_async_client(pokeapi_stub_response)
    monkeypatch.setattr(api_module.httpx, "AsyncClient", client_cls_mock)

    response = client.get("/pokemons?page=1&limit=2")

    assert response.status_code == 200
    assert response.json() == {
        "page": 1,
        "limit": 2,
        "offset": 0,
        "total": 4,
        "next": "/pokemons?page=2&limit=2",
        "previous": None,
        "pokemons": [
            {"name": "bulbasaur", "url": "https://pokeapi.co/api/v2/pokemon/1/"},
            {"name": "ivysaur", "url": "https://pokeapi.co/api/v2/pokemon/2/"},
        ],
    }

    # Verifica mock da chamada HTTP e gravação no stub do Redis
    get_mock.assert_awaited_once_with(f"{api_module.URL}?offset=0&limit=2")
    assert "pokemons:page=1&limit=2" in stub_redis.store
    assert stub_redis.saved_ttl["pokemons:page=1&limit=2"] == api_module.CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# TESTES DE GET /pokemons/{pokemon_id}
# ---------------------------------------------------------------------------

def test_get_pokemon_id_cache_hit_retorna_sem_consultar_db(client, stub_redis, monkeypatch):
    cached_pokemon = {
        "pokemon_id": 25,
        "nome_pokemon": "pikachu",
        "altura_pokemon": 0.4,
        "peso_pokemon": 6.0,
        "tipo_pokemon": "electric",
    }
    stub_redis.store["pokemon:25"] = json.dumps(cached_pokemon)

    async_client_mock = MagicMock(side_effect=AssertionError("Não deveria chamar API externa"))
    monkeypatch.setattr(api_module.httpx, "AsyncClient", async_client_mock)

    res = client.get("/pokemons/25")

    assert res.status_code == 200
    assert res.json() == cached_pokemon
    async_client_mock.assert_not_called()


def test_get_pokemon_id_existente_no_db_popula_redis(client, db_session, stub_redis, monkeypatch):
    db_session.add(
        PokemonDB(
            pokemon_id=1,
            nome_pokemon="bulbasaur",
            altura_pokemon=0.7,
            peso_pokemon=6.9,
            tipo_pokemon="grass",
        )
    )
    db_session.commit()

    client_cls_mock, get_mock = make_stub_async_client(StubHTTPResponse(payload={}))
    monkeypatch.setattr(api_module.httpx, "AsyncClient", client_cls_mock)

    res = client.get("/pokemons/1")

    expected = {
        "pokemon_id": 1,
        "nome_pokemon": "bulbasaur",
        "altura_pokemon": 0.7,
        "peso_pokemon": 6.9,
        "tipo_pokemon": "grass",
    }
    assert res.status_code == 200
    assert res.json() == expected

    get_mock.assert_awaited_once_with(api_module.URL)
    assert stub_redis.store["pokemon:1"] == json.dumps(expected)
    assert stub_redis.saved_ttl["pokemon:1"] == api_module.CACHE_TTL_SECONDS


def test_get_pokemon_id_inexistente_retorna_404(client, stub_redis, monkeypatch):
    client_cls_mock, get_mock = make_stub_async_client(StubHTTPResponse(payload={}))
    monkeypatch.setattr(api_module.httpx, "AsyncClient", client_cls_mock)

    res = client.get("/pokemons/9999")

    assert res.status_code == 404
    assert res.json()["detail"] == "Id não cadastrado!"
    assert "pokemon:9999" not in stub_redis.store
    get_mock.assert_awaited_once_with(api_module.URL)
