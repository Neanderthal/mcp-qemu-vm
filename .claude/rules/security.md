# Security Rules

## Shell Injection Prevention

All xdotool/scrot commands run over SSH. User-supplied values must never be interpolated directly into shell commands.

### type_text()
Uses `xdotool type --file -` with text piped to stdin. Text never touches the shell. Do NOT revert to shell escaping.

### press_keys()
Key names are validated against `VALID_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")` before use. The key combo is `shlex.quote()`d. Rejects `;`, `$`, backticks, pipes.

### move_mouse() / click()
Coordinates and counts are cast to `int()` as defense-in-depth. `VM_DISPLAY` is `shlex.quote()`d.

### run_actions()
Applies the same rules as above for each action type in the batch loop.

### take_screenshot()
Both `VM_DISPLAY` and the remote path are `shlex.quote()`d.

### Display calibration (_calibrate_display)
The calibration probe runs `xdotool getdisplaygeometry`, `scrot`, `file`, and `rm` on the VM — all with `shlex.quote()`d paths. No user input is involved; the only dynamic value is `VM_DISPLAY` (already quoted). The calibration screenshot is written to `/tmp/mcp-calibrate.png` and deleted immediately after reading its dimensions.

## SSH Configuration

- `known_hosts` defaults to `None` for local ephemeral QEMU VMs (host keys change on rebuild). Set `VM_KNOWN_HOSTS` env var to enable verification.
- `connect_timeout` defaults to 10s, configurable via `VM_CONNECT_TIMEOUT`
- Keepalive: `interval=30, count_max=3` prevents idle disconnects

## Environment Variables

- `.env` is gitignored — never commit credentials
- `VM_IDENTITY` (private key path) is optional and sensitive
- See `.env.example` for all supported variables
