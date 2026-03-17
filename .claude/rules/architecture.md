# Architecture

## Key Components (server.py)

- **`Project`** class: manages folder structure (`screenshots/`, `logs/`, `results/`, `advice/`) and metadata under `data/projects/YYYYMMDD-HHMMSS_name/`
- **`DisplayCalibration`** dataclass: stores xdotool vs screenshot coordinate scale factors; computed at startup by `_calibrate_display()`
- **`AppContext`** dataclass: holds SSH connection + `DisplayCalibration` + current project; shared via `ctx.request_context.lifespan_context`
- **`lifespan()`** async context manager: creates SSH connection, runs display calibration, yields `AppContext`, cleans up on shutdown
- **`run_vm_cmd()`** helper: runs a shell command on the VM with `check=True`, returns stdout
- **`_calibrate_display()`**: probes `xdotool getdisplaygeometry` and `scrot`+`file` to detect HiDPI/scaling mismatches; falls back to 1:1 on failure
- **`_scale_input()` / `_scale_output()`**: convert coordinates between screenshot-space (what the LLM sees) and xdotool-space (what xdotool uses); applied at public tool boundaries only

## Tool Categories

- **Project Management**: `project_init`, `project_info`, `project_list`, `project_load`, `project_log`, `project_read_logs`, `project_save_result`
- **Advice System**: `project_save_advice`, `project_read_advice` — tips persisted per-project for future sessions
- **UI Automation**: `move_mouse`, `get_active_window_info`, `click_in_window`, `click`, `type_text`, `press_keys`, `wait`, `run_actions` — via xdotool over SSH
- **SSH Operations**: `ssh_execute`, `ssh_upload`, `ssh_download`, `ssh_connection_info`
- **Display Calibration**: `display_calibration_info` — shows/re-probes coordinate scale factors
- **Screenshots**: `take_screenshot` — via scrot, saved to project folder, exposed as MCP resource `vm://screenshot/{id}`

## Automatic Logging

All tool calls are logged to `project/logs/project.log` when a project is active. Use `project_read_logs(level_filter="ERROR")` to review.

## Code Conventions

- **Python 3.12+**, single-file (`server.py`)
- MCP tool function signatures are **public API** — changes to parameter names/types are breaking
- Lint with `ruff check server.py` (config in `pyproject.toml`)
- VM requirements: `openssh`, `xdotool`, `scrot`, `xorg-xrandr`, `xorg-xinput`
