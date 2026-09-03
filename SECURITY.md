# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
open the repository's **Security** tab and choose **Report a vulnerability**.

Please do not open a public issue for an undisclosed vulnerability.

## Supported versions

This is a personal tool. Only the current `main` branch is supported.

## Scope and threat model

`granola-exporter` is a local, read-only client for two Granola backends: the
public API (a `grn_` key) and the Granola MCP (an OAuth token). It writes
meeting transcripts to disk, so the concerns that matter most are:

- **Local disclosure.** The archive can contain highly sensitive meeting
  content. Archive files are written `0600` and directories `0700` so they are
  readable only by the owning user. `.env` holds a live API key and should be
  `chmod 600`. Neither `.env` nor `archive/` is ever committed — both are
  gitignored.
- **Credential storage.** The MCP OAuth token is written `0600` in a `0700`
  directory at `$XDG_STATE_HOME/granola-exporter/mcp-oauth.json`, deliberately
  **outside the archive**: the archive is something a user may back up or sync
  to another machine, and credentials must not travel with it. The file records
  which endpoint it belongs to, so repointing `GRANOLA_MCP_URL` never silently
  reuses another server's credentials. `granola-export logout` removes it, and
  `logout --all` removes every profile's — note that both are local deletes,
  with no server-side revocation.
- **Credential profiles.** `--profile NAME` gives an account its own
  `mcp-oauth-<name>.json` in the same `0700` directory at the same `0600` mode,
  so a second account cannot silently overwrite the first. The name is
  validated against `^[a-z0-9][a-z0-9._-]{0,31}$` rather than slugified: it can
  only ever be a single path component, so it cannot escape the state
  directory, and two distinct names can never fold onto one credential file.
  Traversal-shaped names are refused before any path is built, with regression
  tests in `tests/test_security.py`. A named profile deliberately ignores
  `GRANOLA_MCP_TOKEN_FILE`, which names one exact file and cannot also hold a
  family of them.
- **The public API key is never written to disk by this tool.** It is read from
  the environment and sent as a bearer header, so `.env` is the only place it
  persists — omitting `.env` in favor of a per-session environment variable
  leaves no credential on disk at all.
- **The OAuth redirect.** The loopback listener binds `127.0.0.1` explicitly,
  serves exactly one request on `/callback`, refuses any other path, and never
  reflects query parameters into the response body. Only `granola-export login`
  ever starts an authorization flow; `sync` and `doctor` will not.
- **Untrusted API responses.** Ids are validated before being used to build
  filesystem paths or request URLs — `^not_[a-zA-Z0-9]{14}$` for the public API,
  and `^mcp_` plus a canonical lowercase UUID for MCP notes — and every archive
  path is checked to resolve inside the archive root. A malicious or compromised
  server cannot write outside the archive directory.

  **Invariant:** widening the archive-path validator must never widen the
  URL-path validator. `mcp_*` keys may become directories, but
  `public_api.get_note` still refuses them before issuing a request. There is a
  regression test asserting exactly that.
- **Parsing model-facing text.** The MCP returns prose shaped for a language
  model, wrapped in a prompt-injection preamble, with meeting summaries carrying
  arbitrary Markdown. That text is **parsed as data and never given to a
  model**. It is also parsed **without an XML parser** — the payload is sliced
  structurally and attribute values are unescaped explicitly — so entity
  expansion, billion-laughs and XXE are out of the threat model by construction
  rather than by configuring a parser correctly. Document type and entity
  declarations are rejected outright and responses are size-capped.
- **Silent truncation.** For an archival tool, quietly archiving less than
  exists is a security-relevant failure. The MCP has no pagination, so the
  envelope's declared `count` is reconciled against the elements parsed and any
  mismatch raises; an unparseable response raises rather than looking like an
  empty window; and oversized listings are bisected, with anything still at the
  cap reported as possibly incomplete.
- **Local index tampering.** `index.json` is treated as untrusted input; entries
  resolving outside the archive root are refused rather than followed.

Out of scope: the security of the Granola service itself, and anything
requiring an attacker who already has code execution as your user.

## Dependencies

Dependencies are pinned in `uv.lock` and scanned against the OSV database by CI
on every push and pull request, plus weekly on a schedule, so a newly published
advisory surfaces even when nothing changes. Dependabot opens update pull
requests weekly.

Adding the MCP backend took the lockfile from 14 to 40 packages. `mcp` brings
its own HTTP stack (`httpx2`, alongside the `httpx` the public API client uses)
plus an ASGI server stack — `starlette`, `uvicorn`, `python-multipart` — that
this tool never executes, since it is only ever an MCP *client*. That is a real
cost: advisories against server-side code paths we do not run will still fail
CI. It is accepted deliberately, and mitigated by confining the SDK behind
`mcp_api.MCPClient` and importing it lazily, so the public API path loads none
of it and the SDK can be replaced in one file.
