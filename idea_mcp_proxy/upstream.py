"""Connection to the real JetBrains IDE MCP server.

Connected lazily on first use rather than at proxy startup, so the proxy
starts fine while the IDE (or its MCP server) is off, and reconnected
transparently when the IDE restarts.

The SDK's client transport and session are async context managers backed by
task groups, which must be entered and exited in the same task. So one
supervisor task owns the connection: request handlers ask it to connect and
wait until it's ready, and tell it to drop the connection when a call fails
at the transport level.

Tool calls are never retried: a reused connection is pinged before the call
is sent, so a stale one is replaced up front, and a failure after sending
means the IDE may already have executed the call (a refactoring or file edit
must not run twice).
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import anyio
import mcp.types as types
from anyio.abc import TaskGroup
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("idea_mcp_proxy.upstream")

DEFAULT_UPSTREAM_URL = "http://127.0.0.1:64342/stream"
DEFAULT_CONNECT_TIMEOUT = 10.0

T = TypeVar("T")


class UpstreamUnavailable(Exception):
    """The IDE's MCP server could not be reached, or the connection broke mid-call."""

    def __init__(self, url: str, cause: BaseException | None = None, *, mid_call: bool = False):
        self.url = url
        self.cause = cause
        detail = f" ({_describe(cause)})" if cause else ""
        if mid_call:
            # Not retried: the IDE may already have executed the tool, and
            # running a refactoring or file edit twice is worse than an error.
            what = (
                f"Connection to the IntelliJ MCP server at {url} was lost during the "
                f"call{detail}; the IDE may or may not have executed it, so check its "
                f"effect before retrying."
            )
        else:
            what = f"IntelliJ MCP server not reachable at {url}{detail}."
        super().__init__(
            f"{what} Is the IDE running with Settings | Tools | MCP Server enabled? "
            f"If it listens on another port, pass --upstream to idea-mcp-proxy."
        )


def _describe(exc: BaseException) -> str:
    # Transport failures arrive wrapped in (possibly nested) exception groups;
    # the innermost leaf is the informative one.
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


_LOST_SESSION = re.compile(r"session (not found|terminated|expired)", re.IGNORECASE)


def _is_rejected_session(exc: MCPError) -> bool:
    # A restarted IDE no longer knows our Mcp-Session-Id: the server says
    # "Session not found", or the SDK maps the bare HTTP 404 to
    # "Session terminated".
    return bool(_LOST_SESSION.search(exc.message or ""))


class _Attempt:
    def __init__(self) -> None:
        self.ready = anyio.Event()
        self.drop = anyio.Event()
        self.session: ClientSession | None = None
        self.used = False
        self.error: BaseException | None = None


class UpstreamConnection:
    def __init__(
        self,
        url: str,
        task_group: TaskGroup,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ):
        self.url = url
        self._tg = task_group
        self._connect_timeout = connect_timeout
        self._current: _Attempt | None = None
        self._lock = anyio.Lock()
        # Bumped on every successful (re)connect, so callers caching anything
        # about the IDE (tool schemas) can tell a new IDE instance apart.
        self.generation = 0

    async def list_tools(self) -> types.ListToolsResult:
        return await self._with_session(lambda s: s.list_tools())

    async def call_tool(self, name: str, arguments: dict[str, object]) -> types.CallToolResult:
        return await self._with_session(lambda s: s.call_tool(name, arguments))

    async def _with_session(self, fn: Callable[[ClientSession], Awaitable[T]]) -> T:
        attempt = await self._live_attempt()
        try:
            return await fn(attempt.session)
        except MCPError as exc:
            if exc.code == types.CONNECTION_CLOSED or _is_rejected_session(exc):
                # The connection was verified just before sending, so it broke
                # while this call was in flight. There is no telling whether
                # the IDE executed it, so it is reported, never retried.
                logger.warning("upstream connection lost during a call (%s)", exc.message)
                self._drop(attempt)
                raise UpstreamUnavailable(self.url, attempt.error or exc, mid_call=True) from exc
            # A real error response from the IDE: pass it through as-is.
            raise
        except Exception as exc:
            logger.warning("upstream call failed (%s); dropping connection", _describe(exc))
            self._drop(attempt)
            raise UpstreamUnavailable(self.url, exc, mid_call=True) from exc

    async def _live_attempt(self) -> _Attempt:
        """A connection verified to work right now.

        A reused connection is pinged first: after an IDE shutdown or restart
        it is dead or its session unknown to the new IDE process, and the SDK
        only finds out on the next request. Finding out with a ping, rather
        than the real call, means a failure here is always safe to recover
        from by reconnecting -- the real call was never sent.
        """
        attempt = await self._ensure_connected()
        if not attempt.used:
            attempt.used = True
            return attempt
        try:
            with anyio.fail_after(self._connect_timeout):
                await attempt.session.send_ping()
            return attempt
        except Exception as exc:
            logger.warning("upstream connection is stale (%s); reconnecting", _describe(exc))
            self._drop(attempt)
        attempt = await self._ensure_connected()
        attempt.used = True
        return attempt

    async def _ensure_connected(self) -> _Attempt:
        current = self._current
        if current is not None and current.session is not None:
            return current
        async with self._lock:
            current = self._current
            if current is not None and current.session is not None:
                return current
            attempt = _Attempt()
            self._current = attempt
            self._tg.start_soon(self._own_connection, attempt)
            with anyio.move_on_after(self._connect_timeout) as scope:
                await attempt.ready.wait()
            if scope.cancelled_caught:
                self._drop(attempt)
                raise UpstreamUnavailable(self.url, TimeoutError("connect timed out"))
            if attempt.session is None:
                raise UpstreamUnavailable(self.url, attempt.error)
            self.generation += 1
            logger.info("connected to upstream IDE server at %s", self.url)
            return attempt

    def _drop(self, attempt: _Attempt) -> None:
        if self._current is attempt:
            self._current = None
        attempt.session = None
        attempt.drop.set()

    async def _own_connection(self, attempt: _Attempt) -> None:
        try:
            async with streamable_http_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    attempt.session = session
                    attempt.ready.set()
                    await attempt.drop.wait()
        except Exception as exc:
            attempt.error = exc
            if attempt.session is not None:
                logger.warning("upstream connection lost: %s", _describe(exc))
        finally:
            attempt.session = None
            if self._current is attempt:
                self._current = None
            attempt.ready.set()


@asynccontextmanager
async def connect(
    url: str = DEFAULT_UPSTREAM_URL,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> AsyncIterator[UpstreamConnection]:
    """Yields a lazily connecting upstream; never fails just because the IDE is down."""
    async with anyio.create_task_group() as tg:
        upstream = UpstreamConnection(url, tg, connect_timeout)
        try:
            yield upstream
        finally:
            tg.cancel_scope.cancel()
