from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from opensac.broker.capabilities.content import ContentLimits
from opensac.broker.capabilities.llm import LLMLimits
from opensac.broker.capabilities.search import SearchLimits

if TYPE_CHECKING:
    from opensac.config import Settings


class BrokerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    search: SearchLimits = Field(default_factory=SearchLimits)
    content: ContentLimits = Field(default_factory=ContentLimits)
    llm: LLMLimits = Field(default_factory=LLMLimits)
    max_context_payload_bytes: int = Field(default=200_000, ge=1)

    @classmethod
    def from_settings(cls, settings: Settings) -> BrokerConfig:
        """Translate deployment settings into broker-owned capability limits."""

        search = settings.capabilities.search
        content = settings.capabilities.content
        extraction = settings.capabilities.extraction
        return cls(
            search=SearchLimits(
                max_queries_per_request=search.max_queries_per_request,
                max_query_chars=search.max_query_chars,
                max_top_k=search.max_top_k,
            ),
            content=ContentLimits(
                max_sources_per_request=content.max_sources_per_request,
                url_admission=content.url_admission,
                batch_deadline_seconds=content.batch_deadline_seconds,
                session_cache_bytes=settings.session_content_cache_bytes,
            ),
            llm=LLMLimits(
                extract_max_instruction_bytes=extraction.max_instruction_bytes,
                extract_max_schema_bytes=extraction.max_schema_bytes,
                extract_max_item_bytes=extraction.max_item_bytes,
                extract_max_schema_depth=extraction.max_schema_depth,
                extract_max_repair_attempts=extraction.max_repair_attempts,
            ),
            max_context_payload_bytes=settings.max_context_payload_bytes,
        )
