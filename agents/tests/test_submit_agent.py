"""Focused tests for the hackathon submission agent."""

from fastapi.testclient import TestClient

from agents.api_client import ApiClient
from submit.agent import ConservativeAgent
from world.api import create_app
from world.config import load_config
from world.sim import World


def test_submit_agent_completes_short_game_and_preserves_cash(monkeypatch) -> None:
    monkeypatch.setenv("GAME_DAYS", "90")
    world = World(config=load_config(), seed_starter_grid=True)
    api = ApiClient(transport=TestClient(create_app(world=world)))

    ConservativeAgent(api, seed=42).play_game()

    assert world.day == 90
    assert world.state.treasury > 100_000
    assert world.state.population >= 90
    assert any(tile.type == "solar_farm" for tile in world.state.tiles)


def test_submit_agent_builds_demand_response_hub_in_mature_city(monkeypatch) -> None:
    monkeypatch.setenv("GAME_DAYS", "730")
    world = World(config=load_config(), seed_starter_grid=True)
    api = ApiClient(transport=TestClient(create_app(world=world)))

    ConservativeAgent(api, seed=42).play_game()

    assert any(tile.type == "demand_response_hub" for tile in world.state.tiles)
