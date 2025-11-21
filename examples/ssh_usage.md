# SSH Tools Usage Examples

This document demonstrates how to use the SSH tools added to the MCP QEMU VM Control server.

## Available SSH Tools

1. **ssh_execute** - Execute shell commands on the VM
2. **ssh_upload** - Upload files from host to VM
3. **ssh_download** - Download files from VM to host
4. **ssh_connection_info** - Get SSH connection details

## Example Use Cases

### 1. System Administration

Check VM system information:
```
ssh_execute("uname -a")
```

Check disk usage:
```
ssh_execute("df -h")
```

Check running processes:
```
ssh_execute("ps aux | head -20")
```

Update package database:
```
ssh_execute("sudo pacman -Sy")
```

### 2. File Management

List files in a directory:
```
ssh_execute("ls -lah /home/vmrobot")
```

Create a directory:
```
ssh_execute("mkdir -p /home/vmrobot/workspace")
```

Check file contents:
```
ssh_execute("cat /etc/hostname")
```

### 3. File Transfer

Upload a configuration file:
```
ssh_upload(
    local_path="./config/app.conf",
    remote_path="/home/vmrobot/app.conf"
)
```

Upload a script and make it executable:
```
ssh_upload(
    local_path="./scripts/deploy.sh",
    remote_path="/home/vmrobot/deploy.sh"
)
ssh_execute("chmod +x /home/vmrobot/deploy.sh")
```

Download logs:
```
ssh_download(
    remote_path="/var/log/syslog",
    local_path="./logs/vm-syslog.log"
)
```

Download backup files:
```
ssh_download(
    remote_path="/home/vmrobot/backup.tar.gz",
    local_path="./backups/vm-backup.tar.gz"
)
```

### 4. Development Workflows

Deploy and run a Python script:
```
# Upload the script
ssh_upload(
    local_path="./app.py",
    remote_path="/home/vmrobot/app.py"
)

# Install dependencies
ssh_execute("pip install --user flask requests")

# Run the script
ssh_execute("python /home/vmrobot/app.py")
```

Build and test a project:
```
# Upload project files
ssh_upload(
    local_path="./project.zip",
    remote_path="/home/vmrobot/project.zip"
)

# Extract and build
ssh_execute("cd /home/vmrobot && unzip project.zip")
ssh_execute("cd /home/vmrobot/project && make build")
ssh_execute("cd /home/vmrobot/project && make test")

# Download build artifacts
ssh_download(
    remote_path="/home/vmrobot/project/build/output.bin",
    local_path="./artifacts/output.bin"
)
```

### 5. Monitoring and Debugging

Check connection status:
```
ssh_connection_info()
```

Monitor system resources:
```
ssh_execute("top -bn1 | head -20")
```

Check network connectivity:
```
ssh_execute("ping -c 4 8.8.8.8")
```

View system logs:
```
ssh_execute("journalctl -n 50")
```

### 6. Combined Automation Workflow

Example: Deploy a web application and verify it's running

```python
# 1. Upload application files
ssh_upload("./webapp.tar.gz", "/home/vmrobot/webapp.tar.gz")

# 2. Extract and setup
ssh_execute("cd /home/vmrobot && tar -xzf webapp.tar.gz")
ssh_execute("cd /home/vmrobot/webapp && npm install")

# 3. Start the application
ssh_execute("cd /home/vmrobot/webapp && npm start &")

# 4. Wait for startup
wait(3)

# 5. Verify it's running
ssh_execute("curl http://localhost:3000/health")

# 6. Take a screenshot of the running app
move_mouse(500, 300)
click()
take_screenshot()

# 7. Download logs for analysis
ssh_download(
    "/home/vmrobot/webapp/logs/app.log",
    "./analysis/app.log"
)
```

## Error Handling

All SSH tools return descriptive error messages if operations fail:

- File not found: `"Error: Local file not found: /path/to/file"`
- Permission denied: Returns stderr output with permission error
- Connection issues: Handled by the connection manager
- Command failures: Returns both stdout/stderr and exit code

## Best Practices

1. **Use absolute paths** when possible for file operations
2. **Check connection status** with `ssh_connection_info()` if experiencing issues
3. **Handle long-running commands** by combining with `wait()` tool
4. **Test commands locally** before executing on production VMs
5. **Use sudo carefully** - ensure passwordless sudo is configured if needed
6. **Create backups** before modifying critical files
7. **Monitor output** by checking stdout/stderr in responses

## Security Considerations

- Commands run with the permissions of the SSH user (vmrobot)
- Use SSH keys instead of passwords for better security
- Avoid hardcoding sensitive information in commands
- Review commands before execution, especially with sudo
- Limit the VM user's permissions appropriately
- Use file upload for complex scripts instead of inline commands

## Troubleshooting

### Command doesn't produce output
Some commands may not produce output to stdout. Check the return value and use:
```
ssh_execute("ls -la /home/vmrobot || echo 'Command failed'")
```

### Permission denied
Ensure the VM user has appropriate permissions:
```
ssh_execute("sudo chown vmrobot:vmrobot /path/to/file")
```

### File transfer fails
Verify paths exist and are accessible:
```
ssh_execute("mkdir -p /home/vmrobot/uploads")
ssh_upload("./file.txt", "/home/vmrobot/uploads/file.txt")
```

### Connection lost
Check connection status and reconnect if needed:
```
ssh_connection_info()
