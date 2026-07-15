# mcp-gitlab — Fork Context

This repo is a local fork of [`vish288/mcp-gitlab`](https://github.com/zereight/mcp-gitlab),
installed locally from `D:\github\mcp-gitlab` (no PyPI release of the fork). The
fork adds repository navigation/search tools without diverging from the upstream
core, so rebases stay cheap.

## Fork Rules (follow strictly)

1. **Do not edit the upstream core.** The files below are treated as upstream and
   must stay untouched (modulo rebase/merge):
   - `src/mcp_gitlab/client.py` (do NOT add new methods to `GitLabClient`)
   - `src/mcp_gitlab/servers/gitlab.py` (do NOT register new tools here)
   - `src/mcp_gitlab/__init__.py` is the ONLY core file touched — and only by one
     import line that loads fork extension modules.

2. **New tools go in extension modules** under `src/mcp_gitlab/servers/`, modeled
   on `servers/review.py` and `servers/repository.py`. Each module:
   - imports the shared helpers from `.gitlab` (`_err`, `_ok`, `_get_client`,
     `_paginated`, `mcp`, …),
   - registers tools with `@mcp.tool(...)` against that shared `mcp` instance,
   - is wired up by a single name added to the lazy import in `__init__.py`.

3. **Call the public client API only.** Fork tools must use `client.get(...)`
   (and `_get_client(ctx)`) directly — never add new methods to `GitLabClient`.
   Do pagination locally (see `_paginated_get` in `repository.py`).

4. **`GitLabClient._encode_id` is private but reused.** It is the canonical
   project/group ID encoder (numeric passthrough, URL-encoded paths, full-URL
   reduction). Fork modules call `client._encode_id(...)` for `project_id` and
   `quote(path, safe="")` for `file_path`. If upstream renames/changes it, the
   fork module must be updated on rebase.

5. **Naming & access.** Tool names follow `gitlab_{verb}_{resource}`. Names that
   match existing Kilo permission globs (`gitlab_gitlab_get_*`, `..._list_*`)
   need no new rule. New verbs (e.g. `search_*`) require one allow-rule entry per
   agent in the Kilo config (`kilo.jsonc`).

6. **Docs/tests.** Fork additions get their own docs (`AGENTS.fork.md`) and tests
   (`tests/unit/test_<module>_tools.py`) mirroring `test_tools.py`. The upstream
   documentation files (`README.md`, `llms*.txt`, `server.json`, `GEMINI.md`)
   are NOT modified — they describe the released upstream tool set.

## Fork Extension Modules

### `servers/repository.py` — repository navigation (6 tools, all read-only)

| Tool | REST v4 endpoint | Notes |
|---|---|---|
| `gitlab_list_tree` | `GET /projects/:id/repository/tree` | Recursive tree for any ref (branch/tag/SHA). |
| `gitlab_get_file_metadata` | `GET /projects/:id/repository/files/:path` | Metadata only (no base64 `content`). |
| `gitlab_get_blob` | `GET /projects/:id/repository/blobs/:sha/raw` | Raw blob text by SHA. |
| `gitlab_search_file` | wraps `tree` + client-side `fnmatch` | Name/glob search across ANY ref (incl. tags/SHAs). |
| `gitlab_search_code` | `GET /projects/:id/search?scope=blobs` | Content search; default-branch only without Advanced Search. |
| `gitlab_search_blame` | `GET /projects/:id/repository/files/:path/blame` | Per-line commit attribution; `max_pages`-bounded. |

Kilo access: `gitlab_gitlab_get_*` / `..._list_*` (pre-existing) plus the new
`gitlab_gitlab_search_*` allow-rule cover all six tools.

### `servers/review.py` — code review tools (upstream-intended, pre-existing)

Draft notes, inline draft notes, raw file content. Documented in upstream `AGENTS.md`.
