from .config import Settings
from .models import ChatRequest


def request_difficulty(request: ChatRequest, request_class: str, settings: Settings) -> str:
    metadata = request.metadata or {}
    explicit = metadata.get("difficulty")
    if explicit in {"easy", "standard", "hard"}:
        return str(explicit)

    prompt_words = sum(len(str(message.content).split()) for message in request.messages)
    if request_class in settings.easy_request_classes and prompt_words <= settings.easy_prompt_max_words:
        return "easy"
    return "standard"


def tiered_model(request: ChatRequest, request_class: str, settings: Settings) -> str:
    difficulty = request_difficulty(request, request_class, settings)
    if difficulty == "easy":
        return settings.cheap_model
    return request.model or settings.default_model
