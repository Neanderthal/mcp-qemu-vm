#!/usr/bin/env python3
"""Unit tests for the MCP QEMU VM server.

The bulk of these tests exercise the pure command-building, coordinate-scaling
and project-filesystem logic and need NO live VM. The live SSH smoke checks at
the bottom are NOT collected by pytest; run this file directly to use them:

    python test_ssh_tools.py
"""

import asyncio

import pytest

import server
from server import (
    DisplayCalibration,
    Project,
    _click_cmd,
    _keys_cmd,
    _move_cmd,
    _parse_frame_extents,
    _run_type,
    _scale_input,
    _scale_output,
    _type_cmd,
    get_screenshot,
)

DISPLAY_Q = "':0'"


class FakeSSH:
    """Records every ssh.run() invocation as (cmd, input)."""

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, cmd, *, input=None, check=False):  # noqa: A002
        self.calls.append((cmd, input))

        class _Result:
            stdout = ""
            stderr = ""
            returncode = 0

        return _Result()


# ---------- xdotool command builders ----------


def test_type_cmd_forces_utf8_locale():
    cmd = _type_cmd(DISPLAY_Q)
    assert "LC_ALL=" in cmd
    assert server.VM_LOCALE in cmd
    assert "xdotool type --delay 10 --clearmodifiers --file -" in cmd


def test_type_cmd_uses_custom_locale(monkeypatch):
    monkeypatch.setattr(server, "VM_LOCALE", "ru_RU.utf8")
    assert "ru_RU.utf8" in _type_cmd(DISPLAY_Q)


def test_keys_cmd_lowercases_and_joins():
    cmd = _keys_cmd(DISPLAY_Q, ["Ctrl", "Shift", "P"])
    assert cmd.endswith("xdotool key ctrl+shift+p")


@pytest.mark.parametrize("bad", ["a;b", "$(rm)", "Ctrl L", "`x`", "a|b"])
def test_keys_cmd_rejects_shell_metacharacters(bad):
    with pytest.raises(ValueError, match="Invalid key name"):
        _keys_cmd(DISPLAY_Q, [bad])


def test_click_cmd_maps_buttons():
    assert _click_cmd(DISPLAY_Q, "left", 1).endswith(" 1")
    assert _click_cmd(DISPLAY_Q, "middle", 1).endswith(" 2")
    assert _click_cmd(DISPLAY_Q, "right", 1).endswith(" 3")


def test_click_cmd_rejects_unknown_button():
    with pytest.raises(ValueError, match="left/middle/right"):
        _click_cmd(DISPLAY_Q, "scroll", 1)


@pytest.mark.parametrize("count,expected", [(0, 1), (-3, 1), (2, 2)])
def test_click_cmd_clamps_count(count, expected):
    assert f"--repeat {expected} " in _click_cmd(DISPLAY_Q, "left", count)


def test_move_cmd_absolute_and_relative():
    assert "mousemove --sync 5 7" in _move_cmd(DISPLAY_Q, 5, 7, "absolute")
    assert "mousemove_relative --sync 5 7" in _move_cmd(DISPLAY_Q, 5, 7, "relative")


def test_move_cmd_rejects_bad_mode():
    with pytest.raises(ValueError, match="absolute"):
        _move_cmd(DISPLAY_Q, 0, 0, "sideways")


# ---------- typing with newline handling (README Known Issues #2) ----------


def _type_inputs(ssh):
    """Text passed to xdotool type, in order (skips key/Return commands)."""
    return [inp for cmd, inp in ssh.calls if "xdotool type" in cmd]


def test_run_type_single_line_no_return():
    ssh = FakeSSH()
    asyncio.run(_run_type(ssh, DISPLAY_Q, "hello world"))
    assert len(ssh.calls) == 1
    assert ssh.calls[0][1] == "hello world"
    assert "xdotool type" in ssh.calls[0][0]


def test_run_type_multiline_presses_return_between_lines():
    ssh = FakeSSH()
    asyncio.run(_run_type(ssh, DISPLAY_Q, "line1\nline2\nline3"))
    # type line1, Return, type line2, Return, type line3
    assert _type_inputs(ssh) == ["line1", "line2", "line3"]
    returns = [cmd for cmd, _ in ssh.calls if "xdotool key Return" in cmd]
    assert len(returns) == 2


def test_run_type_blank_line_presses_return_without_typing():
    ssh = FakeSSH()
    asyncio.run(_run_type(ssh, DISPLAY_Q, "a\n\nb"))
    # ["a", "", "b"] -> type a, Return, Return, type b
    assert _type_inputs(ssh) == ["a", "b"]
    returns = [cmd for cmd, _ in ssh.calls if "xdotool key Return" in cmd]
    assert len(returns) == 2


def test_run_type_normalizes_crlf_and_cr():
    for text in ("a\r\nb", "a\rb"):
        ssh = FakeSSH()
        asyncio.run(_run_type(ssh, DISPLAY_Q, text))
        assert _type_inputs(ssh) == ["a", "b"]
        assert any("xdotool key Return" in cmd for cmd, _ in ssh.calls)


def test_run_type_never_sends_literal_newline_to_xdotool():
    ssh = FakeSSH()
    asyncio.run(_run_type(ssh, DISPLAY_Q, "first\nsecond"))
    assert all("\n" not in (inp or "") for _, inp in ssh.calls)


# ---------- coordinate scaling ----------


def test_scale_roundtrip():
    cal = DisplayCalibration(3840, 2160, 1920, 1080, 2.0, 2.0)
    assert _scale_input(cal, 100, 200) == (200, 400)
    assert _scale_output(cal, 200, 400) == (100, 200)


def test_scale_output_guards_against_zero_scale():
    cal = DisplayCalibration(0, 0, 0, 0, 0.0, 0.0)
    assert _scale_output(cal, 50, 60) == (50, 60)


# ---------- frame extents parsing ----------


def test_parse_frame_extents():
    line = "_NET_FRAME_EXTENTS(CARDINAL) = 1, 1, 30, 1"
    assert _parse_frame_extents(line) == (1, 1, 30, 1)


def test_parse_frame_extents_defaults_when_missing():
    assert _parse_frame_extents("nothing here") == (0, 0, 0, 0)


# ---------- project filesystem logic ----------


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path)
    return Project.create("unit-test", "desc")


def test_save_result_writes_into_results(project):
    path = project.save_result("out.txt", "hello")
    assert path.parent.name == "results"
    assert path.read_text() == "hello"


@pytest.mark.parametrize("evil", ["../escape.txt", "../../etc/passwd", "a/b/c.txt"])
def test_save_result_strips_path_traversal(project, evil):
    path = project.save_result(evil, "x")
    # Always lands directly inside results/, never escapes it.
    assert path.parent == project.path / "results"
    assert ".." not in path.parts[len(project.path.parts):]


@pytest.mark.parametrize("bad", ["..", ".", "/", ""])
def test_save_result_rejects_empty_or_dot_names(project, bad):
    with pytest.raises(ValueError, match="Invalid result filename"):
        project.save_result(bad, "x")


def test_save_advice_sanitizes_title(project):
    path = project.save_advice("Focus: in Citrix/RDP!", "body text")
    assert path.parent.name == "advice"
    assert "/" not in path.name
    assert path.read_text().startswith("# Focus: in Citrix/RDP!")


def test_get_all_advice_roundtrip(project):
    project.save_advice("Tip One", "First body")
    advice = project.get_all_advice()
    assert advice[0]["title"] == "Tip One"
    assert advice[0]["content"] == "First body"


def test_project_load_roundtrip(project):
    loaded = Project.load(project.path)
    assert loaded.name == project.name
    assert loaded.created_at == project.created_at


# ---------- screenshot resource id validation ----------


@pytest.mark.parametrize("bad_sid", ["../../etc/passwd", "..", "abc", "a/b"])
def test_get_screenshot_rejects_bad_sid(bad_sid):
    with pytest.raises(FileNotFoundError):
        asyncio.run(get_screenshot(bad_sid))


# ---------- live SSH smoke checks (manual, not collected by pytest) ----------


async def check_connection() -> bool:
    """Manual smoke check: requires a reachable VM."""
    from server import connect_ssh

    try:
        ssh = await connect_ssh()
        result = await ssh.run("uname -a", check=True)
        print(f"✓ VM Info: {(result.stdout or '').strip()}")
        ssh.close()
        await ssh.wait_closed()
        return True
    except Exception as e:  # noqa: BLE001 - smoke check, report and continue
        print(f"✗ Connection failed: {e}")
        return False


if __name__ == "__main__":
    print("Running live SSH smoke check (requires a reachable VM)...")
    ok = asyncio.run(check_connection())
    raise SystemExit(0 if ok else 1)
