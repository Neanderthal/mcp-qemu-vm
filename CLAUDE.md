# CLAUDE.md

MCP QEMU VM Control — FastMCP server for controlling QEMU VMs via SSH (xdotool, scrot, asyncssh). Single-file app in `server.py`.

## Commands

```bash
uv venv && source .venv/bin/activate && uv pip install -r requirements.txt
python server.py                          # run server
uv run mcp dev server.py                  # MCP Inspector
uv run ruff check server.py               # lint
pytest test_ssh_tools.py                   # tests
```

## Config

Env vars (or `.env`): `VM_HOST`, `VM_USER`, `VM_PORT`, `VM_DISPLAY`, `VM_IDENTITY`, `VM_KNOWN_HOSTS`, `VM_CONNECT_TIMEOUT`. See `.env.example`.

## Architecture

`server.py`: `Project` class (folder structure), `AppContext` (SSH + project state + `DisplayCalibration`), `lifespan()` (connection lifecycle + display calibration), `run_vm_cmd()` (shell helper). Tools: project management, UI automation (xdotool), screenshots (scrot), SSH ops, advice system. `DisplayCalibration` auto-detects HiDPI/scaling mismatches at startup; coordinate tools scale transparently. All outputs go to `data/projects/`.

## Rules

See `.claude/rules/` for detailed rules on architecture, security, and conventions.
