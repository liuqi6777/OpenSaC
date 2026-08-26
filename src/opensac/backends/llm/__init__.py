"""Language model backend contracts and adapters."""

from .base import ClosableLLMBackend, LLMBackend, LLMResponse
from .openai import OpenAICompatibleBackend

__all__ = [
    "ClosableLLMBackend",
    "LLMBackend",
    "LLMResponse",
    "OpenAICompatibleBackend",
]
