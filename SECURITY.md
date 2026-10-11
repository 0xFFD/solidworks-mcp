# Security policy

## Model

solidworks-mcp lets an AI agent operate SolidWorks on your workstation. It is designed for engineering
environments that handle sensitive and export-controlled data:

- It communicates over stdio only. It opens no network ports, makes no network requests and collects
  no telemetry.
- It exposes a fixed set of tools. There is no tool that runs macros, scripts or arbitrary API calls.
- File paths are confined to the workspace and folders added with `--root`; absolute paths elsewhere
  and `..` escapes are rejected.
- Every tool call is recorded in a hash-chained audit log that `solidworks-mcp verify-audit` checks.
- `--read-only` limits the agent to tools that do not change models or files.
- `--require-marking` blocks neutral-format exports of documents without an export-control marking.

What it does not do:

- It does not control what the connected AI model sees. Tool results (dimensions, names, file paths,
  images of the model) are sent to whichever model the client uses. Confirm that the model and client
  are approved for your data, especially for ITAR or EAR controlled designs, before connecting them.
- It is not a sandbox. SolidWorks itself runs with your Windows permissions, and documents you open
  can reference files outside the allowed folders.
- The audit log proves integrity, not existence: someone with access to the file can delete it
  entirely. Ship it to write-once storage if that matters.
- It does not certify compliance with AS9100, ITAR, EAR, CMMC or any other standard. Those are
  obligations of the organisation using it.

Recommendations:

- Run the server under the engineer's own account, with the workspace on a drive covered by your data
  handling rules.
- Use `--require-marking` and a marking property that matches your title blocks.
- Use `--read-only` for review sessions.
- Review every model and drawing an agent produces before release.

## Reporting a vulnerability

Please report security issues privately through GitHub's "Report a vulnerability" (Security Advisories)
on the repository rather than in public issues. Include steps to reproduce and the affected version.
We aim to respond within a week.
