"""Run the minimal agent with ``python -m sac_agent 'question'``."""

from __future__ import annotations

import argparse
import asyncio

from opensac._optional import MissingOptionalDependency

from .react import ReactAgent, ReactConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal OpenSAC ReAct agent")
    parser.add_argument("question", help="Research question")
    parser.add_argument("--max-turns", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=1_800.0, help="Total seconds")
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    try:
        agent = ReactAgent(
            config=ReactConfig(max_turns=max(1, args.max_turns), timeout_seconds=args.timeout)
        )
    except MissingOptionalDependency as exc:
        raise SystemExit(str(exc)) from None
    try:
        result = await agent.arun(args.question)
        if result.answer:
            print(result.answer)
        else:
            raise SystemExit(f"Agent stopped without an answer: {result.termination}")
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
