from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import resource
import socket
import sys
import tempfile
import time
import uuid
import weakref
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from opensac import __version__
from opensac.api.errors import (
    ExecIdConflictError,
    ExecIndeterminateError,
    SessionCapacityError,
    SessionCleanupError,
    SessionClosingError,
    SessionCreateConflictError,
    SessionExpiredError,
    SessionLostError,
    WorkerDrainingError,
)
from opensac.backends.rerank.jina import JinaPassageReranker
from opensac.backends.search.local_http import LocalSearchBackend
from opensac.backends.search.serper import SerperBackend
from opensac.broker import (
    BrokerRuntime,
    BrokerService,
    ProviderExecutionConfig,
    resolve_broker_socket_path,
)
from opensac.broker.session import BrokerSession
from opensac.config import Settings
from opensac.metrics import CapacityGate
from opensac.models import (
    CAPABILITY_CONTRACT,
    CapabilityEvent,
    ExecCreate,
    ExecRecord,
    ExecRecordStatus,
    ExecResult,
    ExecutionWarning,
    Mechanisms,
    ProgramRecord,
    RunUsage,
    Session,
    SessionCreate,
    utc_now,
)
from opensac.provider import ProviderPolicy, ProviderRuntime
from opensac.sandbox import (
    SANDBOX_CONTRACT,
    DockerSandbox,
    UnsafeCodeError,
    WarmDockerSandbox,
)
from opensac.sandbox.base import SandboxRequest, SandboxResult
from opensac.store import StateStore

logger = logging.getLogger(__name__)


class ApplicationRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = StateStore(settings.data_dir)
        identity_seed = f"{socket.gethostname()}|{settings.data_dir.resolve()}"
        self.worker_id = settings.worker_id or (
            f"{socket.gethostname()}-{hashlib.sha256(identity_seed.encode()).hexdigest()[:10]}"
        )
        self.worker_epoch = uuid.uuid4().hex
        self.started_monotonic = time.monotonic()
        self.accepting = True
        self.session_create_lock = asyncio.Lock()
        self.session_lifecycle_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self.model_client = AsyncOpenAI(
            api_key=settings.model_api_key or "not-configured",
            base_url=settings.model_base_url,
        )
        provider_operations = list(
            ("local.search", "local.document")
            if settings.search_backend == "local"
            else ("web.search", "web.scrape")
        )
        if settings.passage_ranker == "jina":
            provider_operations.append("web.rerank")
        self.provider_operations = tuple(provider_operations)
        provider_policies = {
            operation: ProviderPolicy(
                retry_profile=settings.provider_retry_profile,
                max_attempts=settings.provider_max_attempts,
                attempt_timeout_seconds=settings.provider_operation_attempt_timeout_seconds.get(
                    operation, settings.provider_attempt_timeout_seconds
                ),
                logical_deadline_seconds=settings.provider_operation_logical_deadline_seconds.get(
                    operation, settings.provider_logical_deadline_seconds
                ),
                base_backoff_seconds=settings.provider_base_backoff_seconds,
                max_backoff_seconds=settings.provider_max_backoff_seconds,
                max_total_backoff_seconds=settings.provider_max_total_backoff_seconds,
                max_retry_after_seconds=settings.provider_max_retry_after_seconds,
                concurrency=settings.provider_operation_concurrency.get(
                    operation,
                    (
                        settings.max_concurrency
                        if operation.endswith(".search")
                        else 2
                        if operation == "web.rerank"
                        else settings.backend_fetch_concurrency
                    ),
                ),
                requests_per_second=settings.provider_operation_requests_per_second.get(operation),
                burst=settings.provider_operation_burst.get(operation),
            )
            for operation in self.provider_operations
        }
        provider_runtime = ProviderRuntime(provider_policies)
        if settings.search_backend == "local":
            search_backend = LocalSearchBackend(
                settings.local_search_base_url,
                timeout=max(
                    provider_policies["local.search"].attempt_timeout_seconds,
                    provider_policies["local.document"].attempt_timeout_seconds,
                ),
            )
        else:
            search_backend = SerperBackend(
                settings.serper_api_key,
                jina_api_key=settings.jina_api_key,
                timeout=max(
                    provider_policies["web.search"].attempt_timeout_seconds,
                    provider_policies["web.scrape"].attempt_timeout_seconds,
                ),
            )
        passage_reranker = (
            JinaPassageReranker(
                api_key=settings.jina_api_key,
                model=settings.passage_reranker_model,
                timeout=provider_policies["web.rerank"].attempt_timeout_seconds,
            )
            if settings.passage_ranker == "jina"
            else None
        )
        self.broker = BrokerService(
            {settings.search_backend: search_backend},
            model_client=self.model_client if settings.model_name else None,
            extraction_model=settings.model_name,
            passage_reranker=passage_reranker,
            max_concurrency=settings.max_concurrency,
            max_context_payload_bytes=settings.max_context_payload_bytes,
            session_content_cache_bytes=settings.session_content_cache_bytes,
            max_search_queries_per_request=settings.search_max_queries_per_request,
            max_search_query_chars=settings.search_max_query_chars,
            max_search_top_k=settings.search_max_top_k,
            max_extract_items=settings.extract_max_items,
            max_extract_instruction_bytes=settings.extract_max_instruction_bytes,
            max_extract_schema_bytes=settings.extract_max_schema_bytes,
            max_extract_item_bytes=settings.extract_max_item_bytes,
            max_extract_total_item_bytes=settings.extract_max_total_item_bytes,
            max_extract_schema_depth=settings.extract_max_schema_depth,
            max_extract_repair_attempts=settings.extract_max_repair_attempts,
            max_content_sources_per_request=settings.content_max_sources_per_request,
            content_url_admission=settings.content_url_admission,
            content_batch_deadline_seconds=settings.content_batch_deadline_seconds,
            provider_execution_config=ProviderExecutionConfig(
                inflight_coalescing=settings.provider_inflight_coalescing,
                max_inflight_keys=settings.provider_max_inflight_keys,
                max_waiters_per_flight=settings.provider_max_waiters_per_key,
                result_cache_ttl_seconds=settings.provider_result_cache_ttl_seconds,
                result_cache_max_bytes=settings.provider_result_cache_max_bytes,
            ),
            provider_runtime=provider_runtime,
            backend_revision=settings.backend_revision,
        )
        broker_socket = resolve_broker_socket_path(settings.broker_socket)
        self.broker_runtime = BrokerRuntime(self.broker, broker_socket)
        sandbox_type = WarmDockerSandbox if settings.sandbox_mode == "warm" else DockerSandbox
        sandbox_kwargs = dict(
            image=settings.sandbox_image,
            broker_socket=broker_socket,
            docker_host_platform=settings.sandbox_docker_host_platform,
            timeout_seconds=settings.sandbox_timeout_seconds,
            memory=settings.sandbox_memory,
            cpus=settings.sandbox_cpus,
            pids_limit=settings.sandbox_pids_limit,
            max_output_bytes=settings.max_output_bytes,
        )
        if sandbox_type is WarmDockerSandbox:
            sandbox_kwargs["idle_timeout_seconds"] = settings.sandbox_warm_idle_seconds
            sandbox_kwargs["max_containers"] = settings.sandbox_max_warm_containers
        self.sandbox = sandbox_type(**sandbox_kwargs)
        self.sandbox_gate = CapacityGate(settings.sandbox_max_concurrency)
        # /exec is driven by an external harness that may have dozens of
        # rollouts in flight. Without a ceiling each in-flight tool call would
        # start its own container.
        self.session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.closing_sessions: set[str] = set()
        self.exec_tasks: set[asyncio.Task[ExecResult]] = set()
        self.inflight_execs: dict[tuple[str, str], tuple[str, asyncio.Task[ExecResult]]] = {}
        # A reservation is registered synchronously when /exec accepts work,
        # before the child task gets its first event-loop turn. DELETE can
        # then distinguish admitted work from a late request even when the task
        # has not acquired the session lock yet.
        self.session_tasks: dict[str, set[asyncio.Task[Any]]] = defaultdict(set)
        self._reaper_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.broker_runtime.start()
        reap_orphans = getattr(self.sandbox, "reap_orphans", None)
        if callable(reap_orphans):
            await reap_orphans()
        # A process may have died after persisting `closing=true` but before it
        # removed the directory. Finish that cleanup before accepting new work.
        for session in self.store.sessions():
            if session.closing:
                await self.close_session(session.id)
            elif session.worker_epoch != self.worker_epoch:
                await self.close_session(session.id, tombstone_reason="worker_restarted")
        # Always run it: a client may create a leased session even when the
        # deployment-wide TTL is disabled and no warm executor is configured.
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        self.accepting = False
        try:
            if self._reaper_task is not None:
                self._reaper_task.cancel()
                await asyncio.gather(self._reaper_task, return_exceptions=True)
                self._reaper_task = None
            for task in tuple(self.exec_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.exec_tasks, return_exceptions=True)
        finally:
            try:
                await self._close_sandbox()
            finally:
                try:
                    await self.broker_runtime.stop()
                finally:
                    await self.model_client.close()

    def _warm_reaper_enabled(self) -> bool:
        return self.settings.sandbox_warm_idle_seconds > 0 and callable(
            getattr(self.sandbox, "reap_idle", None)
        )

    async def _close_sandbox(self) -> None:
        close = getattr(self.sandbox, "close", None)
        if not callable(close):
            return
        try:
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.exception("sandbox_close_failed")

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.session_reaper_interval_seconds)
            try:
                await self.reap_expired_sessions()
                reap_idle = getattr(self.sandbox, "reap_idle", None)
                if self._warm_reaper_enabled() and callable(reap_idle):
                    await reap_idle(self.settings.sandbox_warm_idle_seconds)
                tombstone_ttl = self.settings.session_tombstone_ttl_seconds
                if tombstone_ttl > 0:
                    self.store.reap_tombstones(before=utc_now() - timedelta(seconds=tombstone_ttl))
            except Exception:
                # One corrupt or concurrently removed session must not disable
                # cleanup for every session created afterwards.
                logger.exception("session_reaper_failed")

    def environment_manifest(self) -> dict[str, Any]:
        try:
            package_version = importlib_metadata.version("opensac")
        except importlib_metadata.PackageNotFoundError:
            package_version = __version__
        sdk_capabilities = self.broker.capability_manifest(
            backend_name=self.settings.search_backend,
            mechanisms=Mechanisms(),
        )
        search_limits = sdk_capabilities["search"]["limits"]
        content_limits = sdk_capabilities["content"]["limits"]
        llm_limits = sdk_capabilities["llm"]["limits"]
        return {
            "opensac_version": package_version,
            "build_commit": self.settings.build_commit,
            "sandbox_image": self.settings.sandbox_image,
            "sandbox_image_digest": self.settings.sandbox_image_digest,
            "sandbox_contract": SANDBOX_CONTRACT,
            "capability_contract": CAPABILITY_CONTRACT,
            "sdk_capabilities": sdk_capabilities,
            "capability_limits": {
                "search": {
                    "max_queries_per_request": search_limits["max_queries_per_request"],
                    "max_query_chars": search_limits["max_query_chars"],
                    "max_top_k": search_limits["max_top_k"],
                },
                "extract_many": {
                    "max_items": llm_limits["extract_max_items"],
                    "max_instruction_bytes": llm_limits["extract_max_instruction_bytes"],
                    "max_schema_bytes": llm_limits["extract_max_schema_bytes"],
                    "max_item_bytes": llm_limits["extract_max_item_bytes"],
                    "max_total_item_bytes": llm_limits["extract_max_total_item_bytes"],
                    "max_schema_depth": llm_limits["extract_max_schema_depth"],
                    "max_repair_attempts": llm_limits["extract_max_repair_attempts"],
                },
                "content": {
                    "max_sources_per_request": content_limits["max_sources_per_request"],
                    "url_admission": sdk_capabilities["content"]["url_admission"],
                    "batch_deadline_seconds": self.settings.content_batch_deadline_seconds,
                    "passage_limit": content_limits["passage_limit"],
                    "passage_max_per_source": content_limits["passage_max_per_source"],
                    "passage_chunk_chars": self.broker.passage_chunk_chars,
                    "passage_chunk_overlap_chars": (self.broker.passage_chunk_overlap_chars),
                    "passage_prefilter_limit": self.broker.passage_prefilter_limit,
                },
                "inflight": {
                    "enabled": self.settings.provider_inflight_coalescing,
                    "max_keys": self.settings.provider_max_inflight_keys,
                    "max_waiters_per_key": self.settings.provider_max_waiters_per_key,
                },
                "provider_result_cache": {
                    "enabled": self.settings.provider_result_cache_ttl_seconds > 0,
                    "ttl_seconds": self.settings.provider_result_cache_ttl_seconds,
                    "max_bytes": self.settings.provider_result_cache_max_bytes,
                    "operations": ["web.search", "web.scrape"],
                },
            },
            "provider_policies": {
                operation: {
                    **asdict(self.broker.provider_runtime.policy_for(operation)),
                    "max_attempts": self.broker.provider_runtime.policy_for(
                        operation
                    ).effective_max_attempts,
                }
                for operation in self.provider_operations
            },
            "backend_revision": self.settings.backend_revision,
            "backend_metadata_hash": self.settings.backend_metadata_hash,
            "search_backend": self.settings.search_backend,
            "passage_ranker": self.settings.passage_ranker,
            "local_search_base_url": self.settings.local_search_base_url,
        }

    def process_snapshot(self) -> dict[str, float | int]:
        rss_bytes = 0
        statm = Path("/proc/self/statm")
        if statm.exists():
            try:
                resident_pages = int(statm.read_text(encoding="utf-8").split()[1])
                rss_bytes = resident_pages * os.sysconf("SC_PAGE_SIZE")
            except (OSError, ValueError, IndexError):
                rss_bytes = 0
        if rss_bytes == 0:
            max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_bytes = int(max_rss if sys.platform == "darwin" else max_rss * 1024)
        fd_root = Path("/proc/self/fd")
        if not fd_root.exists():
            fd_root = Path("/dev/fd")
        try:
            fd_count = len(list(fd_root.iterdir()))
        except OSError:
            fd_count = -1
        return {
            "rss_bytes": rss_bytes,
            "fd_count": fd_count,
            "uptime_seconds": time.monotonic() - self.started_monotonic,
        }

    @staticmethod
    def _session_request_hash(request: SessionCreate) -> str:
        payload = request.model_dump(mode="json", exclude={"request_id"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def get_session(self, session_id: str) -> Session:
        try:
            session = self.store.get_session(session_id)
        except KeyError:
            tombstone = self.store.get_tombstone(session_id)
            if tombstone is not None and tombstone.reason == "worker_restarted":
                raise SessionLostError(
                    f"Session '{session_id}' belonged to a prior worker epoch"
                ) from None
            if tombstone is not None and tombstone.reason == "session_expired":
                raise SessionExpiredError(f"Session '{session_id}' lease expired") from None
            raise
        if session.worker_epoch and session.worker_epoch != self.worker_epoch:
            raise SessionLostError(f"Session '{session_id}' belonged to a prior worker epoch")
        if session.lease_expires_at is not None and session.lease_expires_at <= utc_now():
            raise SessionExpiredError(f"Session '{session_id}' lease expired")
        return session

    async def create_session(self, request: SessionCreate) -> tuple[Session, bool]:
        if not self.accepting:
            raise WorkerDrainingError("Worker is draining")
        request_hash = self._session_request_hash(request)
        async with self.session_create_lock:
            if not self.accepting:
                raise WorkerDrainingError("Worker is draining")
            if request.request_id is not None:
                existing = self.store.find_session_by_request_id(request.request_id)
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise SessionCreateConflictError(
                            f"Session request id '{request.request_id}' was already used "
                            "with a different payload"
                        )
                    if existing.worker_epoch != self.worker_epoch:
                        raise SessionLostError(
                            f"Session request id '{request.request_id}' belongs to "
                            "a prior worker epoch"
                        )
                    if self._is_expired(existing, utc_now()):
                        raise SessionExpiredError(
                            f"Session request id '{request.request_id}' lease expired"
                        )
                    if existing.closing or existing.id in self.closing_sessions:
                        raise SessionClosingError(f"Session '{existing.id}' is closing")
                    return existing, False
                tombstone = self.store.find_tombstone_by_request_id(request.request_id)
                if tombstone is not None:
                    if tombstone.request_hash != request_hash:
                        raise SessionCreateConflictError(
                            f"Session request id '{request.request_id}' was already used "
                            "with a different payload"
                        )
                    if tombstone.reason == "worker_restarted":
                        raise SessionLostError(
                            f"Session request id '{request.request_id}' belongs to "
                            "a prior worker epoch"
                        )
                    raise SessionExpiredError(
                        f"Session request id '{request.request_id}' is no longer active"
                    )
            current_time = utc_now()
            active = sum(
                not session.closing and not self._is_expired(session, current_time)
                for session in self.store.sessions()
            )
            if self.settings.max_active_sessions and active >= self.settings.max_active_sessions:
                raise SessionCapacityError(
                    f"Worker session capacity {self.settings.max_active_sessions} is full"
                )
            default_lease = (
                self.settings.session_ttl_seconds if self.settings.session_ttl_seconds > 0 else None
            )
            session = self.store.create_session(
                request,
                backend=self.settings.search_backend,
                worker_id=self.worker_id,
                worker_epoch=self.worker_epoch,
                request_hash=request_hash,
                default_lease_seconds=default_lease,
                environment=self.environment_manifest(),
            )
            return session, True

    async def renew_session(self, session_id: str) -> Session:
        lock = self.session_locks[session_id]
        async with lock:
            session = self.get_session(session_id)
            if session.closing or session_id in self.closing_sessions:
                raise SessionClosingError(f"Session '{session_id}' is closing")
            return self.store.touch_session(session_id)

    @staticmethod
    def session_state(session: Session) -> str:
        if session.closing:
            return "closing"
        if session.terminal_reason:
            return "exhausted"
        return "active"

    def _reserve_session_task(self, session_id: str, task: asyncio.Task[Any]) -> None:
        self.session_tasks[session_id].add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            tasks = self.session_tasks.get(session_id)
            if tasks is None:
                return
            tasks.discard(finished)
            if not tasks:
                self.session_tasks.pop(session_id, None)

        task.add_done_callback(done)

    def _active_session_tasks(self, session_id: str) -> tuple[asyncio.Task[Any], ...]:
        return tuple(task for task in self.session_tasks.get(session_id, ()) if not task.done())

    def bind_session(self, session: Session) -> BrokerSession:
        """Attach a long-lived broker state to a session.

        The harness owns the loop, so quotas and the admitted source table have
        to survive across calls. Keying the broker state on the durable
        `session.token` gives a program the ability to persist sources to its
        workspace in one turn and resolve them in a later one.

        Idempotent, so a session created before a process restart keeps working.
        Note that only the workspace survives such a restart: broker state is in
        memory, so previously admitted sources become unknown.
        """
        state = self.broker.sessions.get(session.token)
        if state is None:
            state = self.broker.register_session(session)
        return state

    @staticmethod
    def _exec_request_hash(request: ExecCreate) -> str:
        payload = request.model_dump(mode="json", exclude={"exec_id"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _close_sandbox_session(self, session: Session) -> None:
        """Optional connection point for a stateful or pooled sandbox backend."""
        close = getattr(self.sandbox, "close_session", None)
        if not callable(close):
            return
        try:
            outcome = close(session)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:
            logger.exception("sandbox_session_close_failed session_id=%s", session.id)
            raise SessionCleanupError(f"Sandbox cleanup failed for session '{session.id}'") from exc

    def _session_lifecycle_lock(self, session_id: str) -> asyncio.Lock:
        lock = self.session_lifecycle_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self.session_lifecycle_locks[session_id] = lock
        return lock

    async def close_session(
        self,
        session_id: str,
        *,
        expire_at: datetime | None = None,
        tombstone_reason: str | None = None,
    ) -> bool:
        return await self._close_session_once(
            session_id,
            expire_at=expire_at,
            tombstone_reason=tombstone_reason,
        )

    async def _close_session_once(
        self,
        session_id: str,
        *,
        expire_at: datetime | None = None,
        tombstone_reason: str | None = None,
    ) -> bool:
        """Close one session after any execution already holding its lock.

        `closing` is persisted before waiting. New executions therefore fail
        rather than queueing behind DELETE, and a process restart completes the
        cleanup during startup. ``expire_at`` makes the operation conditional
        for the lease reaper and is checked immediately before that transition.
        """
        lock = self.session_locks[session_id]
        # Close the admission gate before inspecting the lock. An execution
        # that already acquired it makes a TTL close back off; one that only
        # passed its first check will observe this gate after acquiring it.
        self.closing_sessions.add(session_id)
        try:
            admitted = self._active_session_tasks(session_id)
            if expire_at is not None and (admitted or lock.locked()):
                return False
            session = self.store.get_session(session_id)
            if (
                expire_at is not None
                and not session.closing
                and not self._is_expired(session, expire_at)
            ):
                return False
            session = self.store.mark_session_closing(session_id)
            # Explicit DELETE owns the lifecycle transition, but work accepted
            # before it closed the admission gate still owns its result. Wait
            # for every such task without holding the session lock; the tasks
            # may themselves be queued on that lock.
            if admitted:
                await asyncio.gather(*admitted, return_exceptions=True)
            async with self._session_lifecycle_lock(session_id), lock:
                # Reload after admitted work touched the session. Another
                # close or abort may already have completed while this
                # caller waited; cleanup itself must run exactly once.
                try:
                    session = self.store.get_session(session_id)
                except KeyError:
                    return False
                self.broker.unregister_session(session.token)
                if tombstone_reason is not None:
                    self.store.save_tombstone(session, tombstone_reason)
                await self._close_sandbox_session(session)
                self.store.delete_session(session_id)
            return True
        finally:
            self.closing_sessions.discard(session_id)
            if not lock.locked() and self.session_locks.get(session_id) is lock:
                self.session_locks.pop(session_id, None)

    async def abort_session(self, session_id: str) -> bool:
        """Cancel admitted work and tear down an ephemeral rollout immediately."""

        return await self._abort_session_once(session_id)

    async def _abort_session_once(self, session_id: str) -> bool:
        """Perform one abort while the session lifecycle lock is held."""

        try:
            session = self.store.get_session(session_id)
        except KeyError:
            return False
        lock = self.session_locks[session_id]
        self.closing_sessions.add(session_id)
        try:
            session = self.store.mark_session_closing(session_id)
            admitted = self._active_session_tasks(session_id)
            for task in admitted:
                if not task.done():
                    task.cancel()
            if admitted:
                await asyncio.gather(*admitted, return_exceptions=True)
            await self.broker.cancel_session(session.token)
            async with self._session_lifecycle_lock(session_id), lock:
                try:
                    session = self.store.get_session(session_id)
                except KeyError:
                    return False
                self.broker.unregister_session(session.token)
                await self._close_sandbox_session(session)
                self.store.delete_session(session_id)
            return True
        finally:
            self.closing_sessions.discard(session_id)
            if not lock.locked() and self.session_locks.get(session_id) is lock:
                self.session_locks.pop(session_id, None)

    async def reap_expired_sessions(self, *, now: datetime | None = None) -> list[str]:
        """Reclaim idle sessions once; the background task calls this periodically."""
        if self.settings.session_ttl_seconds <= 0 and not any(
            session.lease_expires_at is not None for session in self.store.sessions()
        ):
            return []
        current_time = now or utc_now()
        removed: list[str] = []
        for session in self.store.sessions():
            if not session.closing and not self._is_expired(session, current_time):
                continue
            try:
                closed = await self.close_session(
                    session.id,
                    expire_at=current_time,
                    tombstone_reason="session_expired",
                )
            except KeyError:
                continue
            except Exception:
                logger.exception("session_reap_failed session_id=%s", session.id)
                continue
            if closed:
                removed.append(session.id)
        return removed

    def _is_expired(self, session: Session, now: datetime) -> bool:
        if session.lease_expires_at is not None:
            return session.lease_expires_at <= now
        ttl = self.settings.session_ttl_seconds
        return ttl > 0 and session.last_access <= now - timedelta(seconds=ttl)

    @contextmanager
    def _exec_workspace(self, session: Session) -> Iterator[Path]:
        """The directory this execution runs against.

        With persistence enabled -- the default and the only configuration a
        normal run uses -- this is the session's own workspace, so files written
        in one call are there in the next. Disabled, the program gets a fresh
        directory that is discarded on the way out: it can still write and read
        back within one program, but it cannot carry anything forward, which is
        the property the ablation removes.
        """
        if session.mechanisms.persistence:
            workspace = Path(session.workspace)
            workspace.mkdir(parents=True, exist_ok=True)
            yield workspace
            return
        with tempfile.TemporaryDirectory(prefix="opensac-ephemeral-") as directory:
            yield Path(directory)

    @staticmethod
    def _program_error_category(
        result: SandboxResult | None, *, rejected: bool = False
    ) -> str | None:
        """What went wrong, from what this process alone can see.

        Deliberately coarser than the host-side classifier, which also reads the
        capability trace and can tell a search failure from a fetch failure.
        This one only has to separate the classes that decide whether a program
        ran at all, because those are the ones that must not be read as the
        model failing at the task.
        """
        if rejected:
            return "code_validation"
        if result is None:
            return "sandbox"
        if result.launch_error:
            return "sandbox"
        if result.timed_out:
            return "timeout"
        if result.output_limit_exceeded:
            return "output_limit"
        if result.exit_code != 0:
            return "runtime"
        return None

    def _exec_task_done(
        self,
        task: asyncio.Task[ExecResult],
        key: tuple[str, str] | None,
    ) -> None:
        self.exec_tasks.discard(task)
        if key is not None:
            current = self.inflight_execs.get(key)
            if current is not None and current[1] is task:
                self.inflight_execs.pop(key, None)
        # A disconnected handler may be the task's last waiter. Retrieving its
        # exception avoids an unobserved-task warning while the durable pending
        # record remains the source of truth for a later retry.
        if not task.cancelled():
            task.exception()

    async def _cancel_execution_and_take_trace(
        self,
        session_token: str,
        execution_id: str,
        reason: str,
    ) -> list[CapabilityEvent]:
        """Drain provider work before taking the execution's final trace.

        Provider attempts append their trace while unwinding cancellation. If
        the trace were popped first, those late records would either be lost or
        recreate an orphan trace entry after the exec had finished.
        """
        await self.broker.cancel_execution(session_token, execution_id, reason)
        return self.broker.take_trace(session_token, execution_id)

    async def execute_code(self, session_id: str, request: ExecCreate) -> ExecResult:
        """Run an exec as runtime-owned work, independent of its HTTP waiter."""
        session = self.get_session(session_id)
        if session.closing or session_id in self.closing_sessions:
            raise SessionClosingError(f"Session '{session_id}' is closing")
        request_hash = self._exec_request_hash(request)
        key = (session_id, request.exec_id) if request.exec_id is not None else None
        if key is not None:
            inflight = self.inflight_execs.get(key)
            if inflight is not None:
                previous_hash, task = inflight
                if previous_hash != request_hash:
                    raise ExecIdConflictError(
                        f"Execution id '{request.exec_id}' is in flight with a different payload"
                    )
                return await asyncio.shield(task)

        task = asyncio.create_task(
            self._execute_code_once(session_id, request, request_hash=request_hash),
            name=f"opensac-exec-{session_id}",
        )
        self.exec_tasks.add(task)
        self._reserve_session_task(session_id, task)
        if key is not None:
            self.inflight_execs[key] = (request_hash, task)
        task.add_done_callback(lambda finished: self._exec_task_done(finished, key))
        # HTTP disconnect/cancellation only drops this waiter. The execution
        # continues to completion and atomically replaces its pending record.
        return await asyncio.shield(task)

    async def _execute_code_once(
        self,
        session_id: str,
        request: ExecCreate,
        *,
        request_hash: str,
    ) -> ExecResult:
        server_started = time.monotonic()
        # One execution at a time per session. The workspace, the program
        # archive and the broker's reference table are all session-scoped, and
        # two programs sharing them concurrently is not a configuration anyone
        # asks for -- but it used to be reachable, and it silently corrupted the
        # archive by letting one program's code be recorded against another's
        # result. The ceiling on total containers is a separate, global gate.
        session_queue_started = time.monotonic()
        async with self.session_locks[session_id]:
            session_queue_seconds = time.monotonic() - session_queue_started
            prepare_started = time.monotonic()
            session = self.get_session(session_id)
            session = self.store.touch_session(session_id)
            if request.exec_id is not None:
                previous = self.store.get_exec_record(session, request.exec_id)
                if previous is not None:
                    if previous.request_hash != request_hash:
                        raise ExecIdConflictError(
                            f"Execution id '{request.exec_id}' was already used "
                            "with a different payload"
                        )
                    if previous.status is ExecRecordStatus.PENDING:
                        raise ExecIndeterminateError(
                            f"Execution id '{request.exec_id}' has an indeterminate prior attempt"
                        )
                    if previous.result is None:
                        raise RuntimeError("Completed execution record has no result")
                    return previous.result

            state = self.bind_session(session)
            state.policy.require_active()
            workspace_precheck_started = time.monotonic()
            existing_workspace_bytes = await asyncio.to_thread(
                self.store.workspace_bytes,
                session,
            )
            workspace_precheck_seconds = time.monotonic() - workspace_precheck_started
            await state.policy.record_workspace_bytes(existing_workspace_bytes)
            state.policy.require_active()
            await state.policy.record_exec()
            sandbox_timeout = await state.policy.sandbox_timeout(
                self.settings.sandbox_timeout_seconds
            )
            self.store.save_session_usage(
                session.id,
                state.policy.usage,
                terminal_reason=state.policy.terminal_reason,
                touch=False,
            )
            session = self.store.get_session(session_id)
            state.session = session
            if request.exec_id is not None:
                self.store.save_exec_record(
                    session,
                    ExecRecord(
                        exec_id=request.exec_id,
                        request_hash=request_hash,
                        status=ExecRecordStatus.PENDING,
                        result=None,
                        completed_at=None,
                    ),
                )

            sequence, program_path = self.store.reserve_program(session, request.code)
            # An execution id is always minted, not only when the caller wants
            # the trace back: the per-program capability counts come from the
            # same trace, and it is drained unconditionally below, so nothing
            # accumulates for a caller that never asks for it.
            execution_id = uuid.uuid4().hex
            request_names = {
                "program_filename": f".opensac-program-{sequence:03d}.py",
                "output_filename": f".opensac-output-{sequence:03d}.json",
            }
            prepare_seconds = time.monotonic() - prepare_started
            drain_task: asyncio.Task[list[CapabilityEvent]] | None = None
            try:
                with self._exec_workspace(session) as workspace:
                    result: SandboxResult | None = None
                    rejection: str | None = None
                    sandbox_queue_seconds = 0.0
                    sandbox_execute_seconds = 0.0
                    try:
                        async with self.sandbox_gate.slot() as sandbox_queue_seconds:
                            sandbox_started = time.monotonic()
                            result = await self.sandbox.execute(
                                SandboxRequest(
                                    code=request.code,
                                    workspace=workspace,
                                    session_token=session.token,
                                    session_id=session.id,
                                    execution_id=execution_id,
                                    timeout_seconds=sandbox_timeout,
                                    **request_names,
                                )
                            )
                            sandbox_execute_seconds = time.monotonic() - sandbox_started
                    except UnsafeCodeError as exc:
                        # A rejection is a normal observation for the control model,
                        # not a transport error: it has to see the reason and
                        # rewrite the program.
                        rejection = f"Rejected by the sandbox code validator: {exc}"
                        sandbox_execute_seconds = time.monotonic() - sandbox_started

                    postprocess_started = time.monotonic()
                    if result is not None:
                        await state.policy.record_sandbox_seconds(result.duration_seconds)
                    termination_reason = (
                        "sandbox_timeout"
                        if result is not None and result.timed_out
                        else "sandbox_output_limit"
                        if result is not None and result.output_limit_exceeded
                        else "sandbox_finished"
                    )
                    drain_task = asyncio.create_task(
                        self._cancel_execution_and_take_trace(
                            session.token,
                            execution_id,
                            termination_reason,
                        ),
                        name=f"opensac-drain-{execution_id}",
                    )
                    trace = await asyncio.shield(drain_task)
                    workspace_inventory_started = time.monotonic()
                    inventory = await asyncio.to_thread(
                        self.store.workspace_inventory,
                        session,
                        workspace,
                    )
                    workspace_inventory_seconds = time.monotonic() - workspace_inventory_started
                    artifacts = inventory.artifacts
                    workspace_bytes = inventory.total_bytes
            except asyncio.CancelledError:
                if drain_task is None:
                    drain_task = asyncio.create_task(
                        self._cancel_execution_and_take_trace(
                            session.token,
                            execution_id,
                            "execution_cancelled",
                        ),
                        name=f"opensac-cancel-{execution_id}",
                    )
                # The runtime owns this cleanup even though its exec task is
                # already being cancelled. Awaiting the shield also preserves
                # the ordering guarantee: provider tasks settle, their trace is
                # appended, and only then is the trace popped.
                await asyncio.shield(drain_task)
                raise
            except Exception:
                if drain_task is None:
                    drain_task = asyncio.create_task(
                        self._cancel_execution_and_take_trace(
                            session.token,
                            execution_id,
                            "execution_failed",
                        ),
                        name=f"opensac-failed-{execution_id}",
                    )
                await asyncio.shield(drain_task)
                raise

            await state.policy.record_workspace_bytes(workspace_bytes)
            persisted_session = self.store.save_session_usage(
                session.id,
                state.policy.usage,
                terminal_reason=state.policy.terminal_reason,
            )

            self.store.record_program(
                session,
                ProgramRecord(
                    sequence=sequence,
                    path=str(program_path),
                    code=request.code,
                    exit_code=result.exit_code if result else -1,
                    timed_out=bool(result.timed_out) if result else False,
                    output_limit_exceeded=(bool(result.output_limit_exceeded) if result else False),
                    duration_seconds=result.duration_seconds if result else 0.0,
                    error=rejection or (result.launch_error if result else None),
                    error_category=self._program_error_category(
                        result, rejected=rejection is not None
                    ),
                    stdout_bytes=len(result.stdout.encode()) if result else 0,
                    stderr_bytes=len(result.stderr.encode()) if result else 0,
                    capability_calls=dict(Counter(event.method for event in trace)),
                ),
            )
            self.store.touch_session(session_id)
            postprocess_seconds = time.monotonic() - postprocess_started
            timings = dict(result.timings) if result is not None else {}
            timings.update(
                {
                    "session_queue_seconds": session_queue_seconds,
                    "prepare_seconds": prepare_seconds,
                    "workspace_precheck_seconds": workspace_precheck_seconds,
                    "sandbox_queue_seconds": sandbox_queue_seconds,
                    "sandbox_execute_seconds": sandbox_execute_seconds,
                    "workspace_inventory_seconds": workspace_inventory_seconds,
                    "postprocess_seconds": postprocess_seconds,
                    "server_total_seconds": time.monotonic() - server_started,
                }
            )

            response = ExecResult(
                exec_id=request.exec_id,
                exit_code=result.exit_code if result else -1,
                stdout=result.stdout if result else "",
                stderr=result.stderr if result else "",
                duration_seconds=result.duration_seconds if result else 0.0,
                timed_out=bool(result.timed_out) if result else False,
                output_limit_exceeded=(bool(result.output_limit_exceeded) if result else False),
                succeeded=bool(result.succeeded) if result else False,
                output=result.output if result else None,
                citations=result.citations if result else [],
                warnings=self._execution_warnings(result.warnings if result else []),
                error=rejection or (result.launch_error if result else None),
                usage=self._session_usage(state),
                artifacts=artifacts,
                trace=self._returned_trace(
                    session,
                    trace,
                    include_trace=request.include_trace,
                ),
                timings=timings,
                session_state=self.session_state(persisted_session),
                terminal_reason=state.policy.terminal_reason,
                budget_remaining=state.policy.remaining(),
            )
            if request.exec_id is not None:
                self.store.save_exec_record(
                    session,
                    ExecRecord(
                        exec_id=request.exec_id,
                        request_hash=request_hash,
                        status=ExecRecordStatus.COMPLETED,
                        result=response,
                    ),
                )
            return response

    @staticmethod
    def _execution_warnings(values: Any) -> list[ExecutionWarning]:
        if not isinstance(values, list):
            return []
        warnings: list[ExecutionWarning] = []
        for value in values[:16]:
            try:
                warnings.append(ExecutionWarning.model_validate(value))
            except (TypeError, ValueError):
                continue
        return warnings

    @staticmethod
    def _returned_trace(
        session: Session,
        trace: list[CapabilityEvent],
        *,
        include_trace: bool,
    ) -> list[CapabilityEvent]:
        """What of the trace goes back to the caller.

        A session that disables context decoupling puts its results in the
        trace, and those results are the whole point of that arm -- so the
        caller gets them whether or not it asked for a trace, since a harness
        written against the default would otherwise silently run the ablation
        without receiving what makes it an ablation.
        """
        if include_trace or not session.mechanisms.context_decoupling:
            return trace
        return []

    def _session_usage(self, state: BrokerSession) -> RunUsage:
        return RunUsage.model_validate(state.policy.usage.model_dump())
