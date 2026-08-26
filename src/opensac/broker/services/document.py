from __future__ import annotations

from pydantic import TypeAdapter

from opensac.backends.document import (
    DocumentBackend,
    DocumentContent,
    DocumentHandle,
    DocumentSourceKind,
)
from opensac.broker.providers.execution import ProviderExecutor
from opensac.broker.session import BrokerSession
from opensac.broker.sources import document_identity
from opensac.provider import ProviderRequestError, ProviderRuntime, invalid_provider_response

from .base import ServiceExecution

_DOCUMENT_HANDLES = TypeAdapter(list[DocumentHandle])


class DocumentService(ServiceExecution):
    """Reusable document-fetch service with source behavior owned by its adapter."""

    component = "document"
    resource_failures = True

    def __init__(
        self,
        route: str,
        backend: DocumentBackend,
        providers: ProviderExecutor,
        runtime: ProviderRuntime,
        *,
        backend_revision: str,
    ) -> None:
        super().__init__(backend, providers, runtime)
        self.route = route
        self.backend_revision = backend_revision

    def document_fingerprint(self, handle: DocumentHandle) -> str:
        """Identify one logical document independently of provider fallbacks."""
        return self.fingerprint(
            {
                "backend": self.route,
                "revision": self.backend_revision,
                "identity": document_identity(self.route, handle),
            }
        )

    def _candidate_request_value(
        self,
        handle: DocumentHandle,
        candidate: DocumentHandle,
    ) -> dict[str, object]:
        return {
            "backend": self.route,
            "revision": self.backend_revision,
            "identity": document_identity(self.route, handle),
            "representation": candidate.representation,
        }

    @property
    def source_kind(self) -> DocumentSourceKind:
        return self.backend.source_kind

    def fetch_candidates(self, handle: DocumentHandle) -> list[DocumentHandle]:
        try:
            candidates = _DOCUMENT_HANDLES.validate_python(
                self.backend.fetch_candidates(handle),
                strict=True,
            )
        except ProviderRequestError:
            raise
        except Exception as exc:
            raise invalid_provider_response() from exc
        if not candidates or any(candidate.source != handle.source for candidate in candidates):
            raise invalid_provider_response()
        return candidates

    async def fetch(
        self,
        state: BrokerSession,
        handle: DocumentHandle,
        candidate: DocumentHandle,
        *,
        query: str | None,
        request_index: int,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> DocumentContent:
        async def request() -> DocumentContent:
            result = await self.backend.fetch(candidate, query=query)
            content = DocumentContent.model_validate(result)
            if content.source != handle.source:
                raise ValueError("backend changed the requested content source")
            return content

        validate_fetch = getattr(self.backend, "preflight_fetch", None)

        def preflight() -> None:
            if callable(validate_fetch):
                validate_fetch(candidate)
            state.policy.record_content_backend_fetches(1)

        return await self.run(
            state,
            request_indexes=[request_index],
            request_value=self._candidate_request_value(handle, candidate),
            request=request,
            preflight=preflight,
            request_id=request_id,
            track_execution=track_execution,
        )
