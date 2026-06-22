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
    ACTION_HANDLERS,
    DisplayCalibration,
    Project,
    ZoomRegion,
    _act_activate_window,
    _act_click,
    _act_drag,
    _act_key_down,
    _act_key_up,
    _act_move_mouse,
    _act_paste,
    _act_press_keys,
    _act_screenshot,
    _act_scroll,
    _act_set_clipboard,
    _act_type_text,
    _act_wait,
    _activate_window,
    _capture_screenshot,
    _click_at_cmd,
    _click_cmd,
    _clipboard_set_cmd,
    _crop_zoom,
    _drag_cmd,
    _key_event_cmd,
    _keys_cmd,
    _match_text_boxes,
    _move_cmd,
    _parse_frame_extents,
    _run_type,
    _scale_input,
    _scale_output,
    _scroll_cmd,
    _type_cmd,
    _zoom_map,
    _zoom_region,
    get_screenshot,
    run_actions,
)

DISPLAY_Q = "':0'"


class FakeSSH:
    """Records every ssh.run() invocation as (cmd, input).

    *stdout* is returned for every command (default empty), enough for the
    pure command-construction tests that don't depend on real output.
    """

    def __init__(self, stdout: str = ""):
        self.calls: list[tuple[str, str | None]] = []
        self._stdout = stdout

    async def run(self, cmd, *, input=None, check=False):  # noqa: A002
        self.calls.append((cmd, input))
        return type(
            "_Result",
            (),
            {"stdout": self._stdout, "stderr": "", "returncode": 0},
        )()


class FakeApp:
    """Stand-in for AppContext: holds ssh, calibration, and project."""

    def __init__(self, ssh, calibration=None, project=None):
        self.ssh = ssh
        self.calibration = calibration or DisplayCalibration(0, 0, 0, 0, 1.0, 1.0)
        self.project = project


class FakeCtx:
    """Minimal Context whose lifespan_context is a FakeApp."""

    def __init__(self, app):
        rc = type("RC", (), {})()
        rc.lifespan_context = app
        self.request_context = rc


def _cmds(ssh):
    """Just the command strings sent to ssh.run()."""
    return [cmd for cmd, _ in ssh.calls]


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


@pytest.mark.parametrize("event", ["key", "keydown", "keyup"])
def test_key_event_cmd_emits_each_event(event):
    cmd = _key_event_cmd(DISPLAY_Q, ["Ctrl", "Shift"], event)
    assert f"xdotool {event} ctrl+shift" in cmd


def test_key_event_cmd_rejects_bad_event():
    with pytest.raises(ValueError, match="key/keydown/keyup"):
        _key_event_cmd(DISPLAY_Q, ["a"], "press")


def test_key_event_cmd_validates_keys():
    with pytest.raises(ValueError, match="Invalid key name"):
        _key_event_cmd(DISPLAY_Q, ["a;b"], "keydown")


@pytest.mark.parametrize(
    "keys,expected",
    [
        # named keysyms must keep their case (lowercase is ignored by xdotool)
        (["Page_Up"], "Page_Up"),
        (["Page_Down"], "Page_Down"),
        (["Prior"], "Prior"),
        (["Next"], "Next"),
        (["Home"], "Home"),
        (["End"], "End"),
        (["Return"], "Return"),
        (["BackSpace"], "BackSpace"),
        (["F4"], "F4"),
        (["KP_Enter"], "KP_Enter"),
        # single letters fold to lowercase so Ctrl+L means ctrl+l
        (["Ctrl", "L"], "ctrl+l"),
        (["Ctrl", "Shift", "P"], "ctrl+shift+p"),
        # named keysym combined with a modifier keeps case (this is the PgDn bug)
        (["Ctrl", "Home"], "ctrl+Home"),
        (["Alt", "F4"], "alt+F4"),
        (["Shift", "Page_Down"], "shift+Page_Down"),
        # modifier aliases normalise to xdotool forms
        (["Control", "a"], "ctrl+a"),
        (["Win", "d"], "super+d"),
        (["Meta", "Tab"], "super+Tab"),
    ],
)
def test_key_event_cmd_preserves_named_keysym_case(keys, expected):
    assert _key_event_cmd(DISPLAY_Q, keys).endswith(f"xdotool key {expected}")


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


# ---------- scroll / drag / click-at-xy builders ----------


@pytest.mark.parametrize(
    "direction,btn", [("up", 4), ("down", 5), ("left", 6), ("right", 7)]
)
def test_scroll_cmd_maps_direction_to_button(direction, btn):
    assert _scroll_cmd(DISPLAY_Q, direction, 3).endswith(f"--repeat 3 {btn}")


def test_scroll_cmd_clamps_amount():
    assert "--repeat 1 5" in _scroll_cmd(DISPLAY_Q, "down", 0)


def test_scroll_cmd_rejects_bad_direction():
    with pytest.raises(ValueError, match="up/down/left/right"):
        _scroll_cmd(DISPLAY_Q, "diagonal", 1)


def test_click_at_cmd_moves_then_clicks():
    cmd = _click_at_cmd(DISPLAY_Q, 50, 60, "left", 1)
    move_idx = cmd.index("mousemove --sync 50 60")
    click_idx = cmd.index("xdotool click")
    assert move_idx < click_idx
    assert " && " in cmd


def test_drag_cmd_sequence_and_button():
    cmd = _drag_cmd(DISPLAY_Q, 10, 20, 30, 40, "left")
    # press at start, move to end, release — in that order
    order = [
        cmd.index("mousemove --sync 10 20"),
        cmd.index("mousedown 1"),
        cmd.index("mousemove --sync 30 40"),
        cmd.index("mouseup 1"),
    ]
    assert order == sorted(order)


def test_drag_cmd_rejects_bad_button():
    with pytest.raises(ValueError, match="left/middle/right"):
        _drag_cmd(DISPLAY_Q, 0, 0, 1, 1, "scroll")


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


# ---------- run_actions batch dispatch (ACTION_HANDLERS registry) ----------


def test_action_handlers_registry_is_canonical_set():
    assert set(ACTION_HANDLERS) == {
        "press_keys",
        "type_text",
        "click",
        "move_mouse",
        "scroll",
        "drag",
        "key_down",
        "key_up",
        "activate_window",
        "screenshot",
        "set_clipboard",
        "paste",
        "click_text",
        "wait",
    }


def test_act_scroll_summary_and_command():
    ssh = FakeSSH()
    summary = asyncio.run(
        _act_scroll(FakeApp(ssh), DISPLAY_Q, {"direction": "down", "amount": 4})
    )
    assert summary == "scroll down x4"
    assert any("--repeat 4 5" in c for c in _cmds(ssh))


def test_act_drag_scales_and_summarizes():
    ssh = FakeSSH()
    app = FakeApp(ssh, DisplayCalibration(0, 0, 0, 0, 2.0, 2.0))
    summary = asyncio.run(
        _act_drag(app, DISPLAY_Q, {"x1": 10, "y1": 20, "x2": 30, "y2": 40})
    )
    assert summary == "drag left (10, 20) -> (30, 40)"
    cmds = _cmds(ssh)
    assert any("mousemove --sync 20 40" in c for c in cmds)  # scaled start
    assert any("mousemove --sync 60 80" in c for c in cmds)  # scaled end
    assert any("mousedown 1" in c for c in cmds)


def test_act_click_with_xy_moves_then_clicks():
    ssh = FakeSSH()
    summary = asyncio.run(
        _act_click(FakeApp(ssh), DISPLAY_Q, {"x": 100, "y": 200, "button": "left"})
    )
    assert summary == "click left x1 at (100, 200)"
    assert any("mousemove --sync 100 200" in c for c in _cmds(ssh))


def test_act_click_without_xy_clicks_in_place():
    ssh = FakeSSH()
    summary = asyncio.run(_act_click(FakeApp(ssh), DISPLAY_Q, {"button": "left"}))
    assert summary == "click left x1"
    assert all("mousemove" not in c for c in _cmds(ssh))


def test_act_click_runs_click_cmd_and_summarizes():
    ssh = FakeSSH()
    summary = asyncio.run(
        _act_click(FakeApp(ssh), DISPLAY_Q, {"button": "right", "count": 2})
    )
    assert summary == "click right x2"
    assert any("xdotool click --repeat 2 3" in c for c in _cmds(ssh))


def test_act_move_mouse_scales_and_summarizes():
    ssh = FakeSSH()
    app = FakeApp(ssh, DisplayCalibration(0, 0, 0, 0, 2.0, 2.0))
    summary = asyncio.run(
        _act_move_mouse(app, DISPLAY_Q, {"x": 10, "y": 20, "mode": "absolute"})
    )
    assert summary == "move_mouse (10, 20) [absolute]"
    assert any("mousemove --sync 20 40" in c for c in _cmds(ssh))


def test_act_type_text_summarizes_and_handles_newlines():
    ssh = FakeSSH()
    summary = asyncio.run(_act_type_text(FakeApp(ssh), DISPLAY_Q, {"text": "a\nb"}))
    assert summary == "type_text (3 chars)"
    assert any("xdotool key Return" in c for c in _cmds(ssh))


def test_act_press_keys_summary():
    ssh = FakeSSH()
    summary = asyncio.run(
        _act_press_keys(FakeApp(ssh), DISPLAY_Q, {"keys": ["Ctrl", "a"]})
    )
    assert summary == "press_keys ['Ctrl', 'a']"


def test_act_key_down_and_up():
    ssh = FakeSSH()
    down = asyncio.run(_act_key_down(FakeApp(ssh), DISPLAY_Q, {"keys": ["shift"]}))
    up = asyncio.run(_act_key_up(FakeApp(ssh), DISPLAY_Q, {"keys": ["shift"]}))
    assert down == "key_down ['shift']"
    assert up == "key_up ['shift']"
    assert any("xdotool keydown shift" in c for c in _cmds(ssh))
    assert any("xdotool keyup shift" in c for c in _cmds(ssh))


# ---------- activate_window ----------


def test_activate_window_by_id_issues_windowactivate():
    ssh = FakeSSH()
    result = asyncio.run(_activate_window(ssh, window_id=42))
    assert result.startswith("activated window 42:")
    assert any("windowactivate --sync 42" in c for c in _cmds(ssh))
    # window_id path must NOT run a search
    assert all("xdotool search" not in c for c in _cmds(ssh))


def test_activate_window_by_title_searches_then_activates():
    ssh = FakeSSH(stdout="98765")  # search + getwindowname both return this
    result = asyncio.run(_activate_window(ssh, title="Mousepad"))
    assert "98765" in result
    cmds = _cmds(ssh)
    assert any("xdotool search --name" in c for c in cmds)
    assert any("windowactivate --sync 98765" in c for c in cmds)


def test_activate_window_no_match_raises():
    ssh = FakeSSH(stdout="")  # search finds nothing
    with pytest.raises(ValueError, match="no window matching"):
        asyncio.run(_activate_window(ssh, title="Nonexistent"))


def test_activate_window_requires_a_selector():
    with pytest.raises(ValueError, match="provide either"):
        asyncio.run(_activate_window(FakeSSH()))


def test_act_activate_window_delegates():
    ssh = FakeSSH()
    result = asyncio.run(
        _act_activate_window(FakeApp(ssh), DISPLAY_Q, {"window_id": 7})
    )
    assert result.startswith("activated window 7:")


# ---------- screenshot as a batch action ----------


def test_capture_screenshot_requires_project():
    # app.project is None -> guard fires before any scrot/SFTP
    with pytest.raises(ValueError, match="No project initialized"):
        asyncio.run(_capture_screenshot(FakeApp(FakeSSH(), project=None)))


def test_act_screenshot_requires_project():
    with pytest.raises(ValueError, match="No project initialized"):
        asyncio.run(_act_screenshot(FakeApp(FakeSSH(), project=None), DISPLAY_Q, {}))


# ---------- clipboard / paste ----------


def test_clipboard_set_cmd_uses_xclip_and_discards_output():
    cmd = _clipboard_set_cmd(DISPLAY_Q)
    assert "xclip -selection clipboard -in" in cmd
    assert ">/dev/null 2>&1" in cmd


def test_act_set_clipboard_pipes_text_via_stdin():
    ssh = FakeSSH()
    summary = asyncio.run(
        _act_set_clipboard(FakeApp(ssh), DISPLAY_Q, {"text": "hello clip"})
    )
    assert summary == "set_clipboard (10 chars)"
    # text must be passed as stdin input, never inline in the command
    assert ("hello clip" in (inp or "") for _, inp in ssh.calls)
    assert all("hello clip" not in cmd for cmd, _ in ssh.calls)


def test_act_paste_with_text_sets_then_ctrl_v():
    ssh = FakeSSH()
    summary = asyncio.run(_act_paste(FakeApp(ssh), DISPLAY_Q, {"text": "payload"}))
    assert summary == "paste (7 chars)"
    cmds = _cmds(ssh)
    assert any("xclip -selection clipboard -in" in c for c in cmds)
    assert any("xdotool key ctrl+v" in c for c in cmds)


def test_act_paste_without_text_only_ctrl_v():
    ssh = FakeSSH()
    summary = asyncio.run(_act_paste(FakeApp(ssh), DISPLAY_Q, {}))
    assert summary == "paste"
    cmds = _cmds(ssh)
    assert all("xclip" not in c for c in cmds)
    assert any("xdotool key ctrl+v" in c for c in cmds)


def test_act_wait_summary():
    summary = asyncio.run(_act_wait(FakeApp(FakeSSH()), DISPLAY_Q, {"seconds": 0}))
    assert summary == "wait 0s"


def test_run_actions_executes_sequence_in_order():
    ssh = FakeSSH()
    ctx = FakeCtx(FakeApp(ssh))
    out = asyncio.run(
        run_actions(
            [
                {"action": "press_keys", "keys": ["Ctrl", "a"]},
                {"action": "type_text", "text": "hello"},
                {"action": "wait", "seconds": 0},
            ],
            ctx=ctx,
        )
    )
    assert "Executed 3 actions" in out
    assert "1. press_keys" in out and "2. type_text" in out and "3. wait" in out


def test_run_actions_unknown_action_errors_and_stops():
    ssh = FakeSSH()
    ctx = FakeCtx(FakeApp(ssh))
    out = asyncio.run(
        run_actions(
            [
                {"action": "click", "button": "left"},
                {"action": "bogus"},
                {"action": "wait", "seconds": 0},
            ],
            ctx=ctx,
        )
    )
    assert "ERROR in bogus" in out
    assert "unknown action" in out
    # stopped before reaching the 3rd action (wait)
    assert "3." not in out


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


# ---------- OCR text matching (_match_text_boxes) ----------


def _w(text, left, top, width=40, height=20, conf=90, line=(0, 0, 0)):
    """Build a synthetic OCR word box."""
    return {
        "text": text,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "conf": conf,
        "line": line,
    }


def test_match_single_word_returns_center():
    words = [_w("File", 10, 10), _w("Edit", 60, 10)]
    matches = _match_text_boxes(words, "Edit")
    assert len(matches) == 1
    assert matches[0]["text"] == "Edit"
    assert (matches[0]["cx"], matches[0]["cy"]) == (60 + 20, 10 + 10)


def test_match_is_case_insensitive_and_substring():
    matches = _match_text_boxes([_w("Submit", 100, 40, width=80)], "sub")
    assert len(matches) == 1
    assert matches[0]["text"] == "Submit"


def test_match_multiword_unions_adjacent_boxes():
    words = [_w("Save", 100, 50, width=50), _w("As", 160, 50, width=30, line=(0, 0, 0))]
    matches = _match_text_boxes(words, "Save As")
    assert len(matches) == 1
    m = matches[0]
    assert m["text"] == "Save As"
    # union: left=100, right=190 -> width 90; center x = 145
    assert m["left"] == 100 and m["width"] == 90
    assert m["cx"] == 145


def test_match_no_result_returns_empty():
    assert _match_text_boxes([_w("File", 0, 0)], "Quit") == []


def test_match_filters_low_confidence():
    words = [_w("Login", 10, 10, conf=12)]
    assert _match_text_boxes(words, "Login", min_conf=40) == []
    assert len(_match_text_boxes(words, "Login", min_conf=10)) == 1


def test_match_orders_results_top_to_bottom():
    words = [
        _w("OK", 500, 300, line=(0, 0, 5)),
        _w("OK", 500, 50, line=(0, 0, 1)),
    ]
    matches = _match_text_boxes(words, "OK")
    assert [m["top"] for m in matches] == [50, 300]


def test_match_empty_query_returns_empty():
    assert _match_text_boxes([_w("File", 0, 0)], "   ") == []


def test_match_ranks_exact_word_above_substring():
    # "No" should pick the real "No" button over the "no" inside "normally"/"not"
    words = [
        _w("normally", 100, 50, width=80, line=(0, 0, 0)),
        _w("not", 100, 80, width=30, line=(0, 0, 1)),
        _w("No", 100, 300, width=20, line=(0, 0, 9)),  # the button, lower down
    ]
    matches = _match_text_boxes(words, "No")
    assert len(matches) == 3
    assert matches[0]["text"] == "No"  # exact match ranked first despite being lowest


def test_action_handlers_includes_click_text():
    assert "click_text" in ACTION_HANDLERS


# ---------- zoom region + coordinate mapping ----------


def test_zoom_region_centers_and_keeps_size():
    z = _zoom_region(1000, 800, 500, 400, 200, 100, 3.0)
    assert (z.left, z.top, z.crop_w, z.crop_h) == (400, 350, 200, 100)
    assert z.scale == 3.0


def test_zoom_region_clamps_to_edges():
    # near top-left: origin clamps to 0
    z = _zoom_region(1000, 800, 10, 10, 200, 100, 2.0)
    assert (z.left, z.top) == (0, 0)
    # near bottom-right: origin clamps so the crop stays inside
    z = _zoom_region(1000, 800, 990, 790, 200, 100, 2.0)
    assert (z.left, z.top) == (800, 700)


def test_zoom_region_clamps_size_to_image():
    z = _zoom_region(640, 480, 100, 100, 2000, 2000, 2.0)
    assert (z.crop_w, z.crop_h) == (640, 480)


def test_zoom_map_inverts_the_transform():
    z = ZoomRegion(left=100, top=50, crop_w=200, crop_h=100, scale=4.0)
    # point 40,80 in the zoomed image -> 100+10, 50+20
    assert _zoom_map(z, 40, 80) == (110, 70)


def test_zoom_map_clamps_out_of_range_points():
    z = ZoomRegion(left=100, top=50, crop_w=200, crop_h=100, scale=4.0)
    fx, fy = _zoom_map(z, 100000, 100000)
    assert fx == 100 + 200 - 1 and fy == 50 + 100 - 1


def test_zoom_map_roundtrips_a_screen_point():
    z = ZoomRegion(left=100, top=50, crop_w=200, crop_h=100, scale=4.0)
    screen_x, screen_y = 150, 90  # a point inside the crop
    zx = (screen_x - z.left) * z.scale  # its location in the zoomed image
    zy = (screen_y - z.top) * z.scale
    assert _zoom_map(z, zx, zy) == (screen_x, screen_y)


def test_crop_zoom_produces_magnified_png_and_region():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 80), (200, 200, 200)).save(buf, format="PNG")
    zbytes, region = _crop_zoom(buf.getvalue(), 50, 40, 40, 20, 2.0)
    assert (region.left, region.top, region.crop_w, region.crop_h) == (30, 30, 40, 20)
    out = Image.open(io.BytesIO(zbytes))
    assert out.size == (80, 40)  # 40*2 x 20*2


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
