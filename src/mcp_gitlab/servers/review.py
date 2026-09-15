"""GitLab code-review tools: draft notes, inline draft notes, raw file content.

This module extends the core GitLab MCP server with review-specific tools,
kept separate from ``servers/gitlab.py`` to stay merge-friendly with upstream.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, cast

from fastmcp import Context
from pydantic import Field

from .gitlab import _check_write, _err, _get_client, _ok, _paginated, mcp

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_LINE_KIND_TO_TYPE: dict[str, str | None] = {"added": "new", "removed": "old", "context": None}


def _resolve_anchor_in_diff(
    diff_text: str, search_text: str, side: str = "new"
) -> tuple[dict[str, int], str] | None:
    """Resolve a diff anchor by searching for ``search_text``.

    ``side`` selects the new-file (``"new"``) or old-file (``"old"``) side.
    Returns ``(anchor_fields, line_kind)`` where ``line_kind`` is ``"added"``,
    ``"removed"`` or ``"context"``. Context anchors carry BOTH the old and the
    new line number so GitLab can compute ``line_code``. Returns ``None`` when
    the text is not found on the requested side.
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
                return {"new_line": new_line}, "added"
            new_line += 1
        elif prefix == "-":
            if side == "old" and needle in line[1:].strip().lower():
                return {"old_line": old_line}, "removed"
            old_line += 1
        else:
            content = line[1:] if prefix in (" ", "\t") else line
            if needle in content.strip().lower():
                return {"old_line": old_line, "new_line": new_line}, "context"
            old_line += 1
            new_line += 1
    return None


def _anchor_by_new_line(diff_text: str, new_line: int) -> dict[str, Any] | None:
    """Resolve a line_range point by its NEW-file line number.

    Added lines yield ``{"type": "new", "new_line": N}``; context lines yield
    both numbers with ``type: None`` (the old side is resolved automatically).
    Returns ``None`` when no line of the new file has this number.
    """
    old_line = 0
    cur_new_line = 0
    in_hunk = False
    for line in diff_text.split("\n"):
        m = _HUNK_RE.match(line)
        if m:
            old_line = int(m.group(1))
            cur_new_line = int(m.group(2))
            in_hunk = True
            continue
        if not in_hunk or not line or line[0] == "\\":
            continue
        prefix = line[0]
        if prefix == "+":
            if cur_new_line == new_line:
                return {"type": "new", "new_line": cur_new_line}
            cur_new_line += 1
        elif prefix == "-":
            old_line += 1
        else:
            if cur_new_line == new_line:
                return {"type": None, "old_line": old_line, "new_line": cur_new_line}
            old_line += 1
            cur_new_line += 1
    return None


def _single_line_range(old_line: int | None, new_line: int | None) -> dict[str, Any]:
    """Build a degenerate line_range (start == end) from a single anchor."""
    if old_line is not None and new_line is not None:
        point: dict[str, Any] = {"type": None, "old_line": old_line, "new_line": new_line}
    elif new_line is not None:
        point = {"type": "new", "new_line": new_line}
    elif old_line is not None:
        point = {"type": "old", "old_line": old_line}
    else:
        msg = "A single-line range requires old_line and/or new_line"
        raise ValueError(msg)
    return {"start": dict(point), "end": dict(point)}


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
    line_range: dict[str, Any] | None = None,
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
        if line_range is not None:
            position["line_range"] = line_range
            end_point = line_range.get("end") or {}
            if old_line is None and end_point.get("old_line") is not None:
                old_line = end_point["old_line"]
            if new_line is None and end_point.get("new_line") is not None:
                new_line = end_point["new_line"]
        elif old_line is not None or new_line is not None:
            position["line_range"] = _single_line_range(old_line, new_line)
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
        str | None,
        Field(
            description=(
                "Fragment of the START line, unique in the file diff, "
                "case-insensitive. Only way to anchor a REMOVED line (with "
                "side='old'). Ignored when start_line is set."
            ),
            min_length=1,
        ),
    ] = None,
    side: Annotated[
        str,
        Field(
            description="'new' for added/context lines, 'old' for removed; only with search_text",
        ),
    ] = "new",
    start_line: Annotated[
        int | None,
        Field(
            description=(
                "START line number in the NEW file (numbering of the head "
                "version). Covers added and context lines; old side is "
                "resolved automatically. Takes precedence over search_text."
            ),
            ge=1,
        ),
    ] = None,
    end_line: Annotated[
        int | None,
        Field(
            description=(
                "END line number (new file) for a multi-line comment. Omit for single-line."
            ),
            ge=1,
        ),
    ] = None,
) -> str:
    """Create a draft note anchored to a line or line range in a MR file.

    Anchor the START line by ``start_line`` (NEW-file numbering; covers added
    and context lines — the old side is resolved automatically) or by
    ``search_text`` (fragment of the START line; with ``side="old"`` it
    anchors a REMOVED line). Pass ``end_line`` for a multi-line range. The
    top-level position numbers are taken from the range END.
    """
    try:
        _check_write(ctx)
        if end_line is not None and start_line is None and search_text is None:
            msg = "Provide a start anchor (start_line or search_text) for the range end"
            return _err(ValueError(msg))
        if start_line is None and search_text is None:
            msg = "Provide start_line or search_text"
            return _err(ValueError(msg))

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

        if start_line is not None:
            start_point = _anchor_by_new_line(diff_text, start_line)
            if start_point is None:
                msg = (
                    f"start_line {start_line} not found on the new-file side of the diff "
                    f"for {file_path}"
                )
                return _err(ValueError(msg))
        else:
            anchor = _resolve_anchor_in_diff(diff_text, cast("str", search_text), side)
            if anchor is None:
                msg = f"Search text not found in diff for {file_path}"
                return _err(ValueError(msg))
            anchor_fields, line_kind = anchor
            start_point = {"type": _LINE_KIND_TO_TYPE[line_kind], **anchor_fields}

        if end_line is None:
            line_range: dict[str, Any] = {"start": start_point, "end": dict(start_point)}
        else:
            end_point = _anchor_by_new_line(diff_text, end_line)
            if end_point is None:
                msg = (
                    f"end_line {end_line} not found on the new-file side of the diff "
                    f"for {file_path}"
                )
                return _err(ValueError(msg))
            if (
                start_point.get("new_line") is not None
                and end_point.get("new_line") is not None
                and start_point["new_line"] > end_point["new_line"]
            ):
                msg = (
                    f"Invalid range: start new_line {start_point['new_line']} is greater "
                    f"than end new_line {end_point['new_line']}"
                )
                return _err(ValueError(msg))
            if (
                start_point.get("old_line") is not None
                and end_point.get("old_line") is not None
                and start_point["old_line"] > end_point["old_line"]
            ):
                msg = (
                    f"Invalid range: start old_line {start_point['old_line']} is greater "
                    f"than end old_line {end_point['old_line']}"
                )
                return _err(ValueError(msg))
            line_range = {"start": start_point, "end": end_point}

        data = await _create_draft_note_impl(
            ctx,
            project_id,
            mr_iid,
            body,
            base_sha=base_sha,
            head_sha=head_sha,
            start_sha=start_sha,
            old_path=file_path,
            new_path=file_path,
            line_range=line_range,
        )
        return _ok(data)
    except Exception as e:
        return _err(e)
