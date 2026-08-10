from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from opensac.agent.code_parser import parse_action
from opensac.agent.prompts import SYSTEM_PROMPT
from opensac.models import Run, RunStatus, utc_now
from opensac.sandbox.base import Sandbox, SandboxRequest


class AgentController:
    def __init__(
        self,
        client: AsyncOpenAI,
        sandbox: Sandbox,
        *,
        default_model: str,
        temperature: float = 0.1,
        max_observation_chars: int = 30_000,
    ) -> None:
        self.client = client
        self.sandbox = sandbox
        self.default_model = default_model
        self.temperature = temperature
        self.max_observation_chars = max_observation_chars

    async def execute(self, run: Run, *, workspace, session_token: str, max_turns: int) -> Run:
        run.status = RunStatus.RUNNING
        run.updated_at = utc_now()
        user_request = run.input
        if run.output_schema:
            user_request += "\n\nThe final answer must conform to this JSON schema:\n" + json.dumps(
                run.output_schema, ensure_ascii=False
            )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_request},
        ]
        observed_citation_keys: set[str] = set()
        try:
            for turn in range(max_turns):
                response = await self.client.chat.completions.create(
                    model=run.model or self.default_model,
                    messages=messages,
                    temperature=self.temperature,
                )
                usage = response.usage
                if usage:
                    run.usage.model_tokens += usage.total_tokens
                content = response.choices[0].message.content or ""
                action = parse_action(content)
                run.trace.append({"turn": turn + 1, "model_response": content})
                if action.final is not None:
                    citations = action.final.get("citations", [])
                    unknown = [
                        citation
                        for citation in citations
                        if not self._citation_keys(citation) & observed_citation_keys
                    ]
                    if unknown:
                        messages.extend(
                            [
                                {"role": "assistant", "content": content},
                                {
                                    "role": "user",
                                    "content": (
                                        "The final answer used citations that were not resolved "
                                        "by the sandbox. Use only citations from prior sandbox "
                                        "observations, or run more search code."
                                    ),
                                },
                            ]
                        )
                        continue
                    run.output = action.final.get("answer", action.final)
                    run.citations = citations
                    run.status = RunStatus.COMPLETED
                    run.updated_at = utc_now()
                    return run

                result = await self.sandbox.execute(
                    SandboxRequest(
                        code=action.code or "",
                        workspace=workspace,
                        session_token=session_token,
                        session_id=run.session_id,
                    )
                )
                run.usage.sandbox_seconds += result.duration_seconds
                for citation in result.citations:
                    observed_citation_keys.update(self._citation_keys(citation))
                observation: dict[str, Any] = {
                    "succeeded": result.succeeded,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "output": result.output,
                    "citations": result.citations,
                }
                serialized = json.dumps(observation, ensure_ascii=False, default=str)
                messages.extend(
                    [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": "Sandbox observation:\n"
                            + serialized[: self.max_observation_chars],
                        },
                    ]
                )
            raise RuntimeError(f"Agent exceeded its {max_turns}-turn limit")
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)
            run.updated_at = utc_now()
            return run

    @staticmethod
    def _citation_keys(citation: dict[str, Any]) -> set[str]:
        return {str(citation[key]) for key in ("ref", "url", "docid") if citation.get(key)}
