# Security Policy

bean runs entirely on your machine: it pulls with your own credentials, embeds locally, and stores
everything under `~/.bean/` (credentials at `~/.bean/credentials/`, mode `0600`). There is no server
and no telemetry. The security surface is therefore local — credential handling, the drop-in plugin
loader, and the SQL/OCR/embedder code paths that execute or read your data.

## Supported Versions

bean is pre-1.0, so only the latest release and `main` receive security fixes.

| Version | Supported |
|---------|-----------|
| latest release (`0.1.x`) | ✅ |
| `main` | ✅ |
| older tags | ❌ — upgrade to the latest |

## Reporting a Vulnerability

Report privately — **do not open a public issue**.

- **Preferred:** GitHub's private vulnerability reporting. On the repo, go to the **Security** tab →
  **Report a vulnerability**. This opens a private advisory only maintainers can see.
- **Or email:** git@danielhenneberger.com with `bean security` in the subject.

Please include the affected version or commit, steps to reproduce (or a proof of concept), the
impact, and any suggested fix.

**What to expect:** an acknowledgement within **5 business days**, and an assessment with next steps
within **10 business days**. If a report is confirmed, a fix and a released patch are the priority
over any deadline.

## Disclosure

Please give us a reasonable window to ship a fix before disclosing publicly (coordinated
disclosure). We'll credit reporters in the release notes unless you'd rather stay anonymous.
