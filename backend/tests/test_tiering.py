from app.config import Settings
from app.models import ChatMessage, ChatRequest
from app.providers import _available_routes, _litellm_payload
from app.tiering import request_difficulty, tiered_model


def test_easy_request_uses_cheap_model() -> None:
    settings = Settings(cheap_model="cheap-small", easy_request_classes="classification")
    request = ChatRequest(
        model="expensive-large",
        messages=[ChatMessage(role="user", content="classify this")],
        metadata={"request_class": "classification", "difficulty": "easy"},
    )

    assert request_difficulty(request, "classification", settings) == "easy"
    assert tiered_model(request, "classification", settings) == "cheap-small"


def test_non_easy_request_keeps_requested_model() -> None:
    settings = Settings(cheap_model="cheap-small", easy_request_classes="classification")
    request = ChatRequest(
        model="expensive-large",
        messages=[ChatMessage(role="user", content="write a detailed migration plan " * 50)],
        metadata={"difficulty": "hard"},
    )

    assert request_difficulty(request, "long_form_generation", settings) == "hard"
    assert tiered_model(request, "long_form_generation", settings) == "expensive-large"


def test_provider_routes_receive_tiered_model_for_easy_requests() -> None:
    settings = Settings(
        cheap_model="cheap-small",
        classification_provider_preference="openai",
        easy_request_classes="classification",
        openai_api_key="test-openai-key",
    )
    request = ChatRequest(
        model="expensive-large",
        messages=[ChatMessage(role="user", content="classify this")],
    )

    routes = _available_routes(settings, request, "classification")

    assert routes[0].name == "openai"
    assert routes[0].model == "cheap-small"


def test_openai_compatible_api_base_is_passed_to_litellm() -> None:
    settings = Settings(openai_api_key="test-openai-key", openai_api_base="http://localhost:11434/v1")
    request = ChatRequest(
        model="local-model",
        messages=[ChatMessage(role="user", content="hello")],
    )

    route = _available_routes(settings, request, "classification")[0]
    payload = _litellm_payload(request, route, settings)

    assert payload["api_base"] == "http://localhost:11434/v1"
    assert payload["api_key"] == "test-openai-key"
    assert payload["model"] == settings.cheap_model
