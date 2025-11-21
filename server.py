import asyncio
import datetime as dt
import json
import os
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

import asyncssh
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

# ---------- Config ----------
VM_HOST = os.getenv("VM_HOST", "192.168.122.79")
VM_USER = os.getenv("VM_USER", "vmrobot")
VM_PORT = int(os.getenv("VM_PORT", "22"))
VM_DISPLAY = os.getenv("VM_DISPLAY", ":0")
VM_IDENTITY = os.getenv("VM_IDENTITY", "")  # path to private key, optional

PROJECTS_DIR = pathlib.Path("data/projects")
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


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
        timestamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
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
        timestamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
        """Save a result file to the project."""
        result_path = self.path / "results" / filename
        with open(result_path, "w") as f:
            f.write(content)
        self._log(f"Result saved: {filename}")
        return result_path

    def save_advice(self, title: str, content: str) -> pathlib.Path:
        """Save an advice/tip for future LLM sessions."""
        # Create a safe filename from title
        safe_title = "".join(c if c.isalnum() or c in "- _" else "_" for c in title)
        safe_title = safe_title[:50]  # Limit length
        timestamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
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
class AppContext:
    ssh: asyncssh.SSHClientConnection
    project: Optional[Project] = None


async def connect_ssh() -> asyncssh.SSHClientConnection:
    kwargs = dict(
        host=VM_HOST,
        port=VM_PORT,
        username=VM_USER,
        known_hosts=None,
    )
    if VM_IDENTITY:
        kwargs["client_keys"] = [VM_IDENTITY]

    return await asyncssh.connect(**kwargs)


async def run_vm_cmd(ssh: asyncssh.SSHClientConnection, cmd: str) -> str:
    """Run a command inside the VM and return stdout."""
    result = await ssh.run(cmd, check=True)
    return result.stdout.strip()


# ---------- MCP server setup ----------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    ssh = await connect_ssh()
    try:
        yield AppContext(ssh=ssh)
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

    mode: "absolute" or "relative"
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    if mode == "absolute":
        cmd = f"DISPLAY={VM_DISPLAY} xdotool mousemove --sync {x} {y}"
    elif mode == "relative":
        cmd = f"DISPLAY={VM_DISPLAY} xdotool mousemove_relative --sync {x} {y}"
    else:
        raise ValueError("mode must be 'absolute' or 'relative'")

    await run_vm_cmd(ssh, cmd)
    result = f"Mouse moved to ({x}, {y}) [{mode}]"
    _log_tool_call(ctx, "move_mouse", {"x": x, "y": y, "mode": mode})
    return result


@mcp.tool()
async def click(
    button: str = "left",
    count: int = 1,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Click a mouse button.

    button: left/right/middle
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    button_map = {"left": 1, "middle": 2, "right": 3}
    if button not in button_map:
        raise ValueError("button must be left/middle/right")

    cmd = f"DISPLAY={VM_DISPLAY} xdotool click --repeat {count} {button_map[button]}"
    await run_vm_cmd(ssh, cmd)
    result = f"Clicked {button} x{count}"
    _log_tool_call(ctx, "click", {"button": button, "count": count})
    return result


@mcp.tool()
async def type_text(
    text: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """Type literal text into the VM."""
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    # naive escaping; good enough for now
    escaped = text.replace('"', r"\"")
    cmd = f'DISPLAY={VM_DISPLAY} xdotool type --delay 10 "{escaped}"'
    await run_vm_cmd(ssh, cmd)
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
    Press key combo, e.g. ["Ctrl", "L"] or ["Alt", "F4"].
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    # xdotool uses 'ctrl+l', 'alt+F4', etc.
    combo = "+".join(k.lower() for k in keys)
    cmd = f"DISPLAY={VM_DISPLAY} xdotool key {combo}"
    await run_vm_cmd(ssh, cmd)
    result = f"Pressed keys: {keys}"
    _log_tool_call(ctx, "press_keys", {"keys": keys})
    return result


@mcp.tool()
async def wait(
    seconds: float,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """Sleep for a bit."""
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

    Each action is a dict with "action" key and action-specific parameters.

    Supported actions:
    - {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]}
    - {"action": "type_text", "text": "hello"}
    - {"action": "click", "button": "left", "count": 1}
    - {"action": "move_mouse", "x": 100, "y": 200, "mode": "absolute"}
    - {"action": "wait", "seconds": 0.5}

    Example sequence (open VS Code command palette and run command):
    [
        {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]},
        {"action": "wait", "seconds": 0.5},
        {"action": "type_text", "text": "Terminal: Focus Terminal"},
        {"action": "wait", "seconds": 0.3},
        {"action": "press_keys", "keys": ["Return"]}
    ]

    Returns:
        Summary of executed actions
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    results = []

    for i, action_def in enumerate(actions):
        action_type = action_def.get("action")

        try:
            if action_type == "press_keys":
                keys = action_def.get("keys", [])
                combo = "+".join(k.lower() for k in keys)
                cmd = f"DISPLAY={VM_DISPLAY} xdotool key {combo}"
                await run_vm_cmd(ssh, cmd)
                results.append(f"{i + 1}. press_keys {keys}")

            elif action_type == "type_text":
                text = action_def.get("text", "")
                escaped = text.replace('"', r"\"")
                cmd = f'DISPLAY={VM_DISPLAY} xdotool type --delay 10 "{escaped}"'
                await run_vm_cmd(ssh, cmd)
                results.append(f"{i + 1}. type_text ({len(text)} chars)")

            elif action_type == "click":
                button = action_def.get("button", "left")
                count = action_def.get("count", 1)
                button_map = {"left": 1, "middle": 2, "right": 3}
                btn_num = button_map.get(button, 1)
                cmd = f"DISPLAY={VM_DISPLAY} xdotool click --repeat {count} {btn_num}"
                await run_vm_cmd(ssh, cmd)
                results.append(f"{i + 1}. click {button} x{count}")

            elif action_type == "move_mouse":
                x = action_def.get("x", 0)
                y = action_def.get("y", 0)
                mode = action_def.get("mode", "absolute")
                if mode == "absolute":
                    cmd = f"DISPLAY={VM_DISPLAY} xdotool mousemove --sync {x} {y}"
                else:
                    cmd = f"DISPLAY={VM_DISPLAY} xdotool mousemove_relative --sync {x} {y}"
                await run_vm_cmd(ssh, cmd)
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
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Execute an arbitrary shell command on the VM via SSH.

    Args:
        command: The shell command to execute

    Returns:
        Command output (stdout and stderr combined)
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
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

    Args:
        local_path: Path to the local file to upload
        remote_path: Destination path on the VM

    Returns:
        Success/failure message
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

    Args:
        remote_path: Path to the file on the VM
        local_path: Destination path on the host

    Returns:
        Success/failure message
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

    Returns:
        Connection details including host, user, and connection status
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

    info = f"""SSH Connection Information:
Host: {VM_HOST}
Port: {VM_PORT}
User: {VM_USER}
Display: {VM_DISPLAY}
Status: {status}
Identity File: {VM_IDENTITY if VM_IDENTITY else "Not specified (using password/agent)"}"""

    return info


# ---------- Project Tools ----------


def _get_project(ctx: Context[ServerSession, AppContext]) -> Project:
    """Get the current project or raise an error if none is active."""
    project = ctx.request_context.lifespan_context.project  # type: ignore[union-attr]
    if project is None:
        raise ValueError("No project initialized. Call project_init first.")
    return project


def _get_project_optional(
    ctx: Context[ServerSession, AppContext] | None,
) -> Optional[Project]:
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
    Initialize a new project. Must be called before other operations.
    Creates a project folder with screenshots/, logs/, and results/ subdirectories.

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
        return f"No log entries found{' with level ' + level_filter if level_filter else ''}."

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

    Args:
        project_path: Full path to the project folder

    Returns:
        Project information
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

    Returns:
        Screenshot path and resource URI
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    project = _get_project(ctx)  # type: ignore[arg-type]
    ssh = app_ctx.ssh

    sid = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
    remote_path = f"/tmp/mcp-screenshot-{sid}.png"
    cmd = f'DISPLAY={VM_DISPLAY} scrot "{remote_path}"'
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
