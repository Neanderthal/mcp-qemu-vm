import asyncio
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncssh
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

# ---------- Config ----------
VM_HOST = os.getenv("VM_HOST", "192.168.122.79")
VM_USER = os.getenv("VM_USER", "vmrobot")
VM_PORT = int(os.getenv("VM_PORT", "22"))
VM_DISPLAY = os.getenv("VM_DISPLAY", ":0")
VM_IDENTITY = os.getenv("VM_IDENTITY", "")  # path to private key, optional
VM_DESKTOP_USER = os.getenv("VM_DESKTOP_USER", "")  # if different from VM_USER
# UTF-8 locale for `xdotool type`. The vmrobot/desktop SSH environment often has
# no UTF-8 LC_CTYPE, so multi-byte input (Cyrillic, etc.) fails with "Invalid
# multi-byte sequence". Forcing LC_ALL here makes type_text work for non-ASCII.
VM_LOCALE = os.getenv("VM_LOCALE", "C.UTF-8")

PROJECTS_DIR = pathlib.Path("data/projects")
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Every legitimate xdotool key name (Return, BackSpace, Ctrl, F12, KP_Enter, space)
# matches this pattern. Rejects shell metacharacters like ; $ ` |
VALID_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

# xdotool button numbers
BUTTON_MAP = {"left": 1, "middle": 2, "right": 3}


# ---------- xdotool command builders ----------
# Shared by the standalone tools and the run_actions() batch loop so the two
# never diverge. Each takes an already-shlex-quoted display string and returns
# a shell command to run on the VM. Caller is responsible for execution.


def _type_cmd(display_q: str) -> str:
    """Build the `xdotool type` command. Text is piped via stdin (--file -),
    so it never touches the shell. LC_ALL forces a UTF-8 locale so non-ASCII
    (Cyrillic, etc.) decodes correctly."""
    return (
        f"LC_ALL={shlex.quote(VM_LOCALE)} DISPLAY={display_q} "
        "xdotool type --delay 10 --clearmodifiers --file -"
    )


def _keys_cmd(display_q: str, keys: list[str]) -> str:
    """Build an `xdotool key` command from a validated key list."""
    for k in keys:
        if not VALID_KEY_PATTERN.match(k):
            raise ValueError(f"Invalid key name: {k!r}")
    combo = "+".join(k.lower() for k in keys)
    return f"DISPLAY={display_q} xdotool key {shlex.quote(combo)}"


def _click_cmd(display_q: str, button: str, count: int) -> str:
    """Build an `xdotool click` command. Count is clamped to >= 1 (xdotool
    rejects --repeat 0)."""
    if button not in BUTTON_MAP:
        raise ValueError("button must be left/middle/right")
    count = max(1, int(count))
    return f"DISPLAY={display_q} xdotool click --repeat {count} {BUTTON_MAP[button]}"


def _move_cmd(display_q: str, sx: int, sy: int, mode: str) -> str:
    """Build an `xdotool mousemove` command (absolute or relative)."""
    if mode == "absolute":
        return f"DISPLAY={display_q} xdotool mousemove --sync {sx} {sy}"
    if mode == "relative":
        return f"DISPLAY={display_q} xdotool mousemove_relative --sync {sx} {sy}"
    raise ValueError("mode must be 'absolute' or 'relative'")


# ---------- Project Management ----------


@dataclass
class Project:
    """Manages a project's folder structure and metadata."""

    name: str
    path: pathlib.Path
    created_at: str
    description: str = ""

    @classmethod
    def create(cls, name: str, description: str = "") -> "Project":
        """Create a new project with folder structure."""
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        project_path = PROJECTS_DIR / f"{timestamp}_{name}"

        # Create folder structure
        project_path.mkdir(parents=True, exist_ok=True)
        (project_path / "screenshots").mkdir(exist_ok=True)
        (project_path / "logs").mkdir(exist_ok=True)
        (project_path / "results").mkdir(exist_ok=True)
        (project_path / "advice").mkdir(exist_ok=True)

        project = cls(
            name=name,
            path=project_path,
            created_at=timestamp,
            description=description,
        )
        project._save_metadata()
        project._log("Project initialized")
        return project

    @classmethod
    def load(cls, project_path: pathlib.Path) -> "Project":
        """Load an existing project from its metadata."""
        metadata_file = project_path / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"No metadata.json in {project_path}")

        with open(metadata_file) as f:
            data = json.load(f)

        return cls(
            name=data["name"],
            path=project_path,
            created_at=data["created_at"],
            description=data.get("description", ""),
        )

    def _save_metadata(self) -> None:
        """Save project metadata to JSON file."""
        metadata = {
            "name": self.name,
            "created_at": self.created_at,
            "description": self.description,
        }
        with open(self.path / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

    def _log(self, message: str, level: str = "INFO") -> None:
        """Append a log entry to the project log file."""
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        log_file = self.path / "logs" / "project.log"
        with open(log_file, "a") as f:
            f.write(log_line)

    def log(self, message: str, level: str = "INFO") -> str:
        """Public logging method."""
        self._log(message, level)
        return f"Logged: [{level}] {message}"

    def screenshot_path(self, screenshot_id: str) -> pathlib.Path:
        """Get the path for a screenshot in this project."""
        return self.path / "screenshots" / f"{screenshot_id}.png"

    def save_result(self, filename: str, content: str) -> pathlib.Path:
        """Save a result file to the project.

        The filename is reduced to its basename to prevent path traversal
        (e.g. ``../../etc/passwd``) escaping the results directory.
        """
        safe_name = pathlib.Path(filename).name
        if not safe_name or safe_name in (".", ".."):
            raise ValueError(f"Invalid result filename: {filename!r}")
        result_path = self.path / "results" / safe_name
        with open(result_path, "w") as f:
            f.write(content)
        self._log(f"Result saved: {safe_name}")
        return result_path

    def save_advice(self, title: str, content: str) -> pathlib.Path:
        """Save an advice/tip for future LLM sessions."""
        # Create a safe filename from title
        safe_title = "".join(c if c.isalnum() or c in "- _" else "_" for c in title)
        safe_title = safe_title[:50]  # Limit length
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        filename = f"{timestamp}_{safe_title}.md"

        advice_path = self.path / "advice" / filename
        with open(advice_path, "w") as f:
            f.write(f"# {title}\n\n{content}\n")
        self._log(f"Advice saved: {title}")
        return advice_path

    def get_all_advice(self) -> list[dict]:
        """Get all advice files from the project."""
        advice_dir = self.path / "advice"
        if not advice_dir.exists():
            return []

        advice_list = []
        for advice_file in sorted(advice_dir.glob("*.md")):
            content = advice_file.read_text()
            # Extract title from first line (# Title format)
            lines = content.strip().split("\n")
            title = lines[0].lstrip("# ").strip() if lines else advice_file.stem
            body = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
            advice_list.append(
                {
                    "title": title,
                    "content": body,
                    "file": advice_file.name,
                }
            )
        return advice_list

    def get_info(self) -> dict:
        """Get project information and statistics."""
        screenshots = list((self.path / "screenshots").glob("*.png"))
        results = list((self.path / "results").glob("*"))
        log_file = self.path / "logs" / "project.log"
        log_lines = log_file.read_text().count("\n") if log_file.exists() else 0

        return {
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at,
            "description": self.description,
            "screenshot_count": len(screenshots),
            "result_count": len(results),
            "log_entries": log_lines,
        }


# ---------- SSH connection management ----------


@dataclass
class DisplayCalibration:
    """Scale factors between xdotool coordinate space and screenshot pixels."""

    xdotool_w: int
    xdotool_h: int
    screenshot_w: int
    screenshot_h: int
    scale_x: float  # xdotool_w / screenshot_w
    scale_y: float  # xdotool_h / screenshot_h


@dataclass
class AppContext:
    ssh: asyncssh.SSHClientConnection
    calibration: DisplayCalibration
    project: Project | None = None


async def connect_ssh() -> asyncssh.SSHClientConnection:
    # known_hosts defaults to None for local ephemeral QEMU VMs whose host keys
    # change on every rebuild. Set VM_KNOWN_HOSTS to a path to enable verification.
    kwargs: dict = dict(
        host=VM_HOST,
        port=VM_PORT,
        username=VM_USER,
        known_hosts=os.getenv("VM_KNOWN_HOSTS") or None,
        connect_timeout=int(os.getenv("VM_CONNECT_TIMEOUT", "10")),
        keepalive_interval=30,
        keepalive_count_max=3,
    )
    if VM_IDENTITY:
        kwargs["client_keys"] = [VM_IDENTITY]

    return await asyncssh.connect(**kwargs)


async def run_vm_cmd(
    ssh: asyncssh.SSHClientConnection,
    cmd: str,
    *,
    as_desktop_user: bool = False,
) -> str:
    """Run a command inside the VM and return stdout.

    If *as_desktop_user* is True and VM_DESKTOP_USER is set, the command
    is wrapped with ``sudo -u <desktop_user>`` so it runs in the desktop
    user's context (needed for clipboard, pass, dbus, etc.).
    """
    if as_desktop_user and VM_DESKTOP_USER:
        cmd = f"sudo -u {shlex.quote(VM_DESKTOP_USER)} {cmd}"
    result = await ssh.run(cmd, check=True)
    return (result.stdout or "").strip()


async def _calibrate_display(
    ssh: asyncssh.SSHClientConnection,
) -> DisplayCalibration:
    """Probe the VM display to compute scale factors.

    Compares xdotool's coordinate space with the actual screenshot pixel
    dimensions.  Falls back to 1:1 scale on any failure (non-fatal).
    """
    fallback = DisplayCalibration(0, 0, 0, 0, 1.0, 1.0)
    display = shlex.quote(VM_DISPLAY)
    try:
        # 1) xdotool coordinate space
        geo = await run_vm_cmd(ssh, f"DISPLAY={display} xdotool getdisplaygeometry")
        xw, xh = (int(v) for v in geo.split())

        # 2) Take a calibration screenshot and read its pixel dimensions
        cal_path = "/tmp/mcp-calibrate.png"
        await run_vm_cmd(
            ssh,
            f"DISPLAY={display} scrot {shlex.quote(cal_path)}",
        )
        file_info = await run_vm_cmd(ssh, f"file {shlex.quote(cal_path)}")
        await run_vm_cmd(ssh, f"rm -f {shlex.quote(cal_path)}")

        # Parse "PNG image data, 1920 x 1080, ..." from `file` output
        m = re.search(r"(\d+)\s*x\s*(\d+)", file_info)
        if not m:
            print(
                "[calibration] Could not parse screenshot "
                f"dimensions from: {file_info}",
                file=sys.stderr,
            )
            return fallback
        sw, sh = int(m.group(1)), int(m.group(2))

        scale_x = xw / sw
        scale_y = xh / sh
        cal = DisplayCalibration(xw, xh, sw, sh, scale_x, scale_y)

        if scale_x == 1.0 and scale_y == 1.0:
            print("[calibration] 1:1, no scaling needed", file=sys.stderr)
        else:
            print(
                f"[calibration] xdotool={xw}x{xh}, screenshot={sw}x{sh}, "
                f"scale=({scale_x:.4f}, {scale_y:.4f})",
                file=sys.stderr,
            )
        return cal

    except Exception as exc:
        print(
            f"[calibration] Failed ({exc}), falling back to 1:1 scale",
            file=sys.stderr,
        )
        return fallback


def _scale_input(cal: DisplayCalibration, x: int, y: int) -> tuple[int, int]:
    """Convert screenshot-space coordinates to xdotool-space (multiply)."""
    return round(x * cal.scale_x), round(y * cal.scale_y)


def _scale_output(cal: DisplayCalibration, x: int, y: int) -> tuple[int, int]:
    """Convert xdotool-space coordinates to screenshot-space (divide)."""
    if cal.scale_x == 0 or cal.scale_y == 0:
        return x, y
    return round(x / cal.scale_x), round(y / cal.scale_y)


# ---------- MCP server setup ----------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    ssh = await connect_ssh()
    calibration = await _calibrate_display(ssh)
    try:
        yield AppContext(ssh=ssh, calibration=calibration)
    finally:
        ssh.close()
        await ssh.wait_closed()


mcp = FastMCP("QemuVMControl", lifespan=lifespan)

# ---------- Tools: mouse / keyboard / wait ----------


@mcp.tool()
async def move_mouse(
    x: int,
    y: int,
    mode: str = "absolute",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Move the mouse cursor.

    Args:
        x: X coordinate
        y: Y coordinate
        mode: "absolute" or "relative"

    Best Practice: Prefer keyboard shortcuts over mouse operations for reliability,
    especially in nested environments (Citrix, VMs). Mouse movements work but
    keyboard navigation is more consistent.
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration
    x, y = int(x), int(y)
    sx, sy = _scale_input(cal, x, y)
    display = shlex.quote(VM_DISPLAY)
    cmd = _move_cmd(display, sx, sy, mode)

    await run_vm_cmd(ssh, cmd)
    result = f"Mouse moved to ({x}, {y}) [{mode}]"
    _log_tool_call(ctx, "move_mouse", {"x": x, "y": y, "mode": mode})
    return result


def _parse_frame_extents(xprop_output: str) -> tuple[int, int, int, int]:
    """Parse _NET_FRAME_EXTENTS from xprop output.

    Returns (left, right, top, bottom). Defaults to (0, 0, 0, 0) if
    the property is missing (e.g. undecorated windows).
    """
    for line in xprop_output.splitlines():
        if "_NET_FRAME_EXTENTS" in line and "=" in line:
            _, _, values = line.partition("=")
            parts = [int(v.strip()) for v in values.split(",")]
            if len(parts) == 4:
                return (parts[0], parts[1], parts[2], parts[3])
    return (0, 0, 0, 0)


async def _get_active_window_geometry(
    ssh: asyncssh.SSHClientConnection,
) -> dict:
    """Get geometry, name, and frame extents of the active window.

    Returns a dict with keys: window_id, name, x, y, width, height,
    frame_left, frame_right, frame_top, frame_bottom, client_x, client_y.
    """
    display = shlex.quote(VM_DISPLAY)
    cmd = (
        f"WID=$(DISPLAY={display} xdotool getactivewindow) && "
        f"DISPLAY={display} xdotool getwindowgeometry --shell $WID && "
        f'echo "---NAME---" && '
        f"DISPLAY={display} xdotool getwindowname $WID && "
        f'echo "---FRAME---" && '
        f"xprop -display {display} -id $WID _NET_FRAME_EXTENTS 2>/dev/null || true"
    )
    output = await run_vm_cmd(ssh, cmd)

    # Parse getwindowgeometry --shell output (WINDOW=..., X=..., Y=..., etc.)
    geo: dict = {}
    name_section = False
    frame_section = False
    name_lines: list[str] = []
    frame_lines: list[str] = []

    for line in output.splitlines():
        if line.strip() == "---NAME---":
            name_section = True
            frame_section = False
            continue
        if line.strip() == "---FRAME---":
            name_section = False
            frame_section = True
            continue
        if frame_section:
            frame_lines.append(line)
        elif name_section:
            name_lines.append(line)
        elif "=" in line:
            key, _, val = line.partition("=")
            geo[key.strip()] = val.strip()

    window_id = int(geo.get("WINDOW", "0"))
    x = int(geo.get("X", "0"))
    y = int(geo.get("Y", "0"))
    width = int(geo.get("WIDTH", "0"))
    height = int(geo.get("HEIGHT", "0"))
    name = "\n".join(name_lines).strip()

    fl, fr, ft, fb = _parse_frame_extents("\n".join(frame_lines))

    return {
        "window_id": window_id,
        "name": name,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "frame_left": fl,
        "frame_right": fr,
        "frame_top": ft,
        "frame_bottom": fb,
        "client_x": x + fl,
        "client_y": y + ft,
    }


@mcp.tool()
async def get_active_window_info(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Get geometry and frame information about the currently focused window.

    Returns structured data about the active window including its position,
    size, decoration (frame) extents, and the computed client-area origin.
    Use this to understand where the window content starts on screen.

    Returned fields:
    - window_id, name: X11 window ID and title
    - x, y: top-left of the window frame on screen
    - width, height: window client-area dimensions
    - frame_left/right/top/bottom: decoration extents (CSD/SSD title bar, borders)
    - client_x, client_y: top-left of the content area in screen coords
      (computed as x + frame_left, y + frame_top)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration
    info = await _get_active_window_geometry(ssh)
    _log_tool_call(ctx, "get_active_window_info", {}, info["name"])

    # Convert xdotool-space coordinates to screenshot-space for the caller
    pos_x, pos_y = _scale_output(cal, info["x"], info["y"])
    w, h = _scale_output(cal, info["width"], info["height"])
    fl, fr = _scale_output(cal, info["frame_left"], info["frame_right"])
    ft, fb = _scale_output(cal, info["frame_top"], info["frame_bottom"])
    cx, cy = _scale_output(cal, info["client_x"], info["client_y"])

    lines = [
        f"Window ID: {info['window_id']}",
        f"Name: {info['name']}",
        f"Position: ({pos_x}, {pos_y})",
        f"Size: {w}x{h}",
        f"Frame extents: left={fl}, right={fr}, "
        f"top={ft}, bottom={fb}",
        f"Client area origin: ({cx}, {cy})",
    ]
    return "\n".join(lines)


@mcp.tool()
async def click_in_window(
    x: int,
    y: int,
    button: str = "left",
    count: int = 1,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Click at coordinates relative to the active window's client area.

    The server internally resolves the active window's position and frame
    (title bar / CSD) extents, then translates the given (x, y) to absolute
    screen coordinates before clicking. This eliminates all CSD/frame offset
    math from the caller — just pass the offset within the window content
    (e.g. from CSS coordinates or screenshot pixel measurements).

    Args:
        x: X offset within the window content area (0 = left edge)
        y: Y offset within the window content area (0 = top edge, below title bar)
        button: left/right/middle
        count: Number of clicks (1 for single, 2 for double)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration

    x, y, count = int(x), int(y), int(count)
    sx, sy = _scale_input(cal, x, y)

    info = await _get_active_window_geometry(ssh)
    screen_x = info["client_x"] + sx
    screen_y = info["client_y"] + sy

    display = shlex.quote(VM_DISPLAY)
    cmd = (
        f"{_move_cmd(display, screen_x, screen_y, 'absolute')} && "
        f"{_click_cmd(display, button, count)}"
    )
    await run_vm_cmd(ssh, cmd)

    result = (
        f"Clicked {button} x{count} at window-relative ({x}, {y}) "
        f"→ screen ({screen_x}, {screen_y})"
    )
    _log_tool_call(
        ctx,
        "click_in_window",
        {"x": x, "y": y, "button": button, "count": count},
        result,
    )
    return result


@mcp.tool()
async def click(
    button: str = "left",
    count: int = 1,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Click a mouse button.

    Args:
        button: left/right/middle
        count: Number of clicks (1 for single, 2 for double)

    ⚠️ WARNING: Mouse clicks do NOT reliably switch focus in nested environments
    (Citrix, remote desktop, high-latency connections). Use keyboard shortcuts
    like Ctrl+Shift+P to explicitly switch focus instead. Always verify with
    take_screenshot() before typing after a click.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    count = int(count)
    display = shlex.quote(VM_DISPLAY)
    cmd = _click_cmd(display, button, count)
    await run_vm_cmd(ssh, cmd)
    result = f"Clicked {button} x{count}"
    _log_tool_call(ctx, "click", {"button": button, "count": count})
    return result


@mcp.tool()
async def type_text(
    text: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Type literal text into the VM.

    ⚠️ CRITICAL: ALWAYS take_screenshot() first to verify focus before typing!
    Typing into the wrong window (e.g., Vim editor instead of terminal) will
    corrupt files. Check for visual indicators:
    - Cursor blinking in terminal = safe to type commands
    - Vim mode in status bar (INSERT/NORMAL) = DO NOT type commands
    - No cursor visible = STOP and screenshot first

    The text is typed with 10ms delay between characters for reliability.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    # Text goes to stdin via --file -, never touches the shell
    display = shlex.quote(VM_DISPLAY)
    cmd = _type_cmd(display)
    await ssh.run(cmd, input=text, check=True)
    # Mask sensitive text in logs (only show length)
    log_text = text if len(text) <= 20 else f"{text[:10]}...({len(text)} chars)"
    _log_tool_call(ctx, "type_text", {"text": log_text})
    return f"Typed {len(text)} characters"


@mcp.tool()
async def press_keys(
    keys: list[str],
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Press key combination, e.g. ["Ctrl", "L"] or ["Alt", "F4"].

    Best Practice: Keyboard shortcuts are MORE RELIABLE than mouse clicks for
    focus switching and navigation, especially in nested environments.
    Examples:
    - VS Code terminal focus: ["Ctrl", "Shift", "p"]
      then type "Terminal: Focus Terminal"
    - Escape from Vim: ["Escape"]
    - Common modifiers: Ctrl, Shift, Alt, Meta

    Always follow with wait() and take_screenshot() to verify the action completed.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    # xdotool uses 'ctrl+l', 'alt+F4', etc. (key names validated in _keys_cmd)
    display = shlex.quote(VM_DISPLAY)
    cmd = _keys_cmd(display, keys)
    await run_vm_cmd(ssh, cmd)
    result = f"Pressed keys: {keys}"
    _log_tool_call(ctx, "press_keys", {"keys": keys})
    return result


@mcp.tool()
async def wait(
    seconds: float,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Sleep/pause for specified seconds.

    ⚠️ REQUIRED: Always wait between actions in nested/remote environments!
    Recommended wait times:
    - After opening Command Palette: 0.5s
    - After typing search text: 0.3s
    - After pressing Enter/Return: 0.5-1.0s
    - After command execution: 1.0-2.0s (depends on command)
    - After window/focus switch: 0.5s

    Never rapid-fire actions - they may arrive out of order or fail silently
    due to Citrix/VM latency.
    """
    await asyncio.sleep(seconds)
    _log_tool_call(ctx, "wait", {"seconds": seconds})
    return f"Waited {seconds} seconds"


@mcp.tool()
async def run_actions(
    actions: list[dict],
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Execute a sequence of UI actions in one call to reduce latency.

    🚀 RECOMMENDED for nested/remote environments (Citrix, VMs)!
    Benefits:
    - Reduces round-trip latency (5 actions in 1 call vs 5 separate calls)
    - Critical for high-latency environments
    - Ensures sequential execution, stops on first error
    - Actions execute reliably in order

    Each action is a dict with "action" key and action-specific parameters.

    Supported actions:
    - {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]}
    - {"action": "type_text", "text": "hello"}
    - {"action": "click", "button": "left", "count": 1}
    - {"action": "move_mouse", "x": 100, "y": 200, "mode": "absolute"}
    - {"action": "wait", "seconds": 0.5}

    Example - switch to VS Code terminal reliably:
    [
        {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]},
        {"action": "wait", "seconds": 0.5},
        {"action": "type_text", "text": "Terminal: Focus Terminal"},
        {"action": "wait", "seconds": 0.3},
        {"action": "press_keys", "keys": ["Return"]},
        {"action": "wait", "seconds": 0.5}
    ]

    Returns:
        Summary of executed actions
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration
    results = []

    for i, action_def in enumerate(actions):
        action_type = action_def.get("action")

        try:
            display = shlex.quote(VM_DISPLAY)

            if action_type == "press_keys":
                keys = action_def.get("keys", [])
                await run_vm_cmd(ssh, _keys_cmd(display, keys))
                results.append(f"{i + 1}. press_keys {keys}")

            elif action_type == "type_text":
                text = action_def.get("text", "")
                await ssh.run(_type_cmd(display), input=text, check=True)
                results.append(f"{i + 1}. type_text ({len(text)} chars)")

            elif action_type == "click":
                button = action_def.get("button", "left")
                count = int(action_def.get("count", 1))
                await run_vm_cmd(ssh, _click_cmd(display, button, count))
                results.append(f"{i + 1}. click {button} x{count}")

            elif action_type == "move_mouse":
                x = int(action_def.get("x", 0))
                y = int(action_def.get("y", 0))
                mode = action_def.get("mode", "absolute")
                sx, sy = _scale_input(cal, x, y)
                await run_vm_cmd(ssh, _move_cmd(display, sx, sy, mode))
                results.append(f"{i + 1}. move_mouse ({x}, {y}) [{mode}]")

            elif action_type == "wait":
                seconds = action_def.get("seconds", 0.5)
                await asyncio.sleep(seconds)
                results.append(f"{i + 1}. wait {seconds}s")

            else:
                results.append(f"{i + 1}. UNKNOWN ACTION: {action_type}")

        except Exception as e:
            results.append(f"{i + 1}. ERROR in {action_type}: {str(e)}")
            _log_error(ctx, "run_actions", f"Action {i + 1} ({action_type}): {str(e)}")
            break  # Stop on error

    _log_tool_call(
        ctx, "run_actions", {"count": len(actions)}, f"executed {len(results)} actions"
    )
    return f"Executed {len(results)} actions:\n" + "\n".join(results)


# ---------- SSH Tools ----------


@mcp.tool()
async def ssh_execute(
    command: str,
    as_desktop_user: bool = False,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Execute an arbitrary shell command on the VM via SSH.

    ⚠️ CRITICAL LIMITATION: SSH only reaches the FIRST VM LAYER!

    If you're working with nested environments (VM → Citrix → Windows → VS Code),
    SSH commands do NOT reach those inner layers. For nested environments, use
    UI automation tools (type_text, press_keys, run_actions) instead.

    SSH is 20-40x faster than UI automation for VM tasks, so use it when possible
    for the first VM layer (package installs, file operations, system commands).

    Notes:
    - Commands run with vmrobot user permissions by default
    - Set as_desktop_user=True for commands needing the desktop
      user's context (clipboard, pass, dbus, etc.)
    - Uses persistent SSH connection (no reconnect overhead)
    - Returns stdout, stderr, and exit code
    - Use absolute paths for reliability

    Args:
        command: The shell command to execute on the first VM
        as_desktop_user: Run as the desktop session owner
            (VM_DESKTOP_USER) instead of VM_USER. Useful for
            clipboard, password manager, and dbus operations.

    Returns:
        Command output (stdout and stderr combined with exit code)

    Examples:
        - System info: "uname -a"
        - Install packages: "sudo pacman -Sy package-name"
        - Clipboard: as_desktop_user=True, "xclip -selection clipboard -o"
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        if as_desktop_user and VM_DESKTOP_USER:
            command = (
                f"sudo -u {shlex.quote(VM_DESKTOP_USER)} {command}"
            )
        result = await ssh.run(command, check=False)
        output_parts = []

        if result.stdout:
            output_parts.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"EXIT CODE: {result.returncode}")
            _log_tool_call(
                ctx,
                "ssh_execute",
                {"command": command},
                f"exit_code={result.returncode}",
            )
            _log_error(
                ctx, "ssh_execute", f"Command failed with exit code {result.returncode}"
            )
        else:
            _log_tool_call(ctx, "ssh_execute", {"command": command}, "success")

        return (
            "\n\n".join(output_parts)
            if output_parts
            else "Command completed (no output)"
        )
    except Exception as e:
        _log_error(ctx, "ssh_execute", str(e))
        return f"Error executing command: {str(e)}"


@mcp.tool()
async def ssh_upload(
    local_path: str,
    remote_path: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Upload a file from the host to the VM via SFTP.

    ⚠️ Note: This uploads to the first VM layer only. For nested environments,
    files must be transferred to inner layers using alternative methods.

    Notes:
    - Uses SFTP over the persistent SSH connection
    - Destination directory must exist (create with ssh_execute if needed)
    - Use absolute paths for reliability
    - Supports all file types (text, binary, archives)

    Best Practices:
    - Verify local file exists before uploading
    - Create destination directory first: ssh_execute("mkdir -p /path/to/dir")
    - For scripts, set permissions after upload:
      ssh_execute("chmod +x /path/to/script.sh")
    - Use tar/zip for multiple files

    Args:
        local_path: Path to the local file to upload (absolute or relative)
        remote_path: Destination path on the VM (first layer, use absolute path)

    Returns:
        Success/failure message

    Examples:
        - Upload config: local_path="./config.json",
          remote_path="/home/vmrobot/config.json"
        - Upload script: local_path="./deploy.sh", remote_path="/home/vmrobot/deploy.sh"
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        local_file = pathlib.Path(local_path)
        if not local_file.exists():
            _log_error(ctx, "ssh_upload", f"Local file not found: {local_path}")
            return f"Error: Local file not found: {local_path}"

        async with ssh.start_sftp_client() as sftp:
            await sftp.put(str(local_file), remote_path)

        _log_tool_call(
            ctx,
            "ssh_upload",
            {"local_path": local_path, "remote_path": remote_path},
            "success",
        )
        return f"Successfully uploaded {local_path} to {remote_path}"
    except Exception as e:
        _log_error(ctx, "ssh_upload", str(e))
        return f"Error uploading file: {str(e)}"


@mcp.tool()
async def ssh_download(
    remote_path: str,
    local_path: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Download a file from the VM to the host via SFTP.

    ⚠️ Note: This downloads from the first VM layer only. For nested environments,
    files must be transferred from inner layers using alternative methods.

    Notes:
    - Uses SFTP over the persistent SSH connection
    - Automatically creates parent directories on host if needed
    - Use absolute paths for reliability
    - Supports all file types (text, binary, archives, logs)

    Best Practices:
    - Verify remote file exists first:
      ssh_execute("test -f /path/to/file && echo exists")
    - Use absolute paths on both sides
    - For logs, download before they rotate
    - Consider compressing large files first on VM

    Args:
        remote_path: Path to the file on the VM (first layer, use absolute path)
        local_path: Destination path on the host (parent dirs created automatically)

    Returns:
        Success/failure message

    Examples:
        - Download logs: remote_path="/var/log/app.log", local_path="./logs/app.log"
        - Download backup:
          remote_path="/home/vmrobot/backup.tar.gz",
          local_path="./backups/backup.tar.gz"
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        local_file = pathlib.Path(local_path)
        # Create parent directories if needed
        local_file.parent.mkdir(parents=True, exist_ok=True)

        async with ssh.start_sftp_client() as sftp:
            await sftp.get(remote_path, str(local_file))

        _log_tool_call(
            ctx,
            "ssh_download",
            {"remote_path": remote_path, "local_path": local_path},
            "success",
        )
        return f"Successfully downloaded {remote_path} to {local_path}"
    except Exception as e:
        _log_error(ctx, "ssh_download", str(e))
        return f"Error downloading file: {str(e)}"


@mcp.tool()
async def ssh_connection_info(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Get information about the current SSH connection to the VM.

    Use this to:
    - Verify SSH connection is active
    - Check connection parameters (host, port, user)
    - Troubleshoot connection issues
    - Confirm which VM you're connected to

    The connection is persistent across all SSH tool calls, so no reconnect
    overhead between operations.

    Returns:
        Connection details including host, port, user, display, and connection status
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        # Try to execute a simple command to verify connection is alive
        await ssh.run("echo 'connection_test'", check=True, timeout=5)
        status = "Connected"
    except Exception as e:
        status = f"Connection issue: {str(e)}"
        _log_error(ctx, "ssh_connection_info", str(e))

    _log_tool_call(ctx, "ssh_connection_info", {}, status)

    cal = ctx.request_context.lifespan_context.calibration  # type: ignore[union-attr]
    cal_line = (
        f"Display Calibration: xdotool={cal.xdotool_w}x{cal.xdotool_h}, "
        f"screenshot={cal.screenshot_w}x{cal.screenshot_h}, "
        f"scale=({cal.scale_x:.4f}, {cal.scale_y:.4f})"
    )

    info = f"""SSH Connection Information:
Host: {VM_HOST}
Port: {VM_PORT}
User: {VM_USER}
Display: {VM_DISPLAY}
Status: {status}
Identity File: {VM_IDENTITY or "Not specified (using password/agent)"}
{cal_line}"""

    return info


@mcp.tool()
async def display_calibration_info(
    recalibrate: bool = False,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Show current display calibration data (scale factors between xdotool
    and screenshot coordinate spaces).

    Set recalibrate=True to re-probe the display (useful after xrandr
    resolution changes mid-session).

    Returns:
        Calibration details including xdotool geometry, screenshot
        dimensions, and computed scale factors.
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]

    if recalibrate:
        app_ctx.calibration = await _calibrate_display(app_ctx.ssh)

    cal = app_ctx.calibration
    _log_tool_call(ctx, "display_calibration_info", {"recalibrate": recalibrate})

    no_scale = cal.scale_x == 1.0 and cal.scale_y == 1.0
    scaling = "1:1 (no scaling)" if no_scale else "active"
    lines = [
        f"Display Calibration ({scaling}):",
        f"  xdotool geometry: {cal.xdotool_w}x{cal.xdotool_h}",
        f"  Screenshot pixels: {cal.screenshot_w}x{cal.screenshot_h}",
        f"  Scale X: {cal.scale_x:.4f}",
        f"  Scale Y: {cal.scale_y:.4f}",
    ]
    if recalibrate:
        lines.append("  (recalibrated)")
    return "\n".join(lines)


# ---------- Project Tools ----------


def _get_project(ctx: Context[ServerSession, AppContext]) -> Project:
    """Get the current project or raise an error if none is active."""
    project = ctx.request_context.lifespan_context.project  # type: ignore[union-attr]
    if project is None:
        raise ValueError("No project initialized. Call project_init first.")
    return project


def _get_project_optional(
    ctx: Context[ServerSession, AppContext] | None,
) -> Project | None:
    """Get the current project if one exists, otherwise None."""
    if ctx is None:
        return None
    return ctx.request_context.lifespan_context.project  # type: ignore[union-attr]


def _log_tool_call(
    ctx: Context[ServerSession, AppContext] | None,
    tool_name: str,
    params: dict,
    result: str | None = None,
) -> None:
    """Log a tool call to the project log if a project is active."""
    project = _get_project_optional(ctx)
    if project is None:
        return

    # Format parameters, truncating long values
    param_strs = []
    for k, v in params.items():
        v_str = str(v)
        if len(v_str) > 100:
            v_str = v_str[:100] + "..."
        param_strs.append(f"{k}={v_str}")
    params_str = ", ".join(param_strs) if param_strs else ""

    log_msg = f"TOOL: {tool_name}({params_str})"
    if result:
        result_str = result if len(result) <= 200 else result[:200] + "..."
        log_msg += f" -> {result_str}"

    project._log(log_msg)


def _log_error(
    ctx: Context[ServerSession, AppContext] | None, tool_name: str, error: str
) -> None:
    """Log an error to the project log if a project is active."""
    project = _get_project_optional(ctx)
    if project is None:
        return
    project._log(f"ERROR in {tool_name}: {error}", level="ERROR")


@mcp.tool()
async def project_init(
    name: str,
    description: str = "",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Initialize a new project. MUST be called before screenshots and other operations.

    Projects organize all outputs into a timestamped folder with:
    - screenshots/ - All screenshots from take_screenshot()
    - logs/ - Automatic logging of all tool calls
    - results/ - Saved outputs via project_save_result()
    - advice/ - Tips saved via project_save_advice()

    Recommended workflow:
    1. project_init("task-name", "description") - Start here
    2. take_screenshot() - Now available
    3. ... do work ... (all actions auto-logged)
    4. project_save_advice() - Save lessons learned
    5. project_save_result() - Save important outputs

    For continuing work: project_list() → project_load("path")

    Args:
        name: Project name (used in folder name)
        description: Optional project description

    Returns:
        Project information including path
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]

    project = Project.create(name, description)
    app_ctx.project = project

    info = project.get_info()
    return f"""Project initialized:
Name: {info["name"]}
Path: {info["path"]}
Description: {info["description"] or "(none)"}

Folders created:
- screenshots/
- logs/
- results/"""


@mcp.tool()
async def project_info(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Get information about the current project.

    Returns:
        Project details and statistics
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    info = project.get_info()

    return f"""Project Information:
Name: {info["name"]}
Path: {info["path"]}
Created: {info["created_at"]}
Description: {info["description"] or "(none)"}

Statistics:
- Screenshots: {info["screenshot_count"]}
- Results: {info["result_count"]}
- Log entries: {info["log_entries"]}"""


@mcp.tool()
async def project_log(
    message: str,
    level: str = "INFO",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Add a log entry to the current project.

    Args:
        message: Log message
        level: Log level (INFO, WARNING, ERROR, DEBUG)

    Returns:
        Confirmation message
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    return project.log(message, level)


@mcp.tool()
async def project_read_logs(
    lines: int = 50,
    level_filter: str = "",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Read the project log file to see what happened.

    All tool calls are automatically logged with:
    - Tool name and parameters
    - Success/failure results
    - Errors with level=ERROR

    This is useful for debugging issues or reviewing what actions were taken.
    Use level_filter="ERROR" to see only errors.

    Args:
        lines: Number of recent log lines to return (default 50)
        level_filter: Optional filter by level (INFO, WARNING, ERROR, DEBUG)

    Returns:
        Recent log entries
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    log_file = project.path / "logs" / "project.log"

    if not log_file.exists():
        return "No log entries yet."

    all_lines = log_file.read_text().strip().split("\n")

    if level_filter:
        all_lines = [line for line in all_lines if f"[{level_filter.upper()}]" in line]

    recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

    if not recent_lines:
        suffix = f" with level {level_filter}" if level_filter else ""
        return f"No log entries found{suffix}."

    return (
        f"Log entries ({len(recent_lines)} of {len(all_lines)} total):\n\n"
        + "\n".join(recent_lines)
    )


@mcp.tool()
async def project_save_result(
    filename: str,
    content: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Save a result file to the current project's results folder.

    Args:
        filename: Name for the result file
        content: Content to save

    Returns:
        Path to saved file
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    result_path = project.save_result(filename, content)
    return f"Result saved to: {result_path}"


@mcp.tool()
async def project_save_advice(
    title: str,
    content: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Save advice/tips for future LLM sessions working with this project.

    Use this to record lessons learned, environment-specific tips, or
    important information that would help future interactions.

    Args:
        title: Short title for the advice (e.g., "Focus management in Citrix")
        content: Detailed advice content (markdown supported)

    Returns:
        Confirmation with path to saved advice file
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    advice_path = project.save_advice(title, content)
    return f"Advice saved: {title}\nPath: {advice_path}"


@mcp.tool()
async def project_read_advice(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Read all advice/tips saved for this project.

    Returns advice from previous sessions that may help with current tasks.

    Returns:
        All saved advice entries formatted for reading
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    advice_list = project.get_all_advice()

    if not advice_list:
        return "No advice saved for this project yet."

    output = f"## Advice for project '{project.name}' ({len(advice_list)} entries)\n\n"
    for i, advice in enumerate(advice_list, 1):
        output += f"### {i}. {advice['title']}\n"
        output += f"{advice['content']}\n\n"
        output += f"_Source: {advice['file']}_\n\n---\n\n"

    return output


@mcp.tool()
async def project_list(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    List all existing projects.

    Returns:
        List of projects with their paths and creation dates
    """
    projects = []
    for project_dir in sorted(PROJECTS_DIR.iterdir(), reverse=True):
        if project_dir.is_dir() and (project_dir / "metadata.json").exists():
            try:
                proj = Project.load(project_dir)
                projects.append(f"- {proj.name} ({proj.created_at}): {proj.path}")
            except Exception:
                projects.append(f"- (invalid): {project_dir}")

    if not projects:
        return "No projects found."

    return "Projects:\n" + "\n".join(projects)


@mcp.tool()
async def project_load(
    project_path: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Load an existing project by its path.

    When loading, any saved advice is automatically displayed. READ THIS ADVICE
    BEFORE PROCEEDING - it contains lessons learned from previous sessions that
    will help you avoid common mistakes and work more efficiently.

    Use project_list() to see all available projects.

    Args:
        project_path: Full path to the project folder

    Returns:
        Project information including any saved advice
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]

    path = pathlib.Path(project_path)
    if not path.exists():
        return f"Error: Project path not found: {project_path}"

    try:
        project = Project.load(path)
        app_ctx.project = project
        project._log("Project loaded")

        info = project.get_info()
        output = f"""Project loaded:
Name: {info["name"]}
Path: {info["path"]}
Created: {info["created_at"]}
Screenshots: {info["screenshot_count"]}
Results: {info["result_count"]}"""

        # Include advice if any exists
        advice_list = project.get_all_advice()
        if advice_list:
            output += f"\n\n## ⚠️ ADVICE FOR THIS PROJECT ({len(advice_list)} tips)\n"
            output += "Read these tips from previous sessions before proceeding:\n\n"
            for i, advice in enumerate(advice_list, 1):
                output += f"**{i}. {advice['title']}**\n"
                # Show first 200 chars of content
                content_preview = advice["content"][:200]
                if len(advice["content"]) > 200:
                    content_preview += "..."
                output += f"{content_preview}\n\n"

        return output
    except Exception as e:
        return f"Error loading project: {str(e)}"


# ---------- Screenshot tools + resources ----------


@mcp.tool()
async def take_screenshot(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Take a full-screen screenshot and save it to the current project.
    Requires an active project (call project_init first).

    ⚠️ CRITICAL BEST PRACTICE: ALWAYS screenshot BEFORE actions!

    Workflow:
    1. take_screenshot() - see current state
    2. Analyze the image - identify focus, window state
    3. Perform actions - with proper waits
    4. take_screenshot() - verify result

    Never skip screenshots to "save time" - blind actions lead to errors that
    waste more time. Screenshots help identify:
    - Which window/application has focus
    - Current Vim mode (if in editor)
    - Whether dialogs are open
    - If commands completed successfully

    Returns:
        Screenshot path and resource URI for viewing
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    project = _get_project(ctx)  # type: ignore[arg-type]
    ssh = app_ctx.ssh

    sid = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
    remote_path = f"/tmp/mcp-screenshot-{sid}.png"
    display = shlex.quote(VM_DISPLAY)
    cmd = f"DISPLAY={display} scrot {shlex.quote(remote_path)}"
    await run_vm_cmd(ssh, cmd)

    # download via SFTP to project folder
    local_path = project.screenshot_path(sid)
    async with ssh.start_sftp_client() as sftp:
        await sftp.get(remote_path, str(local_path))

    project._log(f"Screenshot captured: {sid}")
    resource_uri = f"vm://screenshot/{sid}"
    return f"Screenshot captured: {local_path}\nResource URI: {resource_uri}"


# Expose screenshots as resources
@mcp.resource("vm://screenshot/{sid}")
async def get_screenshot(sid: str) -> bytes:
    """
    Return a screenshot by ID as binary data.
    Searches in all project folders.
    """
    # Search in all projects for this screenshot
    for project_dir in PROJECTS_DIR.iterdir():
        if project_dir.is_dir():
            screenshot_path = project_dir / "screenshots" / f"{sid}.png"
            if screenshot_path.exists():
                return screenshot_path.read_bytes()

    raise FileNotFoundError(f"No screenshot found for id {sid}")


# ---------- Entrypoint ----------

if __name__ == "__main__":
    # stdio transport; works with most MCP clients
    mcp.run()
