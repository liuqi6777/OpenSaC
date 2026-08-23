from __future__ import annotations

from typing import Any

from opensac.provider import invalid_provider_response


def json_object(response: Any) -> dict[str, Any]:
    """Decode one provider response and require a JSON object."""

    try:
        payload = response.json()
    except Exception as exc:
        raise invalid_provider_response() from exc
    if not isinstance(payload, dict):
        raise invalid_provider_response()
    return payload
