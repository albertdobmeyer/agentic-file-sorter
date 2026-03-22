# Security Policy

## Supported Versions

Only the latest release is supported with security updates.

| Version | Supported |
|---------|-----------|
| 1.3.0+  | Yes       |
| < 1.3.0 | No        |

## Reporting a Vulnerability

Email **security@akd-automation.com** or open a private security advisory on GitHub.

Include:
- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Impact assessment (if known)

We aim to acknowledge reports within 48 hours and provide a fix or mitigation within 7 days for confirmed issues.

## Important: CDR Is Not Anti-Malware

AFS processes untrusted files using **Content Disarm & Reconstruction (CDR)** — images are re-rendered through Pillow to strip embedded payloads. This is a property of the method, not a security guarantee. AFS is a file organizer, not anti-malware software.

CDR reduces risk but does not eliminate it. If you are processing files from untrusted sources:

- Always use `--dry-run` first to preview what AFS will do
- Use `-o <output-dir>` to write sorted files to a separate directory instead of modifying the source
- Do not disable CDR (`--no-sanitize`) on untrusted files unless you understand the implications
