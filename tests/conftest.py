"""
Fixtures for the domain test suite.

In-process tool tests make raw JSON-RPC POSTs directly to /mcp using
httpx ASGITransport with json_response=True. This avoids the SSE GET
stream that streamable_http_client establishes, which would block the
event loop when driven by ASGITransport.

The E2E test in test_server.py starts a real subprocess and uses
streamable_http_client over genuine TCP, where SSE streaming works fine.
"""

import os

import httpx
import pytest
import pytest_asyncio

from domain import tokens
from domain.server import app, mcp


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def running_app():
    """Start the session manager in a background task.

    anyio's cancel scope must be entered and exited within the same task.
    Running the session manager inside asyncio.create_task() keeps all
    anyio task-group lifecycle in that task, so teardown cancels cleanly.
    """
    import asyncio

    starlette_app = mcp.streamable_http_app()
    started = asyncio.Event()

    async def _run() -> None:
        async with mcp._session_manager.run():
            started.set()
            # Hold the context open until the fixture is torn down.
            await asyncio.get_event_loop().create_future()

    task = asyncio.create_task(_run())
    await started.wait()
    yield starlette_app
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture
def log_path(tmp_path):
    """Point DECISION_LOG at a temp file; restore on teardown."""
    p = tmp_path / "decisions.jsonl"
    os.environ["DECISION_LOG"] = str(p)
    yield p
    os.environ.pop("DECISION_LOG", None)


@pytest_asyncio.fixture(autouse=True)
async def _reset(running_app, log_path):
    """Reset server state and tool registry before each test."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        await client.post("/control/reset")


@pytest_asyncio.fixture
async def http_client(running_app):
    """Plain HTTP client against the ASGI app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        yield client


class McpSession:
    """
    Minimal MCP session backed by raw JSON-RPC POSTs.

    Avoids streamable_http_client's SSE GET stream, which blocks
    ASGITransport. Relies on json_response=True so every POST returns
    a 200 with a JSON body.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._session_id: str | None = None
        self._call_id = 0

    async def initialize(self) -> None:
        self._call_id += 1
        resp = await self._post({
            "jsonrpc": "2.0",
            "id": self._call_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        })
        self._session_id = resp.headers.get("mcp-session-id")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self._call_id += 1
        resp = await self._post({
            "jsonrpc": "2.0",
            "id": self._call_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        body = resp.json()
        return body.get("result", body)

    async def list_tools(self) -> list[dict]:
        self._call_id += 1
        resp = await self._post({
            "jsonrpc": "2.0",
            "id": self._call_id,
            "method": "tools/list",
            "params": {},
        })
        body = resp.json()
        return body.get("result", {}).get("tools", [])

    async def _post(self, payload: dict) -> httpx.Response:
        headers: dict = {
            "Content-Type":        "application/json",
            "Accept":              "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        resp = await self._client.post("/mcp", json=payload, headers=headers)
        resp.raise_for_status()
        return resp


@pytest_asyncio.fixture
async def mcp_session(running_app) -> McpSession:
    """Initialised MCP session acting as alice / expense-assistant, with a Bearer token."""
    all_scopes = sorted(tokens.FIXED_SCOPES)

    # Issue tokens from the server's control endpoints
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as ctrl:
        resp = await ctrl.post("/control/identity/user-token", json={
            "sub": "alice",
            "aud": "expense-assistant",
            "scope": all_scopes,
        })
        resp.raise_for_status()
        user_token = resp.json()["access_token"]

        resp = await ctrl.post("/control/identity/agent-token", json={
            "sub": "expense-assistant",
            "aud": "mcp-server",
            "scope": all_scopes,
        })
        resp.raise_for_status()
        agent_token = resp.json()["access_token"]

        resp = await ctrl.post("/identity/exchange", json={
            "subject_token": user_token,
            "actor_token": agent_token,
        })
        resp.raise_for_status()
        bearer = resp.json()["access_token"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={
            "Host":          "localhost:8765",
            "Authorization": f"Bearer {bearer}",
        },
    ) as client:
        session = McpSession(client)
        await session.initialize()
        yield session
