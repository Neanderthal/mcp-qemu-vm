# LLM Instructions: Working with QEMU VM via MCP

## Environment Overview
- **Host**: Linux system with QEMU MCP server
- **VM**: Manjaro Linux (192.168.122.79)
- **Access**: Via MCP tools (qemu-vm-control server)

## Critical Concepts

### SSH Connection Scope
The MCP server's SSH connection (`ssh_execute`, `ssh_upload`, `ssh_download`) only connects to the **ManjaroVM** (first VM layer).

If you're working with nested environments (e.g., Citrix → Windows → VS Code SSH), SSH tools do NOT reach those inner layers. Use UI automation (`type_text`, `press_keys`, `run_actions`) to interact with nested environments.

### Project-Based Workflow
Always use projects to organize your work:

```
1. project_init("task-name", "Description")  # Start new project
   - OR -
   project_list() → project_load("path")     # Resume existing project

2. project_read_advice()                     # Read tips from previous sessions
3. ... do work ...
4. project_save_advice("Title", "Content")   # Save lessons learned
5. project_save_result("output.txt", data)   # Save important outputs
```

### Advice System
The advice system preserves knowledge across sessions. Use it to save:
- Environment-specific tips (focus management, keyboard shortcuts)
- Application quirks and workarounds
- Common pitfalls and how to avoid them
- Successful workflows for specific tasks

## Available MCP Tools

### Project Management
| Tool | Description |
|------|-------------|
| `project_init(name, description)` | Create new project with folders |
| `project_load(project_path)` | Load existing project (shows saved advice) |
| `project_list()` | List all projects |
| `project_info()` | Get current project stats |
| `project_log(message, level)` | Add log entry |
| `project_read_logs(lines, level_filter)` | Read project logs |
| `project_save_result(filename, content)` | Save result file |
| `project_save_advice(title, content)` | Save tips for future sessions |
| `project_read_advice()` | Read all saved advice |

### UI Automation (Mouse/Keyboard)

| Tool | Parameters | Description |
|------|------------|-------------|
| `move_mouse(x, y, mode)` | x, y: coordinates; mode: "absolute"/"relative" | Move cursor |
| `click(button, count)` | button: "left"/"right"/"middle"; count: clicks | Click mouse |
| `type_text(text)` | text: string to type | Type text (10ms delay between chars) |
| `press_keys(keys)` | keys: list like ["Ctrl", "Shift", "p"] | Press key combination |
| `wait(seconds)` | seconds: float | Sleep/pause |
| `take_screenshot()` | none | Capture screen to project folder |

### Batch Actions (Recommended for Latency-Sensitive Environments)

Use `run_actions(actions)` to execute multiple UI operations in a single MCP call:

```json
[
  {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]},
  {"action": "wait", "seconds": 0.5},
  {"action": "type_text", "text": "Terminal: Focus Terminal"},
  {"action": "wait", "seconds": 0.3},
  {"action": "press_keys", "keys": ["Return"]}
]
```

**Supported actions:**
- `{"action": "press_keys", "keys": ["Ctrl", "L"]}`
- `{"action": "type_text", "text": "hello"}`
- `{"action": "click", "button": "left", "count": 1}`
- `{"action": "move_mouse", "x": 100, "y": 200, "mode": "absolute"}`
- `{"action": "wait", "seconds": 0.5}`

**Benefits:**
- Reduces round-trip latency (5 actions in 1 call vs 5 separate calls)
- Critical for nested environments (Citrix, VMs) with high latency
- Actions execute sequentially, stops on first error

### SSH Operations (ManjaroVM Only)

| Tool | Parameters | Description |
|------|------------|-------------|
| `ssh_execute(command)` | command: shell command | Run command on VM |
| `ssh_upload(local_path, remote_path)` | paths | Upload file to VM |
| `ssh_download(remote_path, local_path)` | paths | Download file from VM |
| `ssh_connection_info()` | none | Check connection status |

**Performance:** SSH is 20-40x faster than UI automation for ManjaroVM tasks.

### Resources
- `vm://screenshot/{sid}` - Access screenshot data by ID
- Screenshots saved to `project/screenshots/{sid}.png`

## Best Practices

### 1. Always Verify Actions
```
take_action → wait(1-2s) → take_screenshot → view result
```

### 2. Use Keyboard Over Mouse When Possible
- More reliable in remote/nested environments
- Doesn't require coordinate hunting
- Faster execution

### 3. Batch Operations for Latency
Instead of 5 separate tool calls:
```python
press_keys(["Ctrl", "Shift", "p"])
wait(0.5)
type_text("command")
wait(0.3)
press_keys(["Return"])
```

Use one `run_actions` call with all 5 actions.

### 4. Save Knowledge to Advice
When you learn something about the environment:
- Focus management quirks
- Working keyboard shortcuts
- Application-specific behaviors
- Successful workflows

Save it with `project_save_advice()` so future sessions benefit.

### 5. Check Advice When Loading Projects
When you `project_load()`, advice is shown automatically. Read it before proceeding.

## Automatic Logging

All tool calls are automatically logged to `project/logs/project.log`:
- Tool name and parameters
- Success/failure results
- Errors with level=ERROR

Use `project_read_logs(lines=50, level_filter="ERROR")` to review what happened.

## Troubleshooting

**Problem**: Actions not working in nested environment
- SSH only reaches ManjaroVM, use UI automation for inner layers
- Use `run_actions` to reduce latency issues

**Problem**: Screenshots not capturing expected content
- Check if correct window has focus
- Add wait time before screenshot

**Problem**: Clicks not registering
- Verify coordinates with screenshot first
- Try keyboard navigation instead
- Check if window needs focus first

**Problem**: Commands failing in remote terminal
- You might be typing in wrong window (editor vs terminal)
- Check project advice for focus management tips
