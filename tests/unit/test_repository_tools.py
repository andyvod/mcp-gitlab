"""Tool-level tests for the fork-only repository navigation tools.

Mirrors the fixtures/pattern from ``test_tools.py``: a mocked lifespan, a
respx-mocked GitLab API, and parsing of the FastMCP tool-call result JSON.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import respx
from fastmcp import Client, FastMCP
from httpx import Response

import mcp_gitlab.servers.repository  # noqa: F401 — registers @mcp.tool decorators
from mcp_gitlab.client import GitLabClient
from mcp_gitlab.config import GitLabConfig

TEST_URL = "https://gitlab.example.com"
TEST_TOKEN = "test-token"

ENC = "group%2Fproject"


def _make_mcp() -> FastMCP:
    config = GitLabConfig(url=TEST_URL, token=TEST_TOKEN)
    client = GitLabClient(config)

    @asynccontextmanager
    async def mock_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"client": client, "config": config}
        finally:
            await client.close()

    from mcp_gitlab.servers.gitlab import mcp

    original_lifespan = mcp._lifespan
    mcp._lifespan = mock_lifespan
    return mcp, original_lifespan


@pytest.fixture
async def tool_client():
    mcp, original_lifespan = _make_mcp()
    with respx.mock(base_url=f"{TEST_URL}/api/v4") as router:
        async with Client(mcp) as client:
            yield client, router
    mcp._lifespan = original_lifespan


def _parse(result: Any) -> dict | list:
    if hasattr(result, "content"):
        for item in result.content:
            if hasattr(item, "text"):
                return json.loads(item.text)
    if hasattr(result, "__iter__") and not isinstance(result, (str, dict)):
        for item in result:
            if hasattr(item, "text"):
                return json.loads(item.text)
    return json.loads(str(result))


def _text(result: Any) -> str:
    """Extract raw text from a tool result (for non-JSON tools like get_blob)."""
    if hasattr(result, "content"):
        for item in result.content:
            if hasattr(item, "text"):
                return item.text
    return str(result)


# ═══════════════════════════════════════════════════════
# gitlab_list_tree
# ═══════════════════════════════════════════════════════


class TestListTree:
    async def test_happy_path_slim_fields(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/tree").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "path": "README.md",
                        "type": "blob",
                        "mode": "100644",
                        "id": "abc",
                        "name": "README.md",
                        "extra": "drop",
                    },
                    {"path": "src", "type": "tree", "mode": "040000", "id": "def"},
                ],
            )
        )
        result = await client.call_tool(
            "gitlab_list_tree", {"project_id": "group/project", "ref": "v1.0"}
        )
        parsed = _parse(result)
        assert parsed["count"] == 2
        assert parsed["items"][0] == {
            "path": "README.md",
            "type": "blob",
            "mode": "100644",
            "id": "abc",
        }
        assert "extra" not in parsed["items"][0]

    async def test_not_found(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/tree").mock(
            return_value=Response(404, json={"message": "404 Not Found"})
        )
        result = await client.call_tool(
            "gitlab_list_tree", {"project_id": "group/project", "ref": "nope"}
        )
        parsed = _parse(result)
        assert "error" in parsed


# ═══════════════════════════════════════════════════════
# gitlab_search_file
# ═══════════════════════════════════════════════════════


class TestSearchFile:
    async def test_filter_by_name_across_two_pages(self, tool_client):
        client, router = tool_client

        page1 = [{"path": f"src/file{i}.cs", "type": "blob"} for i in range(100)]
        page1[0] = {"path": "queries/PacketApiQueriesEF.cs", "type": "blob"}
        page2 = [
            {"path": "other/readme.txt", "type": "blob"},
            {"path": "more/PacketHelper.cs", "type": "blob"},
            {"path": "queries/sub", "type": "tree"},
        ]

        def responder(request):
            page = request.url.params.get("page", "1")
            if page == "2":
                return Response(200, json=page2)
            return Response(200, json=page1)

        router.get(f"/projects/{ENC}/repository/tree").mock(side_effect=responder)

        result = await client.call_tool(
            "gitlab_search_file",
            {"project_id": "group/project", "pattern": "Packet*.cs", "ref": "v3.29.2"},
        )
        parsed = _parse(result)
        assert parsed["count"] == 2
        assert "queries/PacketApiQueriesEF.cs" in parsed["paths"]
        assert "more/PacketHelper.cs" in parsed["paths"]
        # tree entries and non-matching names excluded
        assert "queries/sub" not in parsed["paths"]

    async def test_extension_filter(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/tree").mock(
            return_value=Response(
                200,
                json=[
                    {"path": "a/Packet.cs", "type": "blob"},
                    {"path": "a/Packet.md", "type": "blob"},
                    {"path": "b/Packet.cs", "type": "blob"},
                ],
            )
        )
        result = await client.call_tool(
            "gitlab_search_file",
            {"project_id": "group/project", "pattern": "Packet.*", "extension": "cs"},
        )
        parsed = _parse(result)
        assert parsed["count"] == 2
        assert all(p.endswith(".cs") for p in parsed["paths"])

    async def test_no_matches(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/tree").mock(
            return_value=Response(200, json=[{"path": "a.txt", "type": "blob"}])
        )
        result = await client.call_tool(
            "gitlab_search_file",
            {"project_id": "group/project", "pattern": "*.cs"},
        )
        parsed = _parse(result)
        assert parsed["count"] == 0
        assert parsed["paths"] == []


# ═══════════════════════════════════════════════════════
# gitlab_search_code
# ═══════════════════════════════════════════════════════


class TestSearchCode:
    async def test_happy_path(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/search").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "path": "src/app.py",
                        "ref": "master",
                        "startline": 10,
                        "data": "def foo():",
                        "project_id": 1,
                    },
                    {"filename": "src/util.py", "startline": 5, "data": "x = 1"},
                ],
            )
        )
        result = await client.call_tool(
            "gitlab_search_code",
            {"project_id": "group/project", "search": "def foo"},
        )
        parsed = _parse(result)
        assert parsed["count"] == 2
        assert parsed["items"][0]["path"] == "src/app.py"
        assert parsed["items"][0]["startline"] == 10

    async def test_empty_result(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/search").mock(return_value=Response(200, json=[]))
        result = await client.call_tool(
            "gitlab_search_code", {"project_id": "group/project", "search": "nothing"}
        )
        parsed = _parse(result)
        assert parsed["count"] == 0


# ═══════════════════════════════════════════════════════
# gitlab_get_file_metadata
# ═══════════════════════════════════════════════════════


class TestGetFileMetadata:
    async def test_excludes_content(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/files/src%2Fapp.py").mock(
            return_value=Response(
                200,
                json={
                    "file_name": "app.py",
                    "file_path": "src/app.py",
                    "blob_id": "sha123",
                    "commit_id": "com456",
                    "content_sha256": "abc",
                    "size": 42,
                    "encoding": "base64",
                    "content": "BASE64SHOULDBEEXCLUDED",
                    "execute_filemode": False,
                    "ref": "master",
                },
            )
        )
        result = await client.call_tool(
            "gitlab_get_file_metadata",
            {"project_id": "group/project", "file_path": "src/app.py", "ref": "master"},
        )
        parsed = _parse(result)
        assert parsed["blob_id"] == "sha123"
        assert parsed["size"] == 42
        assert "content" not in parsed

    async def test_not_found(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/files/missing.py").mock(
            return_value=Response(404, json={"message": "404 File Not Found"})
        )
        result = await client.call_tool(
            "gitlab_get_file_metadata",
            {"project_id": "group/project", "file_path": "missing.py"},
        )
        parsed = _parse(result)
        assert "error" in parsed


# ═══════════════════════════════════════════════════════
# gitlab_get_blob
# ═══════════════════════════════════════════════════════


class TestGetBlob:
    async def test_returns_raw_text(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/blobs/sha123/raw").mock(
            return_value=Response(200, text="print('hello')\n")
        )
        result = await client.call_tool(
            "gitlab_get_blob", {"project_id": "group/project", "sha": "sha123"}
        )
        assert _text(result) == "print('hello')\n"

    async def test_not_found(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/blobs/nope/raw").mock(
            return_value=Response(404, json={"message": "404 Blob Not Found"})
        )
        result = await client.call_tool(
            "gitlab_get_blob", {"project_id": "group/project", "sha": "nope"}
        )
        parsed = _parse(result)
        assert "error" in parsed


# ═══════════════════════════════════════════════════════
# gitlab_search_blame
# ═══════════════════════════════════════════════════════


class TestSearchBlame:
    async def test_happy_path(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/files/src%2Fapp.py/blame").mock(
            return_value=Response(
                200,
                json=[
                    {"commit": {"id": "c1", "message": "init"}, "lines": [1, 2]},
                    {"commit": {"id": "c2"}, "lines": [3]},
                ],
            )
        )
        result = await client.call_tool(
            "gitlab_search_blame",
            {"project_id": "group/project", "file_path": "src/app.py", "ref": "master"},
        )
        parsed = _parse(result)
        assert parsed["count"] == 2
        assert parsed["groups"][0]["commit"]["id"] == "c1"

    async def test_not_found(self, tool_client):
        client, router = tool_client
        router.get(f"/projects/{ENC}/repository/files/nope.py/blame").mock(
            return_value=Response(404, json={"message": "404 Not Found"})
        )
        result = await client.call_tool(
            "gitlab_search_blame",
            {"project_id": "group/project", "file_path": "nope.py"},
        )
        parsed = _parse(result)
        assert "error" in parsed
