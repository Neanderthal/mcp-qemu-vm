#!/usr/bin/env python3
"""
Simple test script for SSH tools in the MCP QEMU VM server.
"""
import asyncio
import os
from server import connect_ssh, ssh_execute, ssh_connection_info, ssh_upload, ssh_download
from dataclasses import dataclass
from mcp.server.session import ServerSession
from mcp.server.fastmcp import Context


@dataclass
class MockAppContext:
    ssh: any


async def test_connection():
    """Test basic SSH connectivity"""
    print("Testing SSH connection...")
    try:
        ssh = await connect_ssh()
        print("✓ Successfully connected to VM")
        
        # Test basic command
        result = await ssh.run("uname -a", check=True)
        print(f"✓ VM Info: {result.stdout.strip()}")
        
        ssh.close()
        await ssh.wait_closed()
        return True
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return False


async def test_ssh_execute():
    """Test ssh_execute tool"""
    print("\nTesting ssh_execute tool...")
    try:
        ssh = await connect_ssh()
        
        # Create mock context
        class MockRequest:
            class MockLifespan:
                def __init__(self, ssh):
                    self.ssh = ssh
            
            def __init__(self, ssh):
                self.lifespan_context = self.MockLifespan(ssh)
        
        class MockContext:
            def __init__(self, ssh):
                self.request_context = MockRequest(ssh)
        
        ctx = MockContext(ssh)
        
        # Test execution
        result = await ssh_execute("whoami", ctx=ctx)
        print(f"✓ Command output:\n{result}")
        
        ssh.close()
        await ssh.wait_closed()
        return True
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


async def main():
    print("=" * 60)
    print("MCP QEMU VM - SSH Tools Test Suite")
    print("=" * 60)
    print(f"\nConfiguration:")
    print(f"  VM_HOST: {os.getenv('VM_HOST', '192.168.122.79')}")
    print(f"  VM_USER: {os.getenv('VM_USER', 'vmrobot')}")
    print(f"  VM_PORT: {os.getenv('VM_PORT', '22')}")
    print()
    
    results = []
    
    # Run tests
    results.append(await test_connection())
    results.append(await test_ssh_execute())
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("✓ All tests passed!")
        return 0
    else:
        print("✗ Some tests failed")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
