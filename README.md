# MCP QEMU VM Control

> **Give your AI full computer access — safely.**
>
> Let Claude (or any MCP-compatible LLM) see your screen, move the mouse, type on the keyboard, and run commands — all inside an isolated QEMU virtual machine. Perfect for AI-driven automation, testing, and computer-use experiments without risking your host system.

A Model Context Protocol (MCP) server for controlling QEMU virtual machines via SSH. This server enables LLMs to interact with VMs through mouse/keyboard control, screenshots, and SSH command execution.

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [QEMU/libvirt Setup](#qemulibvirt-setup)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Tools Reference](#tools-reference)
- [Typical Workflow](#typical-workflow)
- [Best Practices for LLM Automation](#best-practices-for-llm-automation)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)

## Features

- **Mouse Control** - Move cursor and click buttons
- **Keyboard Input** - Type text and send key combinations
- **Action Batching** - Execute sequences of UI actions in one call
- **Screenshots** - Capture and retrieve VM screenshots
- **SSH Command Execution** - Run shell commands on the VM
- **File Transfer** - Upload and download files via SFTP
- **Project Management** - Organize outputs into project folders with logs, results, and advice
- **Advice System** - Save and retrieve tips for future LLM sessions

## Prerequisites

### Host System
- Python 3.12+
- `uv` (recommended) or `pip`
- QEMU/KVM with libvirt
- virt-manager (optional, for GUI management)

### VM Requirements
- Linux with X11 desktop environment
- SSH server enabled
- Required packages: `openssh`, `xdotool`, `scrot`, `xrandr`, `xinput`

## QEMU/libvirt Setup

### 1. Install virtualization packages

**Arch/Manjaro:**
```bash
sudo pacman -S qemu-full libvirt virt-manager dnsmasq iptables-nft
```

**Debian/Ubuntu:**
```bash
sudo apt install qemu-kvm libvirt-daemon-system libvirt-clients virt-manager bridge-utils
```

**Fedora:**
```bash
sudo dnf install @virtualization
```

### 2. Configure libvirt

```bash
# Enable and start libvirtd
sudo systemctl enable --now libvirtd

# Add your user to libvirt group
sudo usermod -aG libvirt $USER

# Log out and back in, then verify
groups  # should show 'libvirt'
```

### 3. Set up the default network

libvirt provides a default NAT network (`192.168.122.0/24`) that VMs use to communicate with the host:

```bash
# Check network status
virsh -c qemu:///system net-list --all

# If 'default' is not active, start it
virsh -c qemu:///system net-start default

# Enable autostart
virsh -c qemu:///system net-autostart default
```

The default network configuration:
- Bridge: `virbr0`
- Host IP: `192.168.122.1`
- DHCP range: `192.168.122.2` - `192.168.122.254`
- Mode: NAT (VMs can access internet, host can access VMs)

### 4. Create a VM with virt-manager

1. Launch virt-manager
2. Create a new VM (File → New Virtual Machine)
3. Select installation media (ISO)
4. Allocate resources:
   - Memory: 4096 MB recommended
   - CPUs: 2+ recommended
5. **Important**: Under "Network selection", choose "Virtual network 'default': NAT"
6. Complete installation

### 5. Configure the VM

After installing the guest OS:

```bash
# Inside the VM - Install required packages

# Arch/Manjaro
sudo pacman -S --needed openssh xdotool scrot xorg-xrandr xorg-xinput

# Debian/Ubuntu
sudo apt install openssh-server xdotool scrot x11-xserver-utils xinput

# Enable SSH
sudo systemctl enable --now sshd
```

### 6. Create the automation user

On the VM:
```bash
# Create vmrobot user
sudo useradd -m -s /bin/bash vmrobot
sudo passwd vmrobot

# Set up SSH key authentication
sudo -u vmrobot mkdir -p /home/vmrobot/.ssh
sudo -u vmrobot chmod 700 /home/vmrobot/.ssh
```

On the host:
```bash
# Copy your public key to the VM
ssh-copy-id vmrobot@192.168.122.XX

# Or manually add to /home/vmrobot/.ssh/authorized_keys on VM
```

### 7. Grant X11 access to vmrobot

The vmrobot user needs permission to access the X display. On the VM, as the user who owns the desktop session:

```bash
# Quick fix (run once per session)
xhost +local:vmrobot

# Permanent fix - add to ~/.xprofile or ~/.xinitrc
echo "xhost +local:" >> ~/.xprofile
```

### 8. Find your VM's IP address

```bash
# From the host
virsh -c qemu:///system domifaddr manjaro

# Or from inside the VM
ip addr show | grep "inet 192.168.122"
```

### 9. Test the connection

```bash
# Test SSH
ssh vmrobot@192.168.122.XX

# Test X11 automation
ssh vmrobot@192.168.122.XX 'DISPLAY=:0 xdotool getmouselocation'

# Test screenshot
ssh vmrobot@192.168.122.XX 'DISPLAY=:0 scrot /tmp/test.png && echo Success'
```

## Installation

### 1. Clone the repository
```bash
git clone https://github.com/Neanderthal/mcp-qemu-vm.git
cd mcp-qemu-vm
```

### 2. Install dependencies

Using `uv` (recommended):
```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
```

Using `pip`:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Set environment variables or create a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `VM_HOST` | `192.168.122.79` | VM IP address |
| `VM_USER` | `vmrobot` | SSH username |
| `VM_PORT` | `22` | SSH port |
| `VM_DISPLAY` | `:0` | X11 display |
| `VM_IDENTITY` | (empty) | SSH private key path (optional) |

Example `.env` file:
```bash
VM_HOST=192.168.122.79
VM_USER=vmrobot
VM_PORT=22
VM_DISPLAY=:0
```

## Usage

### MCP Client Configuration

Add to your MCP client config (e.g., Claude Desktop `claude_desktop_config.json`):

```json
{
  "qemu-vm-control": {
    "command": "python3",
    "args": ["/path/to/mcp-qemu-vm/server.py"],
    "env": {
      "VM_HOST": "192.168.122.79",
      "VM_USER": "vmrobot",
      "VM_PORT": "22",
      "VM_DISPLAY": ":0"
    }
  }
}
```

**Config file locations:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

### Development with MCP Inspector

```bash
uv run mcp dev server.py

# With custom environment
VM_HOST=192.168.122.79 VM_USER=vmrobot uv run mcp dev server.py
```

### Running Standalone

```bash
python server.py
```

## Tools Reference

### Project Management

Projects organize all outputs (screenshots, logs, results, advice) into timestamped folders under `data/projects/`.

| Tool | Description |
|------|-------------|
| `project_init(name, description)` | Create a new project (required before screenshots) |
| `project_load(project_path)` | Load an existing project |
| `project_list()` | List all projects |
| `project_info()` | Get current project statistics |
| `project_log(message, level)` | Add a log entry |
| `project_read_logs(lines, level_filter)` | Read project logs |
| `project_save_result(filename, content)` | Save a result file |
| `project_save_advice(title, content)` | Save tips for future sessions |
| `project_read_advice()` | Read all saved advice |

### Mouse & Keyboard

| Tool | Description |
|------|-------------|
| `move_mouse(x, y, mode)` | Move cursor (mode: "absolute" or "relative") |
| `click(button, count)` | Click mouse button (left/middle/right) |
| `type_text(text)` | Type text |
| `press_keys(keys)` | Press key combo, e.g., `["Ctrl", "L"]` |
| `wait(seconds)` | Pause execution |
| `run_actions(actions)` | Execute a sequence of actions in one call |

#### Batch Actions Example

```json
[
  {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]},
  {"action": "wait", "seconds": 0.5},
  {"action": "type_text", "text": "Terminal: Focus Terminal"},
  {"action": "press_keys", "keys": ["Return"]}
]
```

### SSH Operations

| Tool | Description |
|------|-------------|
| `ssh_execute(command)` | Run a shell command on the VM |
| `ssh_upload(local_path, remote_path)` | Upload file to VM |
| `ssh_download(remote_path, local_path)` | Download file from VM |
| `ssh_connection_info()` | Get connection status |

### Screenshots

| Tool | Description |
|------|-------------|
| `take_screenshot()` | Capture screenshot (requires active project) |

Screenshots are saved to the project's `screenshots/` folder and exposed as MCP resources at `vm://screenshot/{id}`.

## Typical Workflow

```
1. project_init("my-task", "Description")
2. take_screenshot()
3. ... perform VM operations ...
4. project_read_logs()
5. project_save_result("output.txt", data)
6. project_save_advice("Title", "Lessons learned...")
```

For continuing work:
```
1. project_list()
2. project_load("data/projects/...")  # Shows any saved advice
3. ... continue work ...
```

## Best Practices for LLM Automation

These lessons were learned from real-world usage and help avoid common pitfalls.

### 1. Always Screenshot Before Actions

Before ANY interaction:
1. `take_screenshot()`
2. Analyze the image
3. Identify current focus (which window/field is active)
4. Only then proceed with actions

**Never skip screenshots to "save time"** - blind actions lead to errors.

### 2. Don't Trust Mouse Clicks for Focus

Clicking on a window/terminal does NOT reliably switch focus, especially in:
- Nested environments (Citrix, remote desktop)
- High-latency connections
- Applications with multiple panels (VS Code, IDEs)

**Use keyboard shortcuts instead:**
```json
[
  {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]},
  {"action": "wait", "seconds": 0.5},
  {"action": "type_text", "text": "Terminal: Focus Terminal"},
  {"action": "wait", "seconds": 0.3},
  {"action": "press_keys", "keys": ["Return"]},
  {"action": "wait", "seconds": 0.5}
]
```
Then `take_screenshot()` to verify before typing.

### 3. Required Wait Times

| After This Action | Wait Time |
|-------------------|-----------|
| Opening Command Palette | 0.5s |
| Typing search text | 0.3s |
| Pressing Enter/Return | 0.5-1.0s |
| Command execution | 1.0-2.0s |
| Window/focus switch | 0.5s |

**Never rapid-fire actions** - they may arrive out of order.

### 4. Use Batch Actions

Use `run_actions()` instead of separate tool calls to reduce latency and ensure ordering:

```python
# Instead of 5 separate calls:
run_actions([
    {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]},
    {"action": "wait", "seconds": 0.5},
    {"action": "type_text", "text": "command"},
    {"action": "wait", "seconds": 0.3},
    {"action": "press_keys", "keys": ["Return"]}
])
```

### 5. SSH Scope Limitation

`ssh_execute` only reaches the **first VM layer**. For nested environments (VM → Citrix → Windows), use UI automation to type commands in the visible terminal.

### 6. Recovery Commands

| Problem | Solution |
|---------|----------|
| Typed in wrong window (few chars) | `Escape` → `u` (undo in Vim) |
| Multiple lines in wrong place | `Escape` → `uuuuuuu` |
| File corrupted | `Escape` → `:e!` → `Enter` (reload) |
| VS Code revert | `Ctrl+Shift+P` → "Revert File" |

### 7. Common Mistakes to Avoid

1. Typing immediately after clicking terminal (focus may not have switched)
2. Skipping screenshots to "save time"
3. Using `ssh_execute` for nested environment commands
4. Not waiting between actions
5. Assuming focus switched without verification

## Architecture

```
┌─────────────┐         SSH          ┌──────────────┐
│             │ ◄──────────────────► │              │
│  MCP Server │                      │   QEMU VM    │
│   (Host)    │                      │   (Linux)    │
│             │                      │              │
└──────┬──────┘                      └──────────────┘
       │                                    │
       │ MCP Protocol                       │
       │ (stdio)                            │
       │                                    │
       ▼                                    ▼
┌─────────────┐                      xdotool, scrot
│  LLM Client │                      X11 automation
│  (Claude)   │
└─────────────┘
```

**Network topology:**
```
┌────────────────────────────────────────────────────┐
│  Host (192.168.122.1)                              │
│  ┌──────────┐                                      │
│  │ virbr0   │◄── NAT bridge                        │
│  └────┬─────┘                                      │
│       │                                            │
│  ┌────┴─────┐                                      │
│  │ QEMU VM  │ 192.168.122.79                       │
│  │ (manjaro)│                                      │
│  └──────────┘                                      │
└────────────────────────────────────────────────────┘
```

### Project Structure
```
mcp-qemu-vm/
├── server.py           # Main MCP server (single file)
├── requirements.txt    # Python dependencies
├── data/
│   └── projects/       # Project folders
│       └── YYYYMMDD-HHMMSS_name/
│           ├── screenshots/
│           ├── logs/
│           ├── results/
│           └── advice/
└── README.md
```

## Troubleshooting

### Cannot connect to VM

1. **Check VM is running:**
   ```bash
   virsh -c qemu:///system list
   ```

2. **Check network is active:**
   ```bash
   virsh -c qemu:///system net-list
   # If default is inactive:
   virsh -c qemu:///system net-start default
   ```

3. **Check VM has IP:**
   ```bash
   virsh -c qemu:///system domifaddr <vm-name>
   ```

4. **Test SSH connectivity:**
   ```bash
   ssh vmrobot@192.168.122.XX
   ```

### Mouse/keyboard not working

- Verify `xdotool` is installed on VM: `which xdotool`
- Check X11 display: `echo $DISPLAY` (should be `:0`)
- Test manually: `DISPLAY=:0 xdotool getmouselocation`

### Screenshots failing / X11 Authorization Error

If you see `Authorization required, but no authorization protocol specified`:

**Quick fix** (run as X session owner on VM):
```bash
xhost +local:vmrobot
```

**Permanent fix** - Add to `~/.xprofile`:
```bash
xhost +local:
```

**Verify access:**
```bash
# Check current xhost settings
DISPLAY=:0 xhost

# Should show:
# access control enabled, only authorized clients can connect
# LOCAL:
```

### VM network issues

```bash
# Restart the default network
virsh -c qemu:///system net-destroy default
virsh -c qemu:///system net-start default

# Check virbr0 bridge exists
ip addr show virbr0
```

## License

MIT

## Related

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [FastMCP Documentation](https://github.com/modelcontextprotocol/python-sdk)
- [libvirt Documentation](https://libvirt.org/docs.html)
- [virt-manager](https://virt-manager.org/)
