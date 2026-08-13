"""Minimal ReAct loop for Search as Code."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from .client import LLMResponse, ModelClient, ToolCall
from .tool_sac import SacRunTool


@dataclass(frozen=True)
class ReactConfig:
    max_turns: int = 32
    timeout_seconds: float = 1_800.0
    invalid_response_retries: int = 2


@dataclass(frozen=True)
class AgentResult:
    answer: str
    messages: list[dict[str, Any]]
    turns: int
    termination: str


class ReactAgent:
    def __init__(
        self,
        client: ModelClient | Any | None = None,
        tool: SacRunTool | None = None,
        config: ReactConfig | None = None,
    ) -> None:
        self.client = client or ModelClient()
        self.tool = tool or SacRunTool()
        self.config = config or ReactConfig()

    def _system_prompt(self) -> str:
        return (
            "You are a deep research assistant. Use fresh evidence and investigate the "
            "question thoroughly. Do not answer from memory when research is required.\n\n"
            f"{self.tool.system_prompt_addendum}\n\nCurrent date: {date.today().isoformat()}"
        )

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": response.text or None,
        }
        if response.reasoning:
            message["reasoning"] = response.reasoning
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments,
                    },
                }
                for call in response.tool_calls
            ]
        return message

    async def _execute(self, call: ToolCall) -> dict[str, Any]:
        if call.name != self.tool.name:
            content = f"Unknown tool {call.name!r}; the only available tool is {self.tool.name!r}."
        else:
            content = await self.tool.call(call.arguments)
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": content,
        }

    async def arun(self, question: str) -> AgentResult:
        if not question.strip():
            raise ValueError("question must not be empty")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": question.strip()},
        ]
        invalid_responses = 0
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                for turn in range(1, self.config.max_turns + 1):
                    response = await self.client.complete(
                        messages=messages,
                        tools=[self.tool.schema],
                    )
                    messages.append(self._assistant_message(response))

                    if response.tool_calls:
                        invalid_responses = 0
                        for call in response.tool_calls:
                            messages.append(await self._execute(call))
                        continue

                    if response.text.strip():
                        return AgentResult(response.text, messages, turn, "answer")

                    invalid_responses += 1
                    if invalid_responses > self.config.invalid_response_retries:
                        return AgentResult("", messages, turn, "invalid_response")
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Call sac_run to continue researching, or provide the final "
                                "answer as your entire response."
                            ),
                        }
                    )
        except TimeoutError:
            return AgentResult("", messages, max(0, (len(messages) - 2) // 2), "timeout")
        finally:
            await self.tool.aclose()

        return AgentResult("", messages, self.config.max_turns, "max_turns")

    def run(self, question: str) -> AgentResult:
        async def run_once() -> AgentResult:
            try:
                return await self.arun(question)
            finally:
                await self.aclose()

        return asyncio.run(run_once())

    async def aclose(self) -> None:
        await self.tool.aclose()
        close = getattr(self.client, "aclose", None)
        if callable(close):
            await close()
