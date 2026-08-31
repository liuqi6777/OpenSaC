from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ._diagnostics import error_info, record_external_failures
from .transport import BrokerError


@dataclass(frozen=True, slots=True)
class _ManySuccess[ItemT, ResultT]:
    input_index: int
    item: ItemT
    value: ResultT


@dataclass(frozen=True, slots=True)
class _ManyFailure[ItemT]:
    input_index: int
    item: ItemT
    error: BrokerError
    info: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ManyReport[ItemT, ResultT]:
    outcomes: list[_ManySuccess[ItemT, ResultT] | _ManyFailure[ItemT]]

    @property
    def failures(self) -> list[_ManyFailure[ItemT]]:
        return [outcome for outcome in self.outcomes if isinstance(outcome, _ManyFailure)]

    @property
    def success_count(self) -> int:
        return len(self.outcomes) - len(self.failures)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    def record_failures(
        self,
        method: str,
        *,
        detail: Callable[[_ManyFailure[ItemT]], dict[str, Any]],
    ) -> None:
        """Persist one bounded warning using resource-specific item context."""

        record_external_failures(
            method,
            success_count=self.success_count,
            failures=[detail(failure) for failure in self.failures],
        )


def _run_many[ItemT, ResultT](
    items: Sequence[ItemT],
    *,
    concurrency: int,
    call: Callable[[ItemT], ResultT],
) -> _ManyReport[ItemT, ResultT]:
    """Call one synchronous capability per item and return an aligned failure report."""

    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if not items:
        return _ManyReport([])

    def one(indexed_item: tuple[int, ItemT]) -> _ManySuccess[ItemT, ResultT] | _ManyFailure[ItemT]:
        input_index, item = indexed_item
        try:
            return _ManySuccess(input_index, item, call(item))
        except BrokerError as error:
            return _ManyFailure(input_index, item, error, error_info(error))

    indexed_items = list(enumerate(items))
    if len(indexed_items) == 1:
        return _ManyReport([one(indexed_items[0])])

    workers = min(len(indexed_items), concurrency)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="opensac-sdk") as executor:
        return _ManyReport(list(executor.map(one, indexed_items)))
