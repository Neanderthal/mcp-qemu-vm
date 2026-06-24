# Security Rules

## Shell Injection Prevention

All xdotool/scrot commands run over SSH. User-supplied values must never be interpolated directly into shell commands.

### type_text()
Uses `xdotool type --file -` with text piped to stdin. Text never touches the shell. Do NOT revert to shell escaping. The `human=True` cadence mode types word-by-word with a per-word `--delay <int>`; the delay is always `max(0, int(...))` (never user text), so nothing user-controlled is interpolated.

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

## Path Traversal Prevention

User/LLM-supplied filenames are never joined to a directory as-is.

- `project_save_result()` reduces the filename to its basename
  (`pathlib.Path(name).name`) and rejects empty / `.` / `..` before writing.
- The `vm://screenshot/{sid}` resource validates `sid` against `[0-9-]+`
  (the generated timestamp format) before touching the filesystem.
- `save_advice()` strips non-alphanumeric chars from the title for the filename.

## Logging

- `type_text` logs only the length of long text, never the content.
- `ssh_execute` logs the command string (truncated to 100 chars). Avoid passing
  secrets inline on the command line (e.g. `mysql -pPASSWORD`); prefer env files
  or stdin so credentials don't land in `project.log`.

## Environment Variables

- `.env` is gitignored — never commit credentials
- `VM_IDENTITY` (private key path) is optional and sensitive
- `VM_LOCALE` sets the UTF-8 locale forced for `xdotool type` (default `C.UTF-8`)
- See `.env.example` for all supported variables
