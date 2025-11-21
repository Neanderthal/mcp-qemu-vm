# SSH Tools Quick Reference

## New SSH Tools Added

This project now includes 4 new SSH tools for direct VM interaction:

### 1. ssh_execute
Execute shell commands on the VM.

**Parameters:**
- `command` (string, required): Shell command to execute

**Returns:** Command output (stdout/stderr/exit code)

**Example:**
```json
{
  "command": "ls -la /home/vmrobot"
}
```

---

### 2. ssh_upload
Upload files from host to VM via SFTP.

**Parameters:**
- `local_path` (string, required): Path to local file
- `remote_path` (string, required): Destination path on VM

**Returns:** Success/failure message

**Example:**
```json
{
  "local_path": "./config.json",
  "remote_path": "/home/vmrobot/config.json"
}
```

---

### 3. ssh_download
Download files from VM to host via SFTP.

**Parameters:**
- `remote_path` (string, required): Path to file on VM
- `local_path` (string, required): Destination path on host

**Returns:** Success/failure message

**Example:**
```json
{
  "remote_path": "/var/log/syslog",
  "local_path": "./logs/vm-syslog.log"
}
```

---

### 4. ssh_connection_info
Get SSH connection details and status.

**Parameters:** None

**Returns:** Connection information

**Example:**
```json
{}
```

## Integration with Existing Tools

These SSH tools work seamlessly with existing VM control tools:

- **Mouse & Keyboard**: `move_mouse`, `click`, `type_text`, `press_keys`
- **Screenshots**: `take_screenshot`
- **Timing**: `wait`

## Common Workflows

### Deploy and test application
1. Upload files: `ssh_upload`
2. Install deps: `ssh_execute`
3. Start app: `ssh_execute`
4. Interact with UI: `move_mouse`, `click`
5. Verify: `take_screenshot`
6. Download logs: `ssh_download`

### System administration
1. Check status: `ssh_connection_info`
2. Run commands: `ssh_execute`
3. Transfer files: `ssh_upload` / `ssh_download`
4. Monitor: `ssh_execute` with monitoring commands

### Development workflow
1. Upload code: `ssh_upload`
2. Build: `ssh_execute`
3. Test: `ssh_execute`
4. Download artifacts: `ssh_download`

## Notes

- All tools use the persistent SSH connection managed by the MCP server
- Commands run with vmrobot user permissions
- SFTP automatically creates parent directories when downloading
- Errors return descriptive messages
- Works with existing MCP configuration

## See Also

- [README.md](README.md) - Full documentation
- [examples/ssh_usage.md](examples/ssh_usage.md) - Detailed examples
- [test_ssh_tools.py](test_ssh_tools.py) - Test suite
