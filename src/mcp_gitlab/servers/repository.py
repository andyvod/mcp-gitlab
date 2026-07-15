"""GitLab repository navigation tools: tree, file metadata, blobs, search, blame.

This is a fork-only extension module, kept separate from ``servers/gitlab.py``
to stay merge-friendly with upstream (``vish288/mcp-gitlab``). New repository
tools live here and are registered against the shared ``mcp`` instance exported
by ``servers/gitlab.py``. See ``AGENTS.fork.md`` for the fork conventions.

It never adds methods to ``GitLabClient``: it calls the public ``client.get()``
helper directly and does its own pagination.
"""

from __future__ import annotations

import fnmatch
from typing import Annotated, Any
from urllib.parse import quote

from fastmcp import Context
from pydantic import Field

from .gitlab import _err, _get_client, _ok, mcp

# NOTE: ``GitLabClient._encode_id`` is private. We rely on it here because it is
# the same encoder the core client uses everywhere (URL-encoded paths, numeric
# passthrough, full-URL reduction). If upstream renames or changes its
# signature, this module must be updated on rebase.


async def _paginated_get(
    client: Any,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    per_page: int = 100,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Fetch a list endpoint across pages using the public ``client.get()``.

    Stops on the first empty page or on a short page (fewer than ``per_page``
    items), and is capped at ``max_pages`` to bound cost on large repos.
    """
    p: dict[str, Any] = {"per_page": per_page, "page": 1, **(params or {})}
    out: list[dict[str, Any]] = []
    for _ in range(max_pages):
        page = await client.get(path, params=p)
        if not page:
            break
        out.extend(page)
        if len(page) < per_page:
            break
        p["page"] += 1
    return out


@mcp.tool(
    tags={"gitlab", "repository", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_list_tree(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    ref: Annotated[str, Field(description="Branch, tag, or commit SHA")] = "master",
    path: Annotated[str | None, Field(description="Subpath to list (default: repo root)")] = None,
    recursive: Annotated[bool, Field(description="Recurse into subdirectories")] = True,
    max_pages: Annotated[int, Field(description="Max pages to fetch", ge=1, le=50)] = 50,
) -> str:
    """List repository tree entries (files and dirs) for any ref.

    Returns a JSON object ``{items: [{path, type, mode, id}], count}``. Works for
    branches, tags, and commit SHAs.
    """
    try:
        client = _get_client(ctx)
        enc = client._encode_id(project_id)
        params: dict[str, Any] = {"ref": ref, "recursive": str(recursive).lower()}
        if path:
            params["path"] = path
        items = await _paginated_get(
            client,
            f"/projects/{enc}/repository/tree",
            params=params,
            max_pages=max_pages,
        )
        slim = [
            {
                "path": e.get("path"),
                "type": e.get("type"),
                "mode": e.get("mode"),
                "id": e.get("id"),
            }
            for e in items
        ]
        return _ok({"items": slim, "count": len(slim)})
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "repository", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_search_file(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    pattern: Annotated[
        str,
        Field(
            description="Glob matched against the file basename, e.g. 'PacketApi*.cs'",
            min_length=1,
        ),
    ],
    ref: Annotated[str, Field(description="Branch, tag, or commit SHA")] = "master",
    extension: Annotated[str | None, Field(description="Extension filter, e.g. 'cs'")] = None,
    limit: Annotated[int, Field(description="Max matches to return", ge=1, le=500)] = 50,
) -> str:
    """Find files by name/glob across any ref (including tags and SHAs).

    Walks the recursive tree client-side and filters the basename with
    ``fnmatch``. Unlike ``gitlab_search_code`` this works for ANY ref, not just
    the default branch. Returns a JSON object ``{paths: ["a/b.cs", ...], count}``.
    """
    try:
        client = _get_client(ctx)
        enc = client._encode_id(project_id)
        items = await _paginated_get(
            client,
            f"/projects/{enc}/repository/tree",
            params={"ref": ref, "recursive": "true"},
        )
        ext = extension.lstrip(".") if extension else None
        matches: list[str] = []
        for e in items:
            if e.get("type") != "blob":
                continue
            full = e.get("path", "")
            base = full.rsplit("/", 1)[-1]
            if not fnmatch.fnmatch(base, pattern):
                continue
            if ext and not base.lower().endswith(f".{ext.lower()}"):
                continue
            matches.append(full)
            if len(matches) >= limit:
                break
        return _ok({"paths": matches, "count": len(matches)})
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "repository", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_search_code(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    search: Annotated[str, Field(description="Text to search for in file contents", min_length=1)],
    filename: Annotated[str | None, Field(description="Optional filename filter")] = None,
    path: Annotated[str | None, Field(description="Optional path prefix filter")] = None,
    extension: Annotated[
        str | None, Field(description="Optional extension filter, e.g. 'py'")
    ] = None,
    ref: Annotated[
        str | None,
        Field(description="Ref filter; may be IGNORED without Advanced Search"),
    ] = None,
) -> str:
    """Search file contents via GitLab project search (scope=blobs).

    WARNING: on instances WITHOUT Advanced Search, this only searches the default
    branch and the ``ref`` filter may be ignored. To search an arbitrary tag or
    SHA, use ``gitlab_search_file`` + ``gitlab_get_file_content`` instead.
    Returns a JSON object ``{items: [{path, ref, startline, data}], count}``.
    """
    try:
        client = _get_client(ctx)
        enc = client._encode_id(project_id)
        params: dict[str, Any] = {"scope": "blobs", "search": search}
        if filename:
            params["filename"] = filename
        if path:
            params["path"] = path
        if extension:
            params["extension"] = extension
        if ref:
            params["ref"] = ref
        items = await client.get(f"/projects/{enc}/search", params=params)
        items = items or []
        slim = [
            {
                "path": i.get("path") or i.get("filename"),
                "ref": i.get("ref"),
                "startline": i.get("startline"),
                "data": i.get("data"),
                "project_id": i.get("project_id"),
            }
            for i in items
        ]
        return _ok({"items": slim, "count": len(slim)})
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "repository", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_get_file_metadata(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    file_path: Annotated[str, Field(description="Path to the file in the repo", min_length=1)],
    ref: Annotated[str, Field(description="Branch, tag, or commit SHA")] = "master",
) -> str:
    """Get a file's metadata (blob id, size, sha256) without its content.

    Returns a JSON object with ``blob_id, commit_id, content_sha256, size,
    execute_filemode``. Base64 content is excluded. Use ``gitlab_get_blob`` with
    ``blob_id`` (or ``gitlab_get_file_content``) to fetch the body.
    """
    try:
        client = _get_client(ctx)
        enc = client._encode_id(project_id)
        encoded_path = quote(file_path, safe="")
        data = await client.get(
            f"/projects/{enc}/repository/files/{encoded_path}",
            params={"ref": ref},
        )
        keys = (
            "file_name",
            "file_path",
            "blob_id",
            "commit_id",
            "content_sha256",
            "size",
            "encoding",
            "execute_filemode",
            "ref",
        )
        meta = {k: data.get(k) for k in keys if k in data}
        return _ok(meta)
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "repository", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_get_blob(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    sha: Annotated[
        str,
        Field(description="Blob SHA (from list_tree id / get_file_metadata)", min_length=1),
    ],
) -> str:
    """Get raw blob content by its SHA as plain text.

    Returns the blob text. Note: for binary files the output will be garbage;
    filter by extension/size on the caller side.
    """
    try:
        client = _get_client(ctx)
        enc = client._encode_id(project_id)
        return await client.get(
            f"/projects/{enc}/repository/blobs/{quote(sha, safe='')}/raw",
            raw=True,
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "repository", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_search_blame(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    file_path: Annotated[str, Field(description="Path to the file in the repo", min_length=1)],
    ref: Annotated[str, Field(description="Branch, tag, or commit SHA")] = "master",
    max_pages: Annotated[
        int,
        Field(description="Max blame pages; blame is heavy on large files", ge=1, le=50),
    ] = 20,
) -> str:
    """Get blame (per-line commit attribution) for a file at a ref.

    Returns a JSON object ``{groups: [{commit: {...}, lines: [...]}], count}``.
    Blame can be expensive on large files; ``max_pages`` bounds the volume.
    """
    try:
        client = _get_client(ctx)
        enc = client._encode_id(project_id)
        encoded_path = quote(file_path, safe="")
        groups = await _paginated_get(
            client,
            f"/projects/{enc}/repository/files/{encoded_path}/blame",
            params={"ref": ref},
            max_pages=max_pages,
        )
        return _ok({"groups": groups, "count": len(groups)})
    except Exception as e:
        return _err(e)
