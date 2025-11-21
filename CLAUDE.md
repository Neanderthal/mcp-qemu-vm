# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP QEMU VM Control is a Model Context Protocol server for controlling QEMU virtual machines via SSH. It enables LLMs to interact with VMs through mouse/keyboard control, screenshots, and SSH command execution.

## Development Commands

```bash
# Install dependencies
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

# Run the MCP server
python server.py

# Development/testing with MCP Inspector
uv run mcp dev server.py

# Run with custom VM config
VM_HOST=192.168.122.10 VM_USER=vmrobot uv run mcp dev server.py

# Run tests
pytest test_ssh_tools.py
```

## Architecture

The server is a single-file FastMCP application (`server.py`) that:

1. **SSH Connection Management**: Uses `asyncssh` with a lifespan context manager to maintain a persistent SSH connection to the VM
2. **Project Management**: Organizes all outputs (screenshots, logs, results) into project folders under `data/projects/`
3. **MCP Tools**: Exposes tools for project management, mouse/keyboard control (via `xdotool`), screenshots (via `scrot`), and SSH operations
4. **Resources**: Screenshots are stored in project folders and exposed as MCP resources at `vm://screenshot/{id}`

### Key Components in server.py

- `Project` class manages project folder structure (`screenshots/`, `logs/`, `results/`, `advice/`) and metadata
- `AppContext` dataclass holds the SSH connection and current project
- `lifespan()` async context manager creates/closes SSH connection
- Tools use `ctx.request_context.lifespan_context` to access shared connection and project
- `run_vm_cmd()` helper executes commands on the VM with `DISPLAY` env var set

### Tool Categories

- **Project Management**: `project_init`, `project_info`, `project_list`, `project_load`, `project_log`, `project_read_logs`, `project_save_result` - must call `project_init` before using screenshots
- **Advice System**: `project_save_advice`, `project_read_advice` - save/read tips and lessons learned for future LLM sessions
- **UI Automation**: `move_mouse`, `click`, `type_text`, `press_keys`, `wait` - use xdotool via SSH
- **SSH Operations**: `ssh_execute`, `ssh_upload`, `ssh_download`, `ssh_connection_info` - direct VM access
- **Screenshots**: `take_screenshot` - requires active project, saves to project's screenshots folder

### Automatic Logging

All tool calls are automatically logged to the project's `logs/project.log` when a project is active:
- Tool name and parameters
- Success/failure results
- Errors with level=ERROR
- Sensitive data (long text in `type_text`) is truncated in logs

Use `project_read_logs(lines=50, level_filter="ERROR")` to review what happened.

### Advice System

The advice system allows LLMs to save and retrieve tips/lessons learned for future sessions:

- **Saving advice**: Use `project_save_advice(title, content)` to record environment-specific tips, lessons learned, or important information
- **Reading advice**: Use `project_read_advice()` to get all saved advice
- **Automatic loading**: When `project_load()` is called, any existing advice is automatically shown in the response

Example advice to save:
- Focus management quirks in nested environments (Citrix, VMs)
- Application-specific keyboard shortcuts
- Common pitfalls and how to avoid them
- Environment setup notes

### Typical Workflow

```
1. project_init("my-task", "Description")  # Creates data/projects/YYYYMMDD-HHMMSS_my-task/
2. take_screenshot()                        # Saves to project's screenshots/
3. ... perform VM operations ...            # All tool calls automatically logged
4. project_read_logs()                      # Review what happened
5. project_save_result("output.txt", data)  # Saves to project's results/
6. project_save_advice("Title", "Content")  # Save tips for future sessions
7. project_info()                           # View project statistics
```

For continuing work on an existing project:
```
1. project_load("data/projects/...")        # Loads project AND shows any saved advice
2. ... continue work, applying advice ...
3. project_save_advice("New tip", "...")    # Add more advice as you learn
```

## Configuration

Environment variables (or `.env` file):
- `VM_HOST` - VM IP address (default: 192.168.122.79)
- `VM_USER` - SSH username (default: vmrobot)
- `VM_PORT` - SSH port (default: 22)
- `VM_DISPLAY` - X11 display (default: :0)
- `VM_IDENTITY` - SSH private key path (optional)

## VM Requirements

The target VM needs: `openssh`, `xdotool`, `scrot`, `xorg-xrandr`, `xorg-xinput`
