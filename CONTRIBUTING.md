# Contributing

Thanks for helping. Reports from SolidWorks versions other than 2026, and from real design work, are
especially valuable. Please include the output of `solidworks-mcp doctor` and, if you can, the relevant
lines of the audit log.

## Development setup

```powershell
git clone https://github.com/0xFFD/solidworks-mcp; cd solidworks-mcp
uv sync                     # creates .venv with dev dependencies
uv run pytest               # unit tests everywhere; integration tests when SolidWorks is installed
uv run ruff check src tests
```

Run the server against a scratch workspace while developing:

```powershell
uv run solidworks-mcp serve --workspace C:\temp\swmcp
npx @modelcontextprotocol/inspector uv run solidworks-mcp serve --workspace C:\temp\swmcp
```

## Guidelines

- Keep tools general. Prefer improving a primitive over adding a tool for one kind of part.
- Tool output is read by language models: keep it compact and actionable, and return what the agent
  needs to check its work (feature name, rebuild state, volume).
- Lengths are millimetres and angles degrees at the tool boundary. Convert with `com.mm`, `com.rad`,
  `com.to_mm` and `com.to_deg`, never inline.
- File paths go through `App.path`, which enforces the allowed folders.
- Every COM call runs on the COM thread. Tools registered with `@tool` already do; helper code called
  from elsewhere must use `com.on_com`.
- Look up SolidWorks enum values through `SW.C` and decode result codes with `com.const_name`. Never
  hard-code the numbers.
- When a SolidWorks call reports success but the result could still be wrong (hole placement, STEP
  contents, GD&T symbols), read the result back and fail loudly if it does not match.
- Add a unit test for pure logic, and extend `tests/test_integration.py` when you add or change a tool.
  Integration tests must close only the documents they create.
