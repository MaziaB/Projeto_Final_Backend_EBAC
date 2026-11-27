import json
import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import httpx
from pydantic import BaseModel
import redis
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

URL = os.getenv("URL").rstrip("/")
MEU_USUARIO = os.getenv("MEU_USUARIO")
MINHA_SENHA = os.getenv("MINHA_SENHA")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
CACHE_TTL_SECONDS = 300

app = FastAPI(
    title="API de comunicação com Pokeapi",
    description="API para compartilhamento de dados com a Pokeapi",
    version="0.1.0",
)

security = HTTPBasic()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=0,
    decode_responses=True,
)


class PokeCache(Base):
    __tablename__ = "Cache_Pokemon"

    cache_key = Column(String, primary_key=True)
    data = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=True)


class PokemonDB(Base):
    __tablename__ = "pokemon"

    pokemon_id = Column(Integer, primary_key=True, index=True)
    nome_pokemon = Column(String, index=True, nullable=False)
    altura_pokemon = Column(Float, nullable=True)
    peso_pokemon = Column(Float, nullable=True)
    tipo_pokemon = Column(String, index=True, nullable=False)


class Pokemon(BaseModel):
    pokemon_id: int
    nome_pokemon: str
    altura_pokemon: Optional[float] = None
    peso_pokemon: Optional[float] = None
    tipo_pokemon: str


Base.metadata.create_all(bind=engine)


def sessao_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def autenticacao_de_usuario(credentials: HTTPBasicCredentials = Depends(security)):
    usuario_correto = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        MEU_USUARIO.encode("utf-8"),
    )
    senha_correta = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        MINHA_SENHA.encode("utf-8"),
    )

    if not (usuario_correto and senha_correta):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuário ou senha incorretos!",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials


def salvar_pokemon_redis(pokemon_id: int, pokemon: Pokemon, ttl: int = CACHE_TTL_SECONDS):
    redis_client.setex(
        f"pokemon:{pokemon_id}",
        ttl,
        json.dumps(pokemon.model_dump()),
    )


def deletar_pokemon_redis(pokemon_id: int):
    redis_client.delete(f"pokemon:{pokemon_id}")


@app.get("/")
def hello_world():
    return {"Hello": "World"}


@app.get("/debug/redis")
def ver_pokemons_redis(_: HTTPBasicCredentials = Depends(autenticacao_de_usuario)):
    chaves = redis_client.keys("pokemon:*")
    pokemons = []

    for chave in chaves:
        valor = redis_client.get(chave)
        ttl = redis_client.ttl(chave)

        try:
            valor_parseado = json.loads(valor) if valor is not None else None
        except json.JSONDecodeError:
            valor_parseado = valor

        pokemons.append({
            "chave": chave,
            "valor": valor_parseado,
            "ttl": ttl,
        })

    return pokemons


@app.get("/pokemons")
async def get_pokemons(
    page: int = 1,
    limit: int = 20,
    _: HTTPBasicCredentials = Depends(autenticacao_de_usuario),
):
    if page < 1 or limit < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="page ou limit com valores inválidos!",
        )

    offset = (page - 1) * limit
    cache_key = f"pokemons:page={page}&limit={limit}"

    cached_data = redis_client.get(cache_key)
    if cached_data:
        return json.loads(cached_data)

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{URL}?offset={offset}&limit={limit}")
        response.raise_for_status()
        api_data = response.json()

    response_payload = {
        "page": page,
        "limit": limit,
        "offset": offset,
        "total": api_data.get("count", 0),
        "next": f"/pokemons?page={page + 1}&limit={limit}" if api_data.get("next") else None,
        "previous": f"/pokemons?page={page - 1}&limit={limit}" if page > 1 else None,
        "pokemons": api_data.get("results", []),
    }

    redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(response_payload))
    return response_payload


@app.get("/pokemons/{pokemon_id}")
async def get_pokemon_id(
    pokemon_id: int,
    db: Session = Depends(sessao_db),
    _: HTTPBasicCredentials = Depends(autenticacao_de_usuario),
):
    cache_key = f"pokemon:{pokemon_id}"
    cached = redis_client.get(cache_key)

    if cached:
        return json.loads(cached)

    async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(URL)
            response.raise_for_status()
            payload = response.json()

    pokemon = db.query(PokemonDB).filter(PokemonDB.pokemon_id == pokemon_id).first()
    if not pokemon:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Id não cadastrado!",
        )

    payload = Pokemon(
        pokemon_id=pokemon.pokemon_id,
        nome_pokemon=pokemon.nome_pokemon,
        altura_pokemon=pokemon.altura_pokemon,
        peso_pokemon=pokemon.peso_pokemon,
        tipo_pokemon=pokemon.tipo_pokemon,
    )
    salvar_pokemon_redis(pokemon_id, payload)
    return payload.model_dump()
