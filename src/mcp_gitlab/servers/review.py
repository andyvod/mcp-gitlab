"""GitLab code-review tools: draft notes, inline draft notes, raw file content.

This module extends the core GitLab MCP server with review-specific tools,
kept separate from ``servers/gitlab.py`` to stay merge-friendly with upstream.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from .gitlab import _check_write, _err, _get_client, _ok, _paginated, mcp

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _resolve_line_in_diff(diff_text: str, search_text: str, side: str = "new") -> int | None:
    """Resolve a line number in a unified diff by searching for ``search_text``.

    ``side`` selects the new-file (``"new"``) or old-file (``"old"``) line number.
    Returns ``None`` when the text is not found.
    """
    old_line = 0
    new_line = 0
    needle = search_text.strip().lower()
    for line in diff_text.split("\n"):
        m = _HUNK_RE.match(line)
        if m:
            old_line = int(m.group(1))
            new_line = int(m.group(2))
            continue
        if not line:
            continue
        prefix = line[0]
        if prefix == "+":
            if side == "new" and needle in line[1:].strip().lower():
                return new_line
            new_line += 1
        elif prefix == "-":
            if side == "old" and needle in line[1:].strip().lower():
                return old_line
            old_line += 1
        else:
            content = line[1:] if prefix in (" ", "\t") else line
            if needle in content.strip().lower():
                return new_line if side == "new" else old_line
            old_line += 1
            new_line += 1
    return None


async def _create_draft_note_impl(
    ctx: Context,
    project_id: str,
    mr_iid: int,
    body: str,
    *,
    base_sha: str | None = None,
    head_sha: str | None = None,
    start_sha: str | None = None,
    old_path: str | None = None,
    new_path: str | None = None,
    old_line: int | None = None,
    new_line: int | None = None,
) -> dict[str, Any]:
    """Build the draft-note payload and POST it. Does NOT enforce write mode."""
    payload: dict[str, Any] = {"note": body}
    if all([base_sha, head_sha, start_sha]):
        position: dict[str, Any] = {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": start_sha,
            "position_type": "text",
        }
        if old_path:
            position["old_path"] = old_path
        if new_path:
            position["new_path"] = new_path
        if old_line is not None:
            position["old_line"] = old_line
        if new_line is not None:
            position["new_line"] = new_line
        payload["position"] = position
    return await _get_client(ctx).create_draft_note(project_id, mr_iid, payload)


@mcp.tool(
    tags={"gitlab", "mr", "write"},
    annotations={"readOnlyHint": False, "openWorldHint": True},
)
async def gitlab_create_draft_note(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    mr_iid: Annotated[int, Field(description="Merge request IID")],
    body: Annotated[str, Field(description="Draft note text (markdown)", min_length=1)],
    base_sha: Annotated[str | None, Field(description="Base commit SHA from diff_refs")] = None,
    head_sha: Annotated[str | None, Field(description="Head commit SHA from diff_refs")] = None,
    start_sha: Annotated[str | None, Field(description="Start commit SHA from diff_refs")] = None,
    old_path: Annotated[str | None, Field(description="Old file path (for renames)")] = None,
    new_path: Annotated[str | None, Field(description="New file path")] = None,
    old_line: Annotated[int | None, Field(description="Line number in old file")] = None,
    new_line: Annotated[int | None, Field(description="Line number in new file")] = None,
) -> str:
    """Create a draft note on a MR. Invisible until gitlab_publish_draft_notes is called."""
    try:
        _check_write(ctx)
        data = await _create_draft_note_impl(
            ctx,
            project_id,
            mr_iid,
            body,
            base_sha=base_sha,
            head_sha=head_sha,
            start_sha=start_sha,
            old_path=old_path,
            new_path=new_path,
            old_line=old_line,
            new_line=new_line,
        )
        return _ok(data)
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "mr", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_list_draft_notes(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    mr_iid: Annotated[int, Field(description="Merge request IID")],
) -> str:
    """List all draft notes for a merge request (not yet published)."""
    try:
        items = await _get_client(ctx).list_draft_notes(project_id, mr_iid)
        return _paginated(items)
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "mr", "write"},
    annotations={"readOnlyHint": False, "openWorldHint": True},
)
async def gitlab_publish_draft_notes(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    mr_iid: Annotated[int, Field(description="Merge request IID")],
) -> str:
    """Publish ALL draft notes at once. Equivalent to 'Submit review' in GitLab UI."""
    try:
        _check_write(ctx)
        await _get_client(ctx).bulk_publish_draft_notes(project_id, mr_iid)
        return _ok({"status": "published"})
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "mr", "write"},
    annotations={"readOnlyHint": False, "openWorldHint": True},
)
async def gitlab_delete_draft_note(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    mr_iid: Annotated[int, Field(description="Merge request IID")],
    draft_note_id: Annotated[int, Field(description="Draft note ID to delete")],
) -> str:
    """Delete a single draft note by ID."""
    try:
        _check_write(ctx)
        await _get_client(ctx).delete_draft_note(project_id, mr_iid, draft_note_id)
        return _ok({"status": "deleted"})
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "projects", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def gitlab_get_file_content(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    file_path: Annotated[str, Field(description="Path to the file in the repo", min_length=1)],
    ref: Annotated[str, Field(description="Branch, tag, or commit SHA")] = "master",
) -> str:
    """Get raw file content from a GitLab repository. Returns the file text (not JSON)."""
    try:
        return await _get_client(ctx).get_file_raw(project_id, file_path, ref)
    except Exception as e:
        return _err(e)


@mcp.tool(
    tags={"gitlab", "mr", "write"},
    annotations={"readOnlyHint": False, "openWorldHint": True},
)
async def gitlab_create_inline_draft_note(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project ID or URL-encoded path", min_length=1)],
    mr_iid: Annotated[int, Field(description="Merge request IID")],
    body: Annotated[str, Field(description="Comment text in markdown", min_length=1)],
    file_path: Annotated[str, Field(description="File path (new_path in diff)", min_length=1)],
    search_text: Annotated[
        str,
        Field(
            description=(
                "Fragment of the target line; must be unique in the diff for this "
                "file. Searched case-insensitively."
            ),
            min_length=1,
        ),
    ],
    side: Annotated[
        str,
        Field(description="'new' for added/changed lines, 'old' for removed"),
    ] = "new",
) -> str:
    """Create a draft note anchored to a specific line in a MR file.

    Resolves the line number automatically by searching for ``search_text`` in
    the diff. Prefer this over ``gitlab_create_draft_note`` to avoid manual line
    counting.
    """
    try:
        _check_write(ctx)
        client = _get_client(ctx)
        mr = await client.get_merge_request(project_id, mr_iid)
        refs = mr.get("diff_refs") or {}
        base_sha = refs.get("base_sha")
        head_sha = refs.get("head_sha")
        start_sha = refs.get("start_sha")
        if not all([base_sha, head_sha, start_sha]):
            msg = "MR diff_refs are not available; cannot anchor an inline note"
            return _err(ValueError(msg))

        changes = (await client.get_merge_request_changes(project_id, mr_iid)).get("changes", [])
        diff_text: str | None = None
        for change in changes:
            if change.get("new_path") == file_path or change.get("old_path") == file_path:
                diff_text = change.get("diff", "")
                break
        if diff_text is None:
            msg = f"File {file_path} not found in MR changes"
            return _err(ValueError(msg))

        line_number = _resolve_line_in_diff(diff_text, search_text, side)
        if line_number is None:
            msg = f"Search text not found in diff for {file_path}"
            return _err(ValueError(msg))

        position: dict[str, Any] = {"new_path": file_path, "old_path": file_path}
        if side == "new":
            position["new_line"] = line_number
        else:
            position["old_line"] = line_number

        data = await _create_draft_note_impl(
            ctx,
            project_id,
            mr_iid,
            body,
            base_sha=base_sha,
            head_sha=head_sha,
            start_sha=start_sha,
            old_path=position["old_path"],
            new_path=position["new_path"],
            old_line=position.get("old_line"),
            new_line=position.get("new_line"),
        )
        return _ok(data)
    except Exception as e:
        return _err(e)
    