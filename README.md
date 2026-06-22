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
- [Known Issues & Limitations](#known-issues--limitations)
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

### 8. Choose SSH user strategy

There are two approaches for the SSH user:

**Option A: Dedicated `vmrobot` user (default)**
- Safer — limited permissions, can't accidentally break desktop config
- Requires `xhost +local:vmrobot` for X11 access (step 7)
- Set `VM_DESKTOP_USER` if you need commands that require the desktop
  user's context (clipboard, password manager, dbus):
  ```bash
  # On the VM, allow vmrobot to run commands as your desktop user
  echo 'vmrobot ALL=(sergey) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/vmrobot-desktop
  sudo chmod 440 /etc/sudoers.d/vmrobot-desktop
  ```
  Then set `VM_DESKTOP_USER=sergey` in your config.
  Use `ssh_execute("xclip -selection clipboard -o", as_desktop_user=True)`.

**Option B: SSH directly as the desktop user**
- Simpler — full desktop access out of the box, no xhost or sudo needed
- Set `VM_USER` to your desktop username (e.g., `sergey`)
- All commands run with full desktop permissions
- Best for personal/development VMs where isolation isn't a concern

### 9. Find your VM's IP address

```bash
# From the host
virsh -c qemu:///system domifaddr manjaro

# Or from inside the VM
ip addr show | grep "inet 192.168.122"
```

### 10. Test the connection

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
| `VM_DESKTOP_USER` | (empty) | Desktop session owner, if different from `VM_USER` |
| `VM_LOCALE` | `C.UTF-8` | UTF-8 locale forced for `xdotool type` (non-ASCII input) |
| `VM_KNOWN_HOSTS` | (none) | SSH known_hosts file path (optional) |
| `VM_CONNECT_TIMEOUT` | `10` | SSH connection timeout in seconds |

See `.env.example` for a documented template.

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
| `ssh_execute(command, as_desktop_user)` | Run a shell command on the VM |
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

### UI Action Dispatch

All xdotool interactions are built from a small set of pure **command builders**
(`_type_cmd`, `_keys_cmd`, `_click_cmd`, `_move_cmd`) so the shell command for an
action is constructed in exactly one place. Each builder takes an already
`shlex.quote()`d display string and returns the command to run on the VM; the
builders also own input validation (key-name pattern, button map, click-count
clamp) and the UTF-8 locale prefix for typing.

Two paths consume these builders:

- **Standalone tools** (`move_mouse`, `click`, `type_text`, `press_keys`, `wait`)
  — individually exposed MCP tools with typed signatures and rich docstrings.
- **`run_actions`** — the batch path. It dispatches through `ACTION_HANDLERS`,
  a `{name: async handler}` registry that is the **single source of truth** for
  which actions a batch supports. Each handler shares the signature
  `async (app_ctx, display, action_dict) -> summary`. Unknown action names raise
  and stop the batch (consistent with its "stops on first error" contract).

```
run_actions(actions)
      │  for each action
      ▼
ACTION_HANDLERS[name]  ──►  _act_*(app, display, action)
                                   │ uses
                                   ▼
                       _type_cmd / _keys_cmd / _click_cmd / _move_cmd
                                   │
                                   ▼
                             run_vm_cmd(ssh, …)  ──►  xdotool over SSH
```

**Adding a new batch action:** write a `_act_<name>(app, display, action)` handler
(reusing or adding a `_*_cmd` builder) and add one entry to `ACTION_HANDLERS`. No
changes to the dispatch loop are needed.

### Project Structure
```
mcp-qemu-vm/
├── server.py           # Main MCP server (single file)
├── pyproject.toml      # Project metadata, ruff & pytest config
├── requirements.txt    # Python dependencies
├── .env.example        # Documented env var template
├── test_ssh_tools.py   # Unit tests (no-VM) + manual SSH smoke check
├── LICENSE             # MIT
├── data/
│   └── projects/       # Project folders
│       └── YYYYMMDD-HHMMSS_name/
│           ├── screenshots/
│           ├── logs/
│           ├── results/
│           └── advice/
└── README.md
```

## Known Issues & Limitations

Issues confirmed in real nested-environment use (host → Citrix → Windows → Outlook).
Each lists the symptom, the root cause, and the current workaround.

**#1 and #2 are fixed in `server.py`.** #3–#6 are inherent limitations of the nested
environment (Citrix/RDP session policy) or the architecture (SSH lands on the first VM
layer only) — they can't be fixed in this server, so the workarounds remain the
recommended approach.

### 1. `type_text` fails on Cyrillic / non-ASCII text — FIXED

- **Symptom:** `type_text` (and any `xdotool type` with non-ASCII) errors out with
  exit status 1. Direct run reveals: `Invalid multi-byte sequence encountered /
  xdo_enter_text_window reported an error`. ASCII text types fine.
- **Root cause:** `xdotool type` decodes multi-byte input using the current locale,
  but the `vmrobot` / desktop-user SSH environment has **no UTF-8 locale**
  (`LANG` empty, keyboard layout bare `us`). Without a UTF-8 `LC_CTYPE`, multi-byte
  UTF-8 (Cyrillic, etc.) cannot be decoded.
- **Fix (applied):** `type_text` and the `run_actions` type step now prefix the
  xdotool invocation with `LC_ALL=$VM_LOCALE` (default `C.UTF-8`), so non-ASCII text
  works out of the box. Override with the `VM_LOCALE` env var if the VM lacks
  `C.UTF-8` (e.g. set `VM_LOCALE=ru_RU.utf8`; check available locales with
  `locale -a`).

### 2. Embedded newlines in typed text become literal glyphs, not Enter — FIXED

- **Symptom:** Typing multi-line text (e.g. `xdotool type` with `\n`, or
  `type --file -`) into a rich editor like Outlook produces one **run-on paragraph**
  with stray box/control-character glyphs where the line breaks should be — paragraph
  breaks are lost.
- **Root cause:** In this nested Citrix → Windows path, the `\n` (LF) is delivered as a
  literal control character to the editor instead of being interpreted as a Return
  keypress.
- **Fix (applied):** `type_text` (and the `run_actions` type step) now split text on
  newlines, type each line via stdin, and send line breaks as explicit `Return` key
  presses instead of a literal LF. `\r\n` and `\r` are normalised first. This works in
  both terminals and rich editors — no caller-side splitting needed.

### 3. Clipboard redirection may be disabled in the guest session

- **Symptom:** Setting the host/X clipboard (`xclip -selection clipboard`) and pasting
  with `Ctrl+V` does **not** transfer text into the Windows/Citrix layer.
- **Root cause:** Clipboard redirection is turned off in the Citrix/RDP session policy,
  so the inner session has its own isolated clipboard.
- **Workaround:** do not rely on copy/paste to inject text across the nesting boundary;
  fall back to typing (see issues #1 and #2).

### 4. Focus is silently stolen after long operations

- **Symptom:** A long type/automation sequence succeeds, but subsequent keystrokes
  (e.g. `BackSpace` to correct text) have **no effect** — verified by a zero pixel-diff
  between before/after screenshots.
- **Root cause:** A desktop/mail notification toast (e.g. new-mail popup) grabs focus
  partway through, so later keys go to the wrong window.
- **Workaround:** re-assert focus by clicking the target window/field *immediately*
  before each keyboard burst, and **verify** the result with a screenshot (crop the
  region and diff) rather than trusting the tool's exit code. Keep keyboard bursts
  short so a focus steal corrupts less.

### 5. Mouse clicks are unreliable for window/focus switching

See [Best Practices §2](#best-practices-for-llm-automation). In nested environments a
click often raises a *different* background window than intended; there is no reliable
`Alt+Tab` (it leaks to the host WM). Prefer the in-app taskbar / window controls and
verify every switch with a screenshot.

### 6. `ssh_execute` only reaches the first VM layer

`ssh_execute` lands on the host/first VM only. Commands do **not** reach inner Citrix /
Windows layers — use UI automation (`type_text`, `press_keys`, `run_actions`) for those.
See [Best Practices §5](#best-practices-for-llm-automation).

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
- Non-ASCII text failing with exit 1 / "Invalid multi-byte sequence"? Missing UTF-8
  locale — see [Known Issues #1](#known-issues--limitations).
- Line breaks not working / run-on text? See [Known Issues #2](#known-issues--limitations).

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

Released under the [MIT License](LICENSE) — © 2026 Sergey Istomin.

## Related

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [FastMCP Documentation](https://github.com/modelcontextprotocol/python-sdk)
- [libvirt Documentation](https://libvirt.org/docs.html)
- [virt-manager](https://virt-manager.org/)
