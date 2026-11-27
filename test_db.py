import pytest 
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import Base, PokemonDB, app
from fastapi.testclient import TestClient
import os

DATABASE_URL_TEST = "sqlite:///:memory:"
engine = create_engine(DATABASE_URL_TEST, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(bind=engine)

Base.metadata.create_all(bind=engine)

client = TestClient(app)

@pytest.fixture(autouse=True)
def mock_redis(mocker):
    mock_redis_client = mocker.patch("app.redis_client", autospec=True)
    mock_redis_client.get.return_value = None

@pytest.fixture(scope="function")
def db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

def test_get_pokemons(db, mocker):
    response = client.get("/pokemons", auth=("admin", "admin"))
    assert response.status_code == 200

    data = response.json()

    assert len(data["pokemons"]) == 20
    assert data["pokemons"][0]["nome_pokemon"] == "Pikachu"
    assert data["pokemons"][0]["altura_pokemon"] == 70.0
    assert data["pokemons"][0]["peso_pokemon"] == 30.0
    assert data["pokemons"][0]["tipo_pokemon"] == "Elétrico"



# CÓDIGO QUE PODE SER USADO PARA TESTAR O ENDPOINT GET
#@app.get("/pokemons")
#async def get_pokemons():

#   data = await get_external_data()

#    return data
