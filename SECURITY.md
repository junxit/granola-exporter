# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
open the repository's **Security** tab and choose **Report a vulnerability**.

Please do not open a public issue for an undisclosed vulnerability.

## Supported versions

This is a personal tool. Only the current `main` branch is supported.

## Scope and threat model

`granola-exporter` is a local, read-only client for the Granola public API. It
holds a `grn_` API key and writes meeting transcripts to disk, so the security
concerns that matter most are:

- **Local disclosure.** The archive can contain highly sensitive meeting
  content. Archive files are written `0600` and directories `0700` so they are
  readable only by the owning user. `.env` holds a live API key and should be
  `chmod 600`. Neither `.env` nor `archive/` is ever committed — both are
  gitignored.
- **Untrusted API responses.** Note ids are validated against
  `^not_[a-zA-Z0-9]{14}$` before being used to build filesystem paths or request
  URLs, and every archive path is checked to resolve inside the archive root.
  A malicious or compromised API server therefore cannot write outside the
  archive directory.
- **Local index tampering.** `index.json` is treated as untrusted input; entries
  resolving outside the archive root are refused rather than followed.

Out of scope: the security of the Granola service itself, and anything
requiring an attacker who already has code execution as your user.

## Dependencies

Dependencies are pinned in `uv.lock` and scanned against the OSV database by CI
on every push and pull request, plus weekly on a schedule, so a newly published
advisory surfaces even when nothing changes. Dependabot opens update pull
requests weekly.
