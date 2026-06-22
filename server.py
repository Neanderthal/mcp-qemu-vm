import asyncio
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncssh
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

# ---------- Config ----------
VM_HOST = os.getenv("VM_HOST", "192.168.122.79")
VM_USER = os.getenv("VM_USER", "vmrobot")
VM_PORT = int(os.getenv("VM_PORT", "22"))
VM_DISPLAY = os.getenv("VM_DISPLAY", ":0")
VM_IDENTITY = os.getenv("VM_IDENTITY", "")  # path to private key, optional
VM_DESKTOP_USER = os.getenv("VM_DESKTOP_USER", "")  # if different from VM_USER
# UTF-8 locale for `xdotool type`. The vmrobot/desktop SSH environment often has
# no UTF-8 LC_CTYPE, so multi-byte input (Cyrillic, etc.) fails with "Invalid
# multi-byte sequence". Forcing LC_ALL here makes type_text work for non-ASCII.
VM_LOCALE = os.getenv("VM_LOCALE", "C.UTF-8")

PROJECTS_DIR = pathlib.Path("data/projects")
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Every legitimate xdotool key name (Return, BackSpace, Ctrl, F12, KP_Enter, space)
# matches this pattern. Rejects shell metacharacters like ; $ ` |
VALID_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

# xdotool button numbers
BUTTON_MAP = {"left": 1, "middle": 2, "right": 3}
# Mouse-wheel scroll directions map to xdotool button numbers
SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}


# ---------- xdotool command builders ----------
# Shared by the standalone tools and the run_actions() batch loop so the two
# never diverge. Each takes an already-shlex-quoted display string and returns
# a shell command to run on the VM. Caller is responsible for execution.


def _type_cmd(display_q: str) -> str:
    """Build the `xdotool type` command. Text is piped via stdin (--file -),
    so it never touches the shell. LC_ALL forces a UTF-8 locale so non-ASCII
    (Cyrillic, etc.) decodes correctly."""
    return (
        f"LC_ALL={shlex.quote(VM_LOCALE)} DISPLAY={display_q} "
        "xdotool type --delay 10 --clearmodifiers --file -"
    )


def _key_event_cmd(display_q: str, keys: list[str], event: str = "key") -> str:
    """Build an xdotool key-event command from a validated key list.

    *event* is one of ``key`` (press+release), ``keydown`` (hold), or
    ``keyup`` (release). Key names are validated against VALID_KEY_PATTERN and
    the combo is shlex-quoted, so shell metacharacters can't slip through.
    """
    if event not in ("key", "keydown", "keyup"):
        raise ValueError("event must be key/keydown/keyup")
    for k in keys:
        if not VALID_KEY_PATTERN.match(k):
            raise ValueError(f"Invalid key name: {k!r}")
    combo = "+".join(k.lower() for k in keys)
    return f"DISPLAY={display_q} xdotool {event} {shlex.quote(combo)}"


def _keys_cmd(display_q: str, keys: list[str]) -> str:
    """Build an `xdotool key` (press+release) command from a key list."""
    return _key_event_cmd(display_q, keys, "key")


def _clipboard_set_cmd(display_q: str) -> str:
    """Build the command that loads the X clipboard from stdin via xclip.

    Text is piped in (never on the command line). xclip's stdout/stderr are
    discarded so the forked selection owner doesn't hold the SSH channel open.
    """
    return f"DISPLAY={display_q} xclip -selection clipboard -in >/dev/null 2>&1"


def _click_cmd(display_q: str, button: str, count: int) -> str:
    """Build an `xdotool click` command. Count is clamped to >= 1 (xdotool
    rejects --repeat 0)."""
    if button not in BUTTON_MAP:
        raise ValueError("button must be left/middle/right")
    count = max(1, int(count))
    return f"DISPLAY={display_q} xdotool click --repeat {count} {BUTTON_MAP[button]}"


def _move_cmd(display_q: str, sx: int, sy: int, mode: str) -> str:
    """Build an `xdotool mousemove` command (absolute or relative)."""
    if mode == "absolute":
        return f"DISPLAY={display_q} xdotool mousemove --sync {sx} {sy}"
    if mode == "relative":
        return f"DISPLAY={display_q} xdotool mousemove_relative --sync {sx} {sy}"
    raise ValueError("mode must be 'absolute' or 'relative'")


def _scroll_cmd(display_q: str, direction: str, amount: int) -> str:
    """Build a mouse-wheel scroll command. Scrolling is N wheel 'clicks' on
    xdotool buttons 4-7. Amount is clamped to >= 1."""
    if direction not in SCROLL_BUTTONS:
        raise ValueError("direction must be up/down/left/right")
    amount = max(1, int(amount))
    btn = SCROLL_BUTTONS[direction]
    return f"DISPLAY={display_q} xdotool click --repeat {amount} {btn}"


def _click_at_cmd(display_q: str, sx: int, sy: int, button: str, count: int) -> str:
    """Build a move-then-click command (absolute screen coords already scaled).
    Combining the move and click in one shell command removes the focus/race
    window between a separate move_mouse + click."""
    move = _move_cmd(display_q, sx, sy, "absolute")
    click = _click_cmd(display_q, button, count)
    return f"{move} && {click}"


def _drag_cmd(
    display_q: str, sx1: int, sy1: int, sx2: int, sy2: int, button: str
) -> str:
    """Build a press-move-release drag between two scaled screen points."""
    if button not in BUTTON_MAP:
        raise ValueError("button must be left/middle/right")
    b = BUTTON_MAP[button]
    return (
        f"DISPLAY={display_q} xdotool mousemove --sync {sx1} {sy1} && "
        f"DISPLAY={display_q} xdotool mousedown {b} && "
        f"DISPLAY={display_q} xdotool mousemove --sync {sx2} {sy2} && "
        f"DISPLAY={display_q} xdotool mouseup {b}"
    )


# ---------- Project Management ----------


@dataclass
class Project:
    """Manages a project's folder structure and metadata."""

    name: str
    path: pathlib.Path
    created_at: str
    description: str = ""

    @classmethod
    def create(cls, name: str, description: str = "") -> "Project":
        """Create a new project with folder structure."""
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        project_path = PROJECTS_DIR / f"{timestamp}_{name}"

        # Create folder structure
        project_path.mkdir(parents=True, exist_ok=True)
        (project_path / "screenshots").mkdir(exist_ok=True)
        (project_path / "logs").mkdir(exist_ok=True)
        (project_path / "results").mkdir(exist_ok=True)
        (project_path / "advice").mkdir(exist_ok=True)

        project = cls(
            name=name,
            path=project_path,
            created_at=timestamp,
            description=description,
        )
        project._save_metadata()
        project._log("Project initialized")
        return project

    @classmethod
    def load(cls, project_path: pathlib.Path) -> "Project":
        """Load an existing project from its metadata."""
        metadata_file = project_path / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"No metadata.json in {project_path}")

        with open(metadata_file) as f:
            data = json.load(f)

        return cls(
            name=data["name"],
            path=project_path,
            created_at=data["created_at"],
            description=data.get("description", ""),
        )

    def _save_metadata(self) -> None:
        """Save project metadata to JSON file."""
        metadata = {
            "name": self.name,
            "created_at": self.created_at,
            "description": self.description,
        }
        with open(self.path / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

    def _log(self, message: str, level: str = "INFO") -> None:
        """Append a log entry to the project log file."""
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        log_file = self.path / "logs" / "project.log"
        with open(log_file, "a") as f:
            f.write(log_line)

    def log(self, message: str, level: str = "INFO") -> str:
        """Public logging method."""
        self._log(message, level)
        return f"Logged: [{level}] {message}"

    def screenshot_path(self, screenshot_id: str) -> pathlib.Path:
        """Get the path for a screenshot in this project."""
        return self.path / "screenshots" / f"{screenshot_id}.png"

    def save_result(self, filename: str, content: str) -> pathlib.Path:
        """Save a result file to the project.

        The filename is reduced to its basename to prevent path traversal
        (e.g. ``../../etc/passwd``) escaping the results directory.
        """
        safe_name = pathlib.Path(filename).name
        if not safe_name or safe_name in (".", ".."):
            raise ValueError(f"Invalid result filename: {filename!r}")
        result_path = self.path / "results" / safe_name
        with open(result_path, "w") as f:
            f.write(content)
        self._log(f"Result saved: {safe_name}")
        return result_path

    def save_advice(self, title: str, content: str) -> pathlib.Path:
        """Save an advice/tip for future LLM sessions."""
        # Create a safe filename from title
        safe_title = "".join(c if c.isalnum() or c in "- _" else "_" for c in title)
        safe_title = safe_title[:50]  # Limit length
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        filename = f"{timestamp}_{safe_title}.md"

        advice_path = self.path / "advice" / filename
        with open(advice_path, "w") as f:
            f.write(f"# {title}\n\n{content}\n")
        self._log(f"Advice saved: {title}")
        return advice_path

    def get_all_advice(self) -> list[dict]:
        """Get all advice files from the project."""
        advice_dir = self.path / "advice"
        if not advice_dir.exists():
            return []

        advice_list = []
        for advice_file in sorted(advice_dir.glob("*.md")):
            content = advice_file.read_text()
            # Extract title from first line (# Title format)
            lines = content.strip().split("\n")
            title = lines[0].lstrip("# ").strip() if lines else advice_file.stem
            body = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
            advice_list.append(
                {
                    "title": title,
                    "content": body,
                    "file": advice_file.name,
                }
            )
        return advice_list

    def get_info(self) -> dict:
        """Get project information and statistics."""
        screenshots = list((self.path / "screenshots").glob("*.png"))
        results = list((self.path / "results").glob("*"))
        log_file = self.path / "logs" / "project.log"
        log_lines = log_file.read_text().count("\n") if log_file.exists() else 0

        return {
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at,
            "description": self.description,
            "screenshot_count": len(screenshots),
            "result_count": len(results),
            "log_entries": log_lines,
        }


# ---------- SSH connection management ----------


@dataclass
class DisplayCalibration:
    """Scale factors between xdotool coordinate space and screenshot pixels."""

    xdotool_w: int
    xdotool_h: int
    screenshot_w: int
    screenshot_h: int
    scale_x: float  # xdotool_w / screenshot_w
    scale_y: float  # xdotool_h / screenshot_h


@dataclass
class ZoomRegion:
    """A magnified crop of the screen and how to map its pixels back.

    A point (zx, zy) in the zoomed image maps to the full-screen
    (screenshot-space) point (left + zx/scale, top + zy/scale).
    """

    left: int  # crop origin x in screenshot-space
    top: int  # crop origin y in screenshot-space
    crop_w: int  # crop width in screenshot-space
    crop_h: int  # crop height in screenshot-space
    scale: float  # magnification factor


@dataclass
class AppContext:
    ssh: asyncssh.SSHClientConnection
    calibration: DisplayCalibration
    project: Project | None = None
    last_zoom: ZoomRegion | None = None  # set by zoom(), used by click_zoomed()
    last_marks: list[dict] | None = None  # set by mark_screen(), used by click_mark()


async def connect_ssh() -> asyncssh.SSHClientConnection:
    # known_hosts defaults to None for local ephemeral QEMU VMs whose host keys
    # change on every rebuild. Set VM_KNOWN_HOSTS to a path to enable verification.
    kwargs: dict = dict(
        host=VM_HOST,
        port=VM_PORT,
        username=VM_USER,
        known_hosts=os.getenv("VM_KNOWN_HOSTS") or None,
        connect_timeout=int(os.getenv("VM_CONNECT_TIMEOUT", "10")),
        keepalive_interval=30,
        keepalive_count_max=3,
    )
    if VM_IDENTITY:
        kwargs["client_keys"] = [VM_IDENTITY]

    return await asyncssh.connect(**kwargs)


async def run_vm_cmd(
    ssh: asyncssh.SSHClientConnection,
    cmd: str,
    *,
    as_desktop_user: bool = False,
) -> str:
    """Run a command inside the VM and return stdout.

    If *as_desktop_user* is True and VM_DESKTOP_USER is set, the command
    is wrapped with ``sudo -u <desktop_user>`` so it runs in the desktop
    user's context (needed for clipboard, pass, dbus, etc.).
    """
    if as_desktop_user and VM_DESKTOP_USER:
        cmd = f"sudo -u {shlex.quote(VM_DESKTOP_USER)} {cmd}"
    result = await ssh.run(cmd, check=True)
    return (result.stdout or "").strip()


async def _run_type(
    ssh: asyncssh.SSHClientConnection,
    display_q: str,
    text: str,
) -> None:
    """Type *text* into the VM, one line at a time.

    Newlines are sent as explicit ``Return`` key presses rather than typed as a
    literal LF. In nested / rich-editor contexts (Citrix -> Windows -> Outlook) a
    literal LF from ``xdotool type`` is delivered as a stray control-character
    glyph instead of a paragraph break (README Known Issues #2). Splitting on
    newlines and pressing Return is robust in both terminals and rich editors.

    Each line's text still goes to xdotool via stdin (--file -), so it never
    touches the shell.
    """
    # Normalise CRLF / CR so we only split on \n.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    segments = normalized.split("\n")
    type_cmd = _type_cmd(display_q)
    return_cmd = f"DISPLAY={display_q} xdotool key Return"
    for idx, segment in enumerate(segments):
        if segment:
            await ssh.run(type_cmd, input=segment, check=True)
        if idx < len(segments) - 1:
            await run_vm_cmd(ssh, return_cmd)


# ---------- OCR text location ----------
# Tier 1 of the object-location cascade: the LLM names a target by its visible
# text; the host OCRs the full-res screenshot and returns exact pixel boxes in
# screenshot-space, which feed straight into the existing click path. Detection
# runs on the host (tesseract) against the downloaded PNG — the VM stays lean.


async def _grab_png(ssh: asyncssh.SSHClientConnection) -> bytes:
    """Capture the VM screen and return the PNG bytes (no project needed).

    scrot writes to a temp file on the VM; we read it over SFTP and delete it.
    """
    sid = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
    remote_path = f"/tmp/mcp-ocr-{sid}.png"
    display = shlex.quote(VM_DISPLAY)
    await run_vm_cmd(ssh, f"DISPLAY={display} scrot {shlex.quote(remote_path)}")
    try:
        async with ssh.start_sftp_client() as sftp:
            async with sftp.open(remote_path, "rb") as f:
                data = await f.read()
    finally:
        await run_vm_cmd(ssh, f"rm -f {shlex.quote(remote_path)}")
    return data


def _ocr_words(png_bytes: bytes) -> list[dict]:
    """Run tesseract on PNG bytes and return word boxes in screenshot-space.

    Each word: {text, conf, left, top, width, height, line}. Pillow, pytesseract
    and the tesseract binary must be installed on the host (lazy-imported so the
    server still runs without them). This is CPU-bound — call via asyncio.to_thread.
    """
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on host install
        raise RuntimeError(
            "OCR needs Pillow + pytesseract + the tesseract binary on the host"
        ) from exc

    # Convert to grayscale first. tesseract binarises far better on a single
    # luminance channel than on raw RGB — on themed desktops (e.g. green-on-dark
    # terminal text) raw RGB can yield near-zero detections while grayscale
    # recovers everything at high confidence.
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words: list[dict] = []
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        words.append(
            {
                "text": text,
                "conf": conf,
                "left": int(data["left"][i]),
                "top": int(data["top"][i]),
                "width": int(data["width"][i]),
                "height": int(data["height"][i]),
                "line": (
                    data["block_num"][i],
                    data["par_num"][i],
                    data["line_num"][i],
                ),
            }
        )
    return words


def _union_box(boxes: list[dict]) -> tuple[int, int, int, int]:
    """Bounding box (left, top, width, height) covering several word boxes."""
    left = min(b["left"] for b in boxes)
    top = min(b["top"] for b in boxes)
    right = max(b["left"] + b["width"] for b in boxes)
    bottom = max(b["top"] + b["height"] for b in boxes)
    return left, top, right - left, bottom - top


def _mk_text_match(span: list[dict]) -> dict:
    """Build a match dict (union box + center) from a run of word boxes."""
    left, top, width, height = _union_box(span)
    return {
        "text": " ".join(w["text"] for w in span),
        "conf": min(w["conf"] for w in span),
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "cx": left + width // 2,
        "cy": top + height // 2,
    }


def _match_text_boxes(
    words: list[dict], query: str, min_conf: float = 40.0
) -> list[dict]:
    """Find screen locations matching *query* (case-insensitive).

    Query tokens are matched per word, anchored to word boundaries: a single-token
    query matches any single word containing it (so "edit" hits "Edit", not the
    "edit" inside "File Edit"); a multi-token query like "Save As" matches a window
    of consecutive words where token k is contained in word k, and unions their
    boxes. Returns match dicts with the union box plus center (cx, cy), in reading
    order, de-duplicated. Pure/synchronous — unit-testable without OCR or a VM.
    """
    qt = query.lower().split()
    if not qt:
        return []

    kept = [w for w in words if w["conf"] >= min_conf and w["text"].strip()]
    lines: dict = {}
    for w in kept:
        lines.setdefault(w["line"], []).append(w)

    matches: list[dict] = []
    for line_words in lines.values():
        line_words.sort(key=lambda w: w["left"])
        texts = [w["text"].lower() for w in line_words]
        n = len(line_words)
        if len(qt) == 1:
            matches += [
                _mk_text_match(line_words[k : k + 1])
                for k in range(n)
                if qt[0] in texts[k]
            ]
        else:
            for i in range(n - len(qt) + 1):
                if all(qt[t] in texts[i + t] for t in range(len(qt))):
                    matches.append(_mk_text_match(line_words[i : i + len(qt)]))

    # Rank exact whole-text matches ahead of partial/substring ones, then read
    # order. So click_text("No") prefers a real "No" button over the "no" inside
    # "normally"/"not".
    q_norm = " ".join(qt)
    matches.sort(
        key=lambda m: (0 if m["text"].lower() == q_norm else 1, m["top"], m["left"])
    )
    deduped: list[dict] = []
    for m in matches:
        if not any(
            abs(m["cx"] - d["cx"]) < 5 and abs(m["cy"] - d["cy"]) < 5 for d in deduped
        ):
            deduped.append(m)
    return deduped


# ---------- Zoom (crop + magnify, with coordinate mapping) ----------
# Tier 4 of the cascade: magnify a region so small/low-contrast detail is legible,
# and remember the crop so a point picked in the zoomed image maps back to the
# exact full-screen pixel (no coordinate math for the caller).


def _zoom_region(
    img_w: int, img_h: int, x: int, y: int, w: int, h: int, scale: float
) -> ZoomRegion:
    """Compute a crop of size ~(w, h) centered on (x, y), clamped to the image.

    Pure/synchronous — unit-testable without PIL or a VM.
    """
    w = max(1, min(int(w), img_w))
    h = max(1, min(int(h), img_h))
    left = max(0, min(int(x) - w // 2, img_w - w))
    top = max(0, min(int(y) - h // 2, img_h - h))
    return ZoomRegion(left=left, top=top, crop_w=w, crop_h=h, scale=float(scale))


def _zoom_map(z: ZoomRegion, zx: int, zy: int) -> tuple[int, int]:
    """Map a point in the zoomed image back to full-screen (screenshot-space).

    Clamped to the crop bounds. Inverse of the crop+upscale transform.
    """
    fx = z.left + round(int(zx) / z.scale)
    fy = z.top + round(int(zy) / z.scale)
    fx = max(z.left, min(fx, z.left + z.crop_w - 1))
    fy = max(z.top, min(fy, z.top + z.crop_h - 1))
    return fx, fy


def _crop_zoom(
    png_bytes: bytes, x: int, y: int, w: int, h: int, scale: float
) -> tuple[bytes, ZoomRegion]:
    """Crop a region around (x, y) from a full-screen PNG and magnify it.

    Returns (zoomed_png_bytes, ZoomRegion). PIL is lazy-imported so the server
    still runs without it. CPU-bound — call via asyncio.to_thread.
    """
    try:
        import io

        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on host install
        raise RuntimeError("zoom needs Pillow on the host") from exc

    img = Image.open(io.BytesIO(png_bytes))
    region = _zoom_region(img.width, img.height, x, y, w, h, scale)
    box = (
        region.left,
        region.top,
        region.left + region.crop_w,
        region.top + region.crop_h,
    )
    zoom_w = int(region.crop_w * region.scale)
    zoom_h = int(region.crop_h * region.scale)
    lanczos = getattr(Image, "Resampling", Image).LANCZOS  # Pillow >=9.1 moved it
    zimg = img.crop(box).resize((zoom_w, zoom_h), lanczos)
    buf = io.BytesIO()
    zimg.save(buf, format="PNG")
    return buf.getvalue(), region


# ---------- Set-of-Mark (numbered overlays for pick-by-number) ----------
# Overlay numbered marks on detected text so the model picks an INDEX (a
# classification it's good at) instead of estimating coordinates. Candidates
# come from OCR; click_mark(n) clicks the stored center of mark n.


def _build_marks(
    words: list[dict], min_conf: float = 50.0, max_marks: int = 80
) -> list[dict]:
    """Turn OCR words into numbered marks in reading order.

    Filters by confidence, sorts top-to-bottom/left-to-right, caps at max_marks,
    and assigns sequential ids. Each mark carries its box, center, and text.
    Pure/synchronous — unit-testable without OCR or a VM.
    """
    cands = [w for w in words if w["conf"] >= min_conf and w["text"].strip()]
    cands.sort(key=lambda w: (w["top"], w["left"]))
    marks: list[dict] = []
    for i, w in enumerate(cands[: max(0, int(max_marks))]):
        marks.append(
            {
                "id": i,
                "text": w["text"],
                "left": w["left"],
                "top": w["top"],
                "width": w["width"],
                "height": w["height"],
                "cx": w["left"] + w["width"] // 2,
                "cy": w["top"] + w["height"] // 2,
            }
        )
    return marks


def _render_marks(png_bytes: bytes, marks: list[dict]) -> bytes:
    """Draw numbered red boxes/labels on the screenshot for each mark.

    PIL is lazy-imported. CPU-bound — call via asyncio.to_thread.
    """
    try:
        import io

        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - depends on host install
        raise RuntimeError("mark_screen needs Pillow on the host") from exc

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = None
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, 16)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    for m in marks:
        x0, y0 = m["left"], m["top"]
        x1, y1 = x0 + m["width"], y0 + m["height"]
        draw.rectangle((x0, y0, x1, y1), outline=(255, 0, 0), width=2)
        label = str(m["id"])
        try:
            tw = int(draw.textlength(label, font=font))
        except (AttributeError, TypeError):  # very old Pillow
            tw = 9 * len(label)
        ly = max(0, y0 - 17)
        draw.rectangle((x0, ly, x0 + tw + 4, ly + 16), fill=(255, 0, 0))
        draw.text((x0 + 2, ly), label, fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _calibrate_display(
    ssh: asyncssh.SSHClientConnection,
) -> DisplayCalibration:
    """Probe the VM display to compute scale factors.

    Compares xdotool's coordinate space with the actual screenshot pixel
    dimensions.  Falls back to 1:1 scale on any failure (non-fatal).
    """
    fallback = DisplayCalibration(0, 0, 0, 0, 1.0, 1.0)
    display = shlex.quote(VM_DISPLAY)
    try:
        # 1) xdotool coordinate space
        geo = await run_vm_cmd(ssh, f"DISPLAY={display} xdotool getdisplaygeometry")
        xw, xh = (int(v) for v in geo.split())

        # 2) Take a calibration screenshot and read its pixel dimensions
        cal_path = "/tmp/mcp-calibrate.png"
        await run_vm_cmd(
            ssh,
            f"DISPLAY={display} scrot {shlex.quote(cal_path)}",
        )
        file_info = await run_vm_cmd(ssh, f"file {shlex.quote(cal_path)}")
        await run_vm_cmd(ssh, f"rm -f {shlex.quote(cal_path)}")

        # Parse "PNG image data, 1920 x 1080, ..." from `file` output
        m = re.search(r"(\d+)\s*x\s*(\d+)", file_info)
        if not m:
            print(
                "[calibration] Could not parse screenshot "
                f"dimensions from: {file_info}",
                file=sys.stderr,
            )
            return fallback
        sw, sh = int(m.group(1)), int(m.group(2))

        scale_x = xw / sw
        scale_y = xh / sh
        cal = DisplayCalibration(xw, xh, sw, sh, scale_x, scale_y)

        if scale_x == 1.0 and scale_y == 1.0:
            print("[calibration] 1:1, no scaling needed", file=sys.stderr)
        else:
            print(
                f"[calibration] xdotool={xw}x{xh}, screenshot={sw}x{sh}, "
                f"scale=({scale_x:.4f}, {scale_y:.4f})",
                file=sys.stderr,
            )
        return cal

    except Exception as exc:
        print(
            f"[calibration] Failed ({exc}), falling back to 1:1 scale",
            file=sys.stderr,
        )
        return fallback


def _scale_input(cal: DisplayCalibration, x: int, y: int) -> tuple[int, int]:
    """Convert screenshot-space coordinates to xdotool-space (multiply)."""
    return round(x * cal.scale_x), round(y * cal.scale_y)


def _scale_output(cal: DisplayCalibration, x: int, y: int) -> tuple[int, int]:
    """Convert xdotool-space coordinates to screenshot-space (divide)."""
    if cal.scale_x == 0 or cal.scale_y == 0:
        return x, y
    return round(x / cal.scale_x), round(y / cal.scale_y)


# ---------- MCP server setup ----------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    ssh = await connect_ssh()
    calibration = await _calibrate_display(ssh)
    try:
        yield AppContext(ssh=ssh, calibration=calibration)
    finally:
        ssh.close()
        await ssh.wait_closed()


mcp = FastMCP("QemuVMControl", lifespan=lifespan)

# ---------- Tools: mouse / keyboard / wait ----------


@mcp.tool()
async def move_mouse(
    x: int,
    y: int,
    mode: str = "absolute",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Move the mouse cursor.

    Args:
        x: X coordinate
        y: Y coordinate
        mode: "absolute" or "relative"

    Best Practice: Prefer keyboard shortcuts over mouse operations for reliability,
    especially in nested environments (Citrix, VMs). Mouse movements work but
    keyboard navigation is more consistent.
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration
    x, y = int(x), int(y)
    sx, sy = _scale_input(cal, x, y)
    display = shlex.quote(VM_DISPLAY)
    cmd = _move_cmd(display, sx, sy, mode)

    await run_vm_cmd(ssh, cmd)
    result = f"Mouse moved to ({x}, {y}) [{mode}]"
    _log_tool_call(ctx, "move_mouse", {"x": x, "y": y, "mode": mode})
    return result


def _parse_frame_extents(xprop_output: str) -> tuple[int, int, int, int]:
    """Parse _NET_FRAME_EXTENTS from xprop output.

    Returns (left, right, top, bottom). Defaults to (0, 0, 0, 0) if
    the property is missing (e.g. undecorated windows).
    """
    for line in xprop_output.splitlines():
        if "_NET_FRAME_EXTENTS" in line and "=" in line:
            _, _, values = line.partition("=")
            parts = [int(v.strip()) for v in values.split(",")]
            if len(parts) == 4:
                return (parts[0], parts[1], parts[2], parts[3])
    return (0, 0, 0, 0)


async def _get_active_window_geometry(
    ssh: asyncssh.SSHClientConnection,
) -> dict:
    """Get geometry, name, and frame extents of the active window.

    Returns a dict with keys: window_id, name, x, y, width, height,
    frame_left, frame_right, frame_top, frame_bottom, client_x, client_y.
    """
    display = shlex.quote(VM_DISPLAY)
    cmd = (
        f"WID=$(DISPLAY={display} xdotool getactivewindow) && "
        f"DISPLAY={display} xdotool getwindowgeometry --shell $WID && "
        f'echo "---NAME---" && '
        f"DISPLAY={display} xdotool getwindowname $WID && "
        f'echo "---FRAME---" && '
        f"xprop -display {display} -id $WID _NET_FRAME_EXTENTS 2>/dev/null || true"
    )
    output = await run_vm_cmd(ssh, cmd)

    # Parse getwindowgeometry --shell output (WINDOW=..., X=..., Y=..., etc.)
    geo: dict = {}
    name_section = False
    frame_section = False
    name_lines: list[str] = []
    frame_lines: list[str] = []

    for line in output.splitlines():
        if line.strip() == "---NAME---":
            name_section = True
            frame_section = False
            continue
        if line.strip() == "---FRAME---":
            name_section = False
            frame_section = True
            continue
        if frame_section:
            frame_lines.append(line)
        elif name_section:
            name_lines.append(line)
        elif "=" in line:
            key, _, val = line.partition("=")
            geo[key.strip()] = val.strip()

    window_id = int(geo.get("WINDOW", "0"))
    x = int(geo.get("X", "0"))
    y = int(geo.get("Y", "0"))
    width = int(geo.get("WIDTH", "0"))
    height = int(geo.get("HEIGHT", "0"))
    name = "\n".join(name_lines).strip()

    fl, fr, ft, fb = _parse_frame_extents("\n".join(frame_lines))

    return {
        "window_id": window_id,
        "name": name,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "frame_left": fl,
        "frame_right": fr,
        "frame_top": ft,
        "frame_bottom": fb,
        "client_x": x + fl,
        "client_y": y + ft,
    }


@mcp.tool()
async def get_active_window_info(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Get geometry and frame information about the currently focused window.

    Returns structured data about the active window including its position,
    size, decoration (frame) extents, and the computed client-area origin.
    Use this to understand where the window content starts on screen.

    Returned fields:
    - window_id, name: X11 window ID and title
    - x, y: top-left of the window frame on screen
    - width, height: window client-area dimensions
    - frame_left/right/top/bottom: decoration extents (CSD/SSD title bar, borders)
    - client_x, client_y: top-left of the content area in screen coords
      (computed as x + frame_left, y + frame_top)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration
    info = await _get_active_window_geometry(ssh)
    _log_tool_call(ctx, "get_active_window_info", {}, info["name"])

    # Convert xdotool-space coordinates to screenshot-space for the caller
    pos_x, pos_y = _scale_output(cal, info["x"], info["y"])
    w, h = _scale_output(cal, info["width"], info["height"])
    fl, fr = _scale_output(cal, info["frame_left"], info["frame_right"])
    ft, fb = _scale_output(cal, info["frame_top"], info["frame_bottom"])
    cx, cy = _scale_output(cal, info["client_x"], info["client_y"])

    lines = [
        f"Window ID: {info['window_id']}",
        f"Name: {info['name']}",
        f"Position: ({pos_x}, {pos_y})",
        f"Size: {w}x{h}",
        f"Frame extents: left={fl}, right={fr}, "
        f"top={ft}, bottom={fb}",
        f"Client area origin: ({cx}, {cy})",
    ]
    return "\n".join(lines)


@mcp.tool()
async def click_in_window(
    x: int,
    y: int,
    button: str = "left",
    count: int = 1,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Click at coordinates relative to the active window's client area.

    The server internally resolves the active window's position and frame
    (title bar / CSD) extents, then translates the given (x, y) to absolute
    screen coordinates before clicking. This eliminates all CSD/frame offset
    math from the caller — just pass the offset within the window content
    (e.g. from CSS coordinates or screenshot pixel measurements).

    Args:
        x: X offset within the window content area (0 = left edge)
        y: Y offset within the window content area (0 = top edge, below title bar)
        button: left/right/middle
        count: Number of clicks (1 for single, 2 for double)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration

    x, y, count = int(x), int(y), int(count)
    sx, sy = _scale_input(cal, x, y)

    info = await _get_active_window_geometry(ssh)
    screen_x = info["client_x"] + sx
    screen_y = info["client_y"] + sy

    display = shlex.quote(VM_DISPLAY)
    cmd = (
        f"{_move_cmd(display, screen_x, screen_y, 'absolute')} && "
        f"{_click_cmd(display, button, count)}"
    )
    await run_vm_cmd(ssh, cmd)

    result = (
        f"Clicked {button} x{count} at window-relative ({x}, {y}) "
        f"→ screen ({screen_x}, {screen_y})"
    )
    _log_tool_call(
        ctx,
        "click_in_window",
        {"x": x, "y": y, "button": button, "count": count},
        result,
    )
    return result


@mcp.tool()
async def click(
    button: str = "left",
    count: int = 1,
    x: int | None = None,
    y: int | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Click a mouse button, optionally moving to an absolute point first.

    Args:
        button: left/right/middle
        count: Number of clicks (1 for single, 2 for double)
        x, y: Optional absolute screen coordinates (screenshot-space). If both
            are given, the cursor moves there and clicks in one operation,
            avoiding the focus/race gap of a separate move_mouse + click. If
            omitted, clicks at the current cursor position.

    ⚠️ WARNING: Mouse clicks do NOT reliably switch focus in nested environments
    (Citrix, remote desktop, high-latency connections). Use keyboard shortcuts
    like Ctrl+Shift+P to explicitly switch focus instead. Always verify with
    take_screenshot() before typing after a click.
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    count = int(count)
    display = shlex.quote(VM_DISPLAY)

    if x is not None and y is not None:
        sx, sy = _scale_input(app_ctx.calibration, int(x), int(y))
        cmd = _click_at_cmd(display, sx, sy, button, count)
        result = f"Clicked {button} x{count} at ({int(x)}, {int(y)})"
        params = {"button": button, "count": count, "x": int(x), "y": int(y)}
    else:
        cmd = _click_cmd(display, button, count)
        result = f"Clicked {button} x{count}"
        params = {"button": button, "count": count}

    await run_vm_cmd(ssh, cmd)
    _log_tool_call(ctx, "click", params)
    return result


def _format_matches(query: str, matches: list[dict]) -> str:
    lines = [
        f"  {i}. {m['text']!r} center=({m['cx']}, {m['cy']}) "
        f"box=({m['left']},{m['top']},{m['width']}x{m['height']}) conf={m['conf']:.0f}"
        for i, m in enumerate(matches)
    ]
    return f"Found {len(matches)} match(es) for {query!r}:\n" + "\n".join(lines)


@mcp.tool()
async def find_text(
    query: str,
    min_conf: float = 40.0,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Locate on-screen text by OCR — returns exact pixel coordinates, no guessing.

    Captures the current screen, OCRs it on the host (tesseract), and returns
    every match for *query* (case-insensitive substring; multi-word queries like
    "Save As" are matched across adjacent words) with its center and box in
    screenshot-space. Pair with click_text() to click one, or pass the center to
    click(x, y).

    Use this instead of eyeballing coordinates from a screenshot — it is exact.
    Works on any visible text, including nested Citrix/web where accessibility
    APIs can't reach. Does not find icons/unlabeled controls.

    Args:
        query: Visible text to find (e.g. "Submit", "File", "Save As")
        min_conf: Minimum OCR confidence 0-100 (default 40)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    png = await _grab_png(app_ctx.ssh)
    words = await asyncio.to_thread(_ocr_words, png)
    matches = _match_text_boxes(words, query, min_conf)
    _log_tool_call(ctx, "find_text", {"query": query}, f"{len(matches)} matches")
    if not matches:
        return f"No text matching {query!r} found (OCR'd {len(words)} words)."
    return _format_matches(query, matches)


@mcp.tool()
async def click_text(
    query: str,
    index: int = 0,
    button: str = "left",
    count: int = 1,
    min_conf: float = 40.0,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Find on-screen text by OCR and click its center — precise, no coordinate math.

    The reliable way to click a labeled button/menu/link: name the visible text
    and the host resolves it to exact pixels (full-res screenshot-space → scaled →
    clicked). Far more accurate than estimating coordinates from a screenshot.

    If several places match, they are ordered top-to-bottom, left-to-right; use
    `index` to pick one (call find_text() first to see them). Errors without
    clicking if nothing matches.

    Args:
        query: Visible text to click (e.g. "OK", "File", "Sign in")
        index: Which match to click when there are several (default 0 = first)
        button: left/right/middle
        count: Click count (2 = double-click)
        min_conf: Minimum OCR confidence 0-100 (default 40)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    png = await _grab_png(ssh)
    words = await asyncio.to_thread(_ocr_words, png)
    matches = _match_text_boxes(words, query, min_conf)

    if not matches:
        _log_error(ctx, "click_text", f"no match for {query!r}")
        return f"No text matching {query!r} found; nothing clicked."
    if index < 0 or index >= len(matches):
        return (
            f"index {index} out of range; {len(matches)} match(es) for {query!r}.\n"
            + _format_matches(query, matches)
        )

    m = matches[index]
    sx, sy = _scale_input(app_ctx.calibration, m["cx"], m["cy"])
    display = shlex.quote(VM_DISPLAY)
    await run_vm_cmd(ssh, _click_at_cmd(display, sx, sy, button, int(count)))

    extra = f" (+{len(matches) - 1} other match(es))" if len(matches) > 1 else ""
    result = f"Clicked {m['text']!r} at ({m['cx']}, {m['cy']}){extra}"
    _log_tool_call(ctx, "click_text", {"query": query, "index": index}, result)
    return result


@mcp.tool()
async def zoom(
    x: int,
    y: int,
    width: int = 400,
    height: int = 300,
    scale: float = 3.0,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Magnify a region of the screen so small/low-contrast detail is legible.

    Captures the screen, crops a `width`×`height` box centered on (x, y) — in
    screenshot-space coordinates, clamped to the screen — and upscales it by
    `scale`. Saves the result as a screenshot resource you can view, and
    remembers the crop so click_zoomed(zx, zy) maps points in the zoomed image
    back to exact full-screen pixels — no coordinate math needed.

    Use this to read a tiny label/icon you can't resolve in the full screenshot,
    then click_zoomed() the spot you want. Requires an active project.

    Args:
        x, y: Center of the region to magnify (screenshot-space)
        width, height: Crop size before magnification (default 400×300)
        scale: Magnification factor (default 3×)

    Returns:
        The zoomed image resource URI plus the crop mapping.
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    project = _get_project(ctx)  # type: ignore[arg-type]

    png = await _grab_png(app_ctx.ssh)
    zbytes, region = await asyncio.to_thread(
        _crop_zoom, png, x, y, width, height, scale
    )
    app_ctx.last_zoom = region

    sid = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
    project.screenshot_path(sid).write_bytes(zbytes)
    project._log(f"Zoom captured: {sid} ({region.left},{region.top} x{region.scale})")

    zoom_w = int(region.crop_w * region.scale)
    zoom_h = int(region.crop_h * region.scale)
    _log_tool_call(ctx, "zoom", {"x": x, "y": y, "scale": scale})
    return (
        f"Zoomed: origin=({region.left}, {region.top}) "
        f"crop={region.crop_w}x{region.crop_h} scale={region.scale} "
        f"-> image {zoom_w}x{zoom_h}\n"
        f"Resource URI: vm://screenshot/{sid}\n"
        f"Click a point in this image with click_zoomed(zx, zy)."
    )


@mcp.tool()
async def click_zoomed(
    zx: int,
    zy: int,
    button: str = "left",
    count: int = 1,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Click a point given in the most recent zoom()'s image coordinates.

    The server maps (zx, zy) from the zoomed image back to the exact full-screen
    pixel and clicks there. Call zoom() first; coordinates are read from the
    magnified image it returned.

    Args:
        zx, zy: Point within the last zoomed image (its pixel coordinates)
        button: left/right/middle
        count: Click count (2 = double-click)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    region = app_ctx.last_zoom
    if region is None:
        return "Error: no prior zoom(); call zoom() first."

    fx, fy = _zoom_map(region, int(zx), int(zy))
    sx, sy = _scale_input(app_ctx.calibration, fx, fy)
    display = shlex.quote(VM_DISPLAY)
    await run_vm_cmd(app_ctx.ssh, _click_at_cmd(display, sx, sy, button, int(count)))

    result = f"Clicked zoomed ({int(zx)}, {int(zy)}) -> screen ({fx}, {fy})"
    _log_tool_call(ctx, "click_zoomed", {"zx": zx, "zy": zy}, result)
    return result


@mcp.tool()
async def mark_screen(
    min_conf: float = 50.0,
    max_marks: int = 80,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Overlay numbered marks on detected on-screen text — then pick by NUMBER.

    Captures the screen, OCRs it, draws a numbered red box on each text element,
    and saves the annotated image as a resource. View it, then click the element
    you want with click_mark(n) — you choose an index instead of estimating
    coordinates, which is far more reliable for dense/ambiguous UIs.

    The text legend (id -> text -> center) is also returned. Raise min_conf or
    lower max_marks if the result is too cluttered. Requires an active project.

    Args:
        min_conf: Minimum OCR confidence 0-100 to mark an element (default 50)
        max_marks: Cap on number of marks (default 80)

    Returns:
        Annotated image resource URI plus the numbered legend.
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    project = _get_project(ctx)  # type: ignore[arg-type]

    png = await _grab_png(app_ctx.ssh)
    words = await asyncio.to_thread(_ocr_words, png)
    marks = _build_marks(words, min_conf, max_marks)
    if not marks:
        return "No text detected to mark (try lowering min_conf)."

    annotated = await asyncio.to_thread(_render_marks, png, marks)
    app_ctx.last_marks = marks

    sid = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
    project.screenshot_path(sid).write_bytes(annotated)
    project._log(f"Marked {len(marks)} elements: {sid}")

    legend = "\n".join(
        f"  [{m['id']}] {m['text']!r} ({m['cx']}, {m['cy']})" for m in marks
    )
    _log_tool_call(ctx, "mark_screen", {"count": len(marks)})
    return (
        f"Marked {len(marks)} elements.\n"
        f"Resource URI: vm://screenshot/{sid}\n"
        f"{legend}\n"
        f"Click one with click_mark(n)."
    )


@mcp.tool()
async def click_mark(
    n: int,
    button: str = "left",
    count: int = 1,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Click the element labeled `n` from the most recent mark_screen().

    Args:
        n: The mark number shown in the annotated image / legend
        button: left/right/middle
        count: Click count (2 = double-click)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    marks = app_ctx.last_marks
    if not marks:
        return "Error: no marks; call mark_screen() first."

    mark = next((m for m in marks if m["id"] == int(n)), None)
    if mark is None:
        return f"No mark {n}; valid range is 0..{len(marks) - 1}."

    sx, sy = _scale_input(app_ctx.calibration, mark["cx"], mark["cy"])
    display = shlex.quote(VM_DISPLAY)
    await run_vm_cmd(app_ctx.ssh, _click_at_cmd(display, sx, sy, button, int(count)))

    result = f"Clicked mark [{n}] {mark['text']!r} at ({mark['cx']}, {mark['cy']})"
    _log_tool_call(ctx, "click_mark", {"n": n}, result)
    return result


@mcp.tool()
async def scroll(
    direction: str = "down",
    amount: int = 3,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Scroll the mouse wheel at the current cursor position.

    Move the cursor over the area you want to scroll first (move_mouse) — most
    apps scroll the widget under the pointer.

    Args:
        direction: up / down / left / right
        amount: Number of wheel steps (clamped to >= 1; ~3-5 ≈ one notch burst)
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    display = shlex.quote(VM_DISPLAY)
    cmd = _scroll_cmd(display, direction, amount)
    await run_vm_cmd(ssh, cmd)
    result = f"Scrolled {direction} x{max(1, int(amount))}"
    _log_tool_call(ctx, "scroll", {"direction": direction, "amount": amount})
    return result


@mcp.tool()
async def drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    button: str = "left",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Press at (x1, y1), drag to (x2, y2), and release — a click-and-drag.

    Use for selecting text, moving sliders/scrollbars, dragging files/icons,
    or repositioning windows. Coordinates are absolute screenshot-space points.

    Args:
        x1, y1: Start point (button pressed here)
        x2, y2: End point (button released here)
        button: left/right/middle (default left)
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    ssh = app_ctx.ssh
    cal = app_ctx.calibration
    sx1, sy1 = _scale_input(cal, int(x1), int(y1))
    sx2, sy2 = _scale_input(cal, int(x2), int(y2))
    display = shlex.quote(VM_DISPLAY)
    cmd = _drag_cmd(display, sx1, sy1, sx2, sy2, button)
    await run_vm_cmd(ssh, cmd)
    result = f"Dragged {button} from ({int(x1)}, {int(y1)}) to ({int(x2)}, {int(y2)})"
    _log_tool_call(
        ctx,
        "drag",
        {"x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2), "button": button},
    )
    return result


@mcp.tool()
async def type_text(
    text: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Type literal text into the VM.

    ⚠️ CRITICAL: ALWAYS take_screenshot() first to verify focus before typing!
    Typing into the wrong window (e.g., Vim editor instead of terminal) will
    corrupt files. Check for visual indicators:
    - Cursor blinking in terminal = safe to type commands
    - Vim mode in status bar (INSERT/NORMAL) = DO NOT type commands
    - No cursor visible = STOP and screenshot first

    The text is typed with 10ms delay between characters for reliability.
    Newlines are sent as explicit Return key presses (not a literal LF), so
    multi-line text produces real line breaks in both terminals and rich editors.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    # Text goes to stdin via --file -, never touches the shell
    display = shlex.quote(VM_DISPLAY)
    await _run_type(ssh, display, text)
    # Mask sensitive text in logs (only show length)
    log_text = text if len(text) <= 20 else f"{text[:10]}...({len(text)} chars)"
    _log_tool_call(ctx, "type_text", {"text": log_text})
    return f"Typed {len(text)} characters"


@mcp.tool()
async def set_clipboard(
    text: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Load the VM clipboard with text (first VM layer's X clipboard).

    Faster and more reliable than type_text() for large ASCII blobs. Follow
    with paste() or press_keys(["Ctrl", "v"]) to insert it.

    ⚠️ Clipboard redirection is often disabled across nested boundaries
    (Citrix/RDP) — paste may not cross into inner sessions. See README
    Known Issues #3.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    display = shlex.quote(VM_DISPLAY)
    # Text goes to xclip via stdin, never the shell
    await ssh.run(_clipboard_set_cmd(display), input=text, check=True)
    _log_tool_call(ctx, "set_clipboard", {"chars": len(text)})
    return f"Clipboard set ({len(text)} chars)"


@mcp.tool()
async def paste(
    text: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Paste with Ctrl+V, optionally setting the clipboard first.

    If *text* is given, the clipboard is loaded with it and then pasted in one
    call — a fast alternative to type_text() for big ASCII payloads. If omitted,
    pastes whatever is already on the clipboard.

    Note: terminals usually need Ctrl+Shift+V — use press_keys(["Ctrl",
    "Shift", "v"]) there instead. Clipboard may not cross nested boundaries
    (see README Known Issues #3).
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    display = shlex.quote(VM_DISPLAY)
    if text is not None:
        await ssh.run(_clipboard_set_cmd(display), input=text, check=True)
    await run_vm_cmd(ssh, _keys_cmd(display, ["ctrl", "v"]))
    set_note = f" ({len(text)} chars set)" if text is not None else ""
    _log_tool_call(ctx, "paste", {"chars": len(text) if text is not None else 0})
    return f"Pasted{set_note}"


@mcp.tool()
async def press_keys(
    keys: list[str],
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Press key combination, e.g. ["Ctrl", "L"] or ["Alt", "F4"].

    Best Practice: Keyboard shortcuts are MORE RELIABLE than mouse clicks for
    focus switching and navigation, especially in nested environments.
    Examples:
    - VS Code terminal focus: ["Ctrl", "Shift", "p"]
      then type "Terminal: Focus Terminal"
    - Escape from Vim: ["Escape"]
    - Common modifiers: Ctrl, Shift, Alt, Meta

    Always follow with wait() and take_screenshot() to verify the action completed.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    # xdotool uses 'ctrl+l', 'alt+F4', etc. (key names validated in _keys_cmd)
    display = shlex.quote(VM_DISPLAY)
    cmd = _keys_cmd(display, keys)
    await run_vm_cmd(ssh, cmd)
    result = f"Pressed keys: {keys}"
    _log_tool_call(ctx, "press_keys", {"keys": keys})
    return result


@mcp.tool()
async def key_down(
    keys: list[str],
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Hold down a key or modifier combo WITHOUT releasing it.

    Pair with key_up() to bracket other actions — e.g. Shift-click to extend a
    selection, or hold Ctrl while clicking several items:
        key_down(["shift"]) → click(...) → key_up(["shift"])

    ⚠️ Always release with key_up() — a stuck modifier corrupts later input.
    Prefer press_keys() for a normal one-shot combo.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    display = shlex.quote(VM_DISPLAY)
    await run_vm_cmd(ssh, _key_event_cmd(display, keys, "keydown"))
    _log_tool_call(ctx, "key_down", {"keys": keys})
    return f"Holding keys: {keys}"


@mcp.tool()
async def key_up(
    keys: list[str],
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Release a key or modifier combo previously held with key_down().
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    display = shlex.quote(VM_DISPLAY)
    await run_vm_cmd(ssh, _key_event_cmd(display, keys, "keyup"))
    _log_tool_call(ctx, "key_up", {"keys": keys})
    return f"Released keys: {keys}"


async def _activate_window(
    ssh: asyncssh.SSHClientConnection,
    *,
    title: str | None = None,
    window_id: int | None = None,
) -> str:
    """Activate (focus + raise) a window by title substring or window id.

    Returns the activated window's id and name. Raises ValueError if neither
    selector is given or no window matches the title. The title is treated as
    an xdotool --name regex and is shlex-quoted, so it can't reach the shell.
    """
    display = shlex.quote(VM_DISPLAY)
    if window_id is not None:
        wid = str(int(window_id))
    elif title:
        # `|| true` so a no-match (xdotool exit 1) doesn't raise in run_vm_cmd
        out = await run_vm_cmd(
            ssh,
            f"DISPLAY={display} xdotool search --name {shlex.quote(title)} || true",
        )
        matches = out.split()
        if not matches:
            raise ValueError(f"no window matching title {title!r}")
        wid = matches[0]
    else:
        raise ValueError("provide either title or window_id")

    name = await run_vm_cmd(
        ssh,
        f"DISPLAY={display} xdotool windowactivate --sync {wid} && "
        f"DISPLAY={display} xdotool getwindowname {wid}",
    )
    return f"activated window {wid}: {name}"


@mcp.tool()
async def activate_window(
    title: str | None = None,
    window_id: int | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Focus and raise a window by title (substring/regex) or X11 window id.

    More reliable than clicking to switch focus (see the click warning). Use
    get_active_window_info() or take_screenshot() to read window titles first.

    Args:
        title: Match against window names (xdotool --name regex). First match wins.
        window_id: Exact X11 window id (from get_active_window_info).

    Returns:
        The activated window's id and name, or an error if nothing matched.
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]
    try:
        result = await _activate_window(ssh, title=title, window_id=window_id)
    except ValueError as e:
        _log_error(ctx, "activate_window", str(e))
        return f"Error: {e}"
    _log_tool_call(
        ctx, "activate_window", {"title": title, "window_id": window_id}, result
    )
    return result


@mcp.tool()
async def wait(
    seconds: float,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Sleep/pause for specified seconds.

    ⚠️ REQUIRED: Always wait between actions in nested/remote environments!
    Recommended wait times:
    - After opening Command Palette: 0.5s
    - After typing search text: 0.3s
    - After pressing Enter/Return: 0.5-1.0s
    - After command execution: 1.0-2.0s (depends on command)
    - After window/focus switch: 0.5s

    Never rapid-fire actions - they may arrive out of order or fail silently
    due to Citrix/VM latency.
    """
    await asyncio.sleep(seconds)
    _log_tool_call(ctx, "wait", {"seconds": seconds})
    return f"Waited {seconds} seconds"


# ---------- Batch action handlers ----------
# Each handler runs one batch action and returns a short human summary. The
# ACTION_HANDLERS dict below is the single source of truth for which actions
# run_actions() supports — add an action by writing a handler and one entry.
# Signature: async (app_ctx, display_q, action_dict) -> str


async def _act_press_keys(app: "AppContext", display: str, a: dict) -> str:
    keys = a.get("keys", [])
    await run_vm_cmd(app.ssh, _keys_cmd(display, keys))
    return f"press_keys {keys}"


async def _act_type_text(app: "AppContext", display: str, a: dict) -> str:
    text = a.get("text", "")
    await _run_type(app.ssh, display, text)
    return f"type_text ({len(text)} chars)"


async def _act_click(app: "AppContext", display: str, a: dict) -> str:
    button = a.get("button", "left")
    count = int(a.get("count", 1))
    x, y = a.get("x"), a.get("y")
    if x is not None and y is not None:
        sx, sy = _scale_input(app.calibration, int(x), int(y))
        await run_vm_cmd(app.ssh, _click_at_cmd(display, sx, sy, button, count))
        return f"click {button} x{count} at ({int(x)}, {int(y)})"
    await run_vm_cmd(app.ssh, _click_cmd(display, button, count))
    return f"click {button} x{count}"


async def _act_move_mouse(app: "AppContext", display: str, a: dict) -> str:
    x = int(a.get("x", 0))
    y = int(a.get("y", 0))
    mode = a.get("mode", "absolute")
    sx, sy = _scale_input(app.calibration, x, y)
    await run_vm_cmd(app.ssh, _move_cmd(display, sx, sy, mode))
    return f"move_mouse ({x}, {y}) [{mode}]"


async def _act_scroll(app: "AppContext", display: str, a: dict) -> str:
    direction = a.get("direction", "down")
    amount = int(a.get("amount", 3))
    await run_vm_cmd(app.ssh, _scroll_cmd(display, direction, amount))
    return f"scroll {direction} x{max(1, amount)}"


async def _act_drag(app: "AppContext", display: str, a: dict) -> str:
    x1, y1 = int(a.get("x1", 0)), int(a.get("y1", 0))
    x2, y2 = int(a.get("x2", 0)), int(a.get("y2", 0))
    button = a.get("button", "left")
    sx1, sy1 = _scale_input(app.calibration, x1, y1)
    sx2, sy2 = _scale_input(app.calibration, x2, y2)
    await run_vm_cmd(app.ssh, _drag_cmd(display, sx1, sy1, sx2, sy2, button))
    return f"drag {button} ({x1}, {y1}) -> ({x2}, {y2})"


async def _act_key_down(app: "AppContext", display: str, a: dict) -> str:
    keys = a.get("keys", [])
    await run_vm_cmd(app.ssh, _key_event_cmd(display, keys, "keydown"))
    return f"key_down {keys}"


async def _act_key_up(app: "AppContext", display: str, a: dict) -> str:
    keys = a.get("keys", [])
    await run_vm_cmd(app.ssh, _key_event_cmd(display, keys, "keyup"))
    return f"key_up {keys}"


async def _act_activate_window(app: "AppContext", display: str, a: dict) -> str:
    return await _activate_window(
        app.ssh, title=a.get("title"), window_id=a.get("window_id")
    )


async def _act_screenshot(app: "AppContext", display: str, a: dict) -> str:
    _, sid = await _capture_screenshot(app)
    return f"screenshot -> vm://screenshot/{sid}"


async def _act_set_clipboard(app: "AppContext", display: str, a: dict) -> str:
    text = a.get("text", "")
    await app.ssh.run(_clipboard_set_cmd(display), input=text, check=True)
    return f"set_clipboard ({len(text)} chars)"


async def _act_paste(app: "AppContext", display: str, a: dict) -> str:
    text = a.get("text")
    if text is not None:
        await app.ssh.run(_clipboard_set_cmd(display), input=text, check=True)
    await run_vm_cmd(app.ssh, _keys_cmd(display, ["ctrl", "v"]))
    return "paste" + (f" ({len(text)} chars)" if text is not None else "")


async def _act_click_text(app: "AppContext", display: str, a: dict) -> str:
    query = a.get("query", "")
    index = int(a.get("index", 0))
    png = await _grab_png(app.ssh)
    words = await asyncio.to_thread(_ocr_words, png)
    matches = _match_text_boxes(words, query, float(a.get("min_conf", 40)))
    if not matches:
        raise ValueError(f"no text matching {query!r}")
    if index < 0 or index >= len(matches):
        raise ValueError(f"index {index} out of range ({len(matches)} matches)")
    m = matches[index]
    sx, sy = _scale_input(app.calibration, m["cx"], m["cy"])
    await run_vm_cmd(
        app.ssh,
        _click_at_cmd(display, sx, sy, a.get("button", "left"), int(a.get("count", 1))),
    )
    return f"click_text {query!r} -> ({m['cx']}, {m['cy']})"


async def _act_wait(app: "AppContext", display: str, a: dict) -> str:
    seconds = a.get("seconds", 0.5)
    await asyncio.sleep(seconds)
    return f"wait {seconds}s"


ACTION_HANDLERS = {
    "press_keys": _act_press_keys,
    "type_text": _act_type_text,
    "click": _act_click,
    "move_mouse": _act_move_mouse,
    "scroll": _act_scroll,
    "drag": _act_drag,
    "key_down": _act_key_down,
    "key_up": _act_key_up,
    "activate_window": _act_activate_window,
    "screenshot": _act_screenshot,
    "set_clipboard": _act_set_clipboard,
    "paste": _act_paste,
    "click_text": _act_click_text,
    "wait": _act_wait,
}


@mcp.tool()
async def run_actions(
    actions: list[dict],
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Execute a sequence of UI actions in one call to reduce latency.

    🚀 RECOMMENDED for nested/remote environments (Citrix, VMs)!
    Benefits:
    - Reduces round-trip latency (the whole batch runs over one SSH call)
    - Critical for high-latency environments
    - Runs sequentially in order; stops on the first error (an unknown action
      name is an error and halts the batch)

    Each action is a dict with an "action" key plus that action's parameters.
    The same parameters as the standalone tools apply.

    Supported actions:
    - {"action": "press_keys", "keys": ["Ctrl", "Shift", "p"]}
    - {"action": "type_text", "text": "hello"}
    - {"action": "click", "button": "left", "count": 1}  # optional "x"/"y"
    - {"action": "move_mouse", "x": 100, "y": 200, "mode": "absolute"}
    - {"action": "scroll", "direction": "down", "amount": 3}
    - {"action": "drag", "x1": 100, "y1": 200, "x2": 400, "y2": 200}
    - {"action": "key_down", "keys": ["shift"]}
    - {"action": "key_up", "keys": ["shift"]}
    - {"action": "activate_window", "title": "Mousepad"}  # or "window_id"
    - {"action": "screenshot"}
    - {"action": "set_clipboard", "text": "hello"}
    - {"action": "paste", "text": "hello"}  # "text" optional
    - {"action": "click_text", "query": "Submit"}  # OCR-locate + click
    - {"action": "wait", "seconds": 0.5}

    Example - focus a window, paste text, and capture the result:
    [
        {"action": "activate_window", "title": "Mousepad"},
        {"action": "wait", "seconds": 0.3},
        {"action": "paste", "text": "Hello from the clipboard"},
        {"action": "wait", "seconds": 0.3},
        {"action": "screenshot"}
    ]

    Example - hold Shift to select to end of line (key_down/key_up):
    [
        {"action": "press_keys", "keys": ["Home"]},
        {"action": "key_down", "keys": ["shift"]},
        {"action": "press_keys", "keys": ["End"]},
        {"action": "key_up", "keys": ["shift"]}
    ]

    Returns:
        Summary of executed actions
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    display = shlex.quote(VM_DISPLAY)  # constant for the whole batch
    results = []

    for i, action_def in enumerate(actions):
        action_type = action_def.get("action")
        handler = ACTION_HANDLERS.get(action_type)

        try:
            if handler is None:
                valid = ", ".join(ACTION_HANDLERS)
                raise ValueError(f"unknown action {action_type!r} (valid: {valid})")
            summary = await handler(app_ctx, display, action_def)
            results.append(f"{i + 1}. {summary}")

        except Exception as e:
            results.append(f"{i + 1}. ERROR in {action_type}: {str(e)}")
            _log_error(ctx, "run_actions", f"Action {i + 1} ({action_type}): {str(e)}")
            break  # Stop on error

    _log_tool_call(
        ctx, "run_actions", {"count": len(actions)}, f"executed {len(results)} actions"
    )
    return f"Executed {len(results)} actions:\n" + "\n".join(results)


# ---------- SSH Tools ----------


@mcp.tool()
async def ssh_execute(
    command: str,
    as_desktop_user: bool = False,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Execute an arbitrary shell command on the VM via SSH.

    ⚠️ CRITICAL LIMITATION: SSH only reaches the FIRST VM LAYER!

    If you're working with nested environments (VM → Citrix → Windows → VS Code),
    SSH commands do NOT reach those inner layers. For nested environments, use
    UI automation tools (type_text, press_keys, run_actions) instead.

    SSH is 20-40x faster than UI automation for VM tasks, so use it when possible
    for the first VM layer (package installs, file operations, system commands).

    Notes:
    - Commands run with vmrobot user permissions by default
    - Set as_desktop_user=True for commands needing the desktop
      user's context (clipboard, pass, dbus, etc.)
    - Uses persistent SSH connection (no reconnect overhead)
    - Returns stdout, stderr, and exit code
    - Use absolute paths for reliability

    Args:
        command: The shell command to execute on the first VM
        as_desktop_user: Run as the desktop session owner
            (VM_DESKTOP_USER) instead of VM_USER. Useful for
            clipboard, password manager, and dbus operations.

    Returns:
        Command output (stdout and stderr combined with exit code)

    Examples:
        - System info: "uname -a"
        - Install packages: "sudo pacman -Sy package-name"
        - Clipboard: as_desktop_user=True, "xclip -selection clipboard -o"
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        if as_desktop_user and VM_DESKTOP_USER:
            command = (
                f"sudo -u {shlex.quote(VM_DESKTOP_USER)} {command}"
            )
        result = await ssh.run(command, check=False)
        output_parts = []

        if result.stdout:
            output_parts.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"EXIT CODE: {result.returncode}")
            _log_tool_call(
                ctx,
                "ssh_execute",
                {"command": command},
                f"exit_code={result.returncode}",
            )
            _log_error(
                ctx, "ssh_execute", f"Command failed with exit code {result.returncode}"
            )
        else:
            _log_tool_call(ctx, "ssh_execute", {"command": command}, "success")

        return (
            "\n\n".join(output_parts)
            if output_parts
            else "Command completed (no output)"
        )
    except Exception as e:
        _log_error(ctx, "ssh_execute", str(e))
        return f"Error executing command: {str(e)}"


@mcp.tool()
async def ssh_upload(
    local_path: str,
    remote_path: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Upload a file from the host to the VM via SFTP.

    ⚠️ Note: This uploads to the first VM layer only. For nested environments,
    files must be transferred to inner layers using alternative methods.

    Notes:
    - Uses SFTP over the persistent SSH connection
    - Destination directory must exist (create with ssh_execute if needed)
    - Use absolute paths for reliability
    - Supports all file types (text, binary, archives)

    Best Practices:
    - Verify local file exists before uploading
    - Create destination directory first: ssh_execute("mkdir -p /path/to/dir")
    - For scripts, set permissions after upload:
      ssh_execute("chmod +x /path/to/script.sh")
    - Use tar/zip for multiple files

    Args:
        local_path: Path to the local file to upload (absolute or relative)
        remote_path: Destination path on the VM (first layer, use absolute path)

    Returns:
        Success/failure message

    Examples:
        - Upload config: local_path="./config.json",
          remote_path="/home/vmrobot/config.json"
        - Upload script: local_path="./deploy.sh", remote_path="/home/vmrobot/deploy.sh"
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        local_file = pathlib.Path(local_path)
        if not local_file.exists():
            _log_error(ctx, "ssh_upload", f"Local file not found: {local_path}")
            return f"Error: Local file not found: {local_path}"

        async with ssh.start_sftp_client() as sftp:
            await sftp.put(str(local_file), remote_path)

        _log_tool_call(
            ctx,
            "ssh_upload",
            {"local_path": local_path, "remote_path": remote_path},
            "success",
        )
        return f"Successfully uploaded {local_path} to {remote_path}"
    except Exception as e:
        _log_error(ctx, "ssh_upload", str(e))
        return f"Error uploading file: {str(e)}"


@mcp.tool()
async def ssh_download(
    remote_path: str,
    local_path: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Download a file from the VM to the host via SFTP.

    ⚠️ Note: This downloads from the first VM layer only. For nested environments,
    files must be transferred from inner layers using alternative methods.

    Notes:
    - Uses SFTP over the persistent SSH connection
    - Automatically creates parent directories on host if needed
    - Use absolute paths for reliability
    - Supports all file types (text, binary, archives, logs)

    Best Practices:
    - Verify remote file exists first:
      ssh_execute("test -f /path/to/file && echo exists")
    - Use absolute paths on both sides
    - For logs, download before they rotate
    - Consider compressing large files first on VM

    Args:
        remote_path: Path to the file on the VM (first layer, use absolute path)
        local_path: Destination path on the host (parent dirs created automatically)

    Returns:
        Success/failure message

    Examples:
        - Download logs: remote_path="/var/log/app.log", local_path="./logs/app.log"
        - Download backup:
          remote_path="/home/vmrobot/backup.tar.gz",
          local_path="./backups/backup.tar.gz"
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        local_file = pathlib.Path(local_path)
        # Create parent directories if needed
        local_file.parent.mkdir(parents=True, exist_ok=True)

        async with ssh.start_sftp_client() as sftp:
            await sftp.get(remote_path, str(local_file))

        _log_tool_call(
            ctx,
            "ssh_download",
            {"remote_path": remote_path, "local_path": local_path},
            "success",
        )
        return f"Successfully downloaded {remote_path} to {local_path}"
    except Exception as e:
        _log_error(ctx, "ssh_download", str(e))
        return f"Error downloading file: {str(e)}"


@mcp.tool()
async def ssh_connection_info(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Get information about the current SSH connection to the VM.

    Use this to:
    - Verify SSH connection is active
    - Check connection parameters (host, port, user)
    - Troubleshoot connection issues
    - Confirm which VM you're connected to

    The connection is persistent across all SSH tool calls, so no reconnect
    overhead between operations.

    Returns:
        Connection details including host, port, user, display, and connection status
    """
    ssh = ctx.request_context.lifespan_context.ssh  # type: ignore[union-attr]

    try:
        # Try to execute a simple command to verify connection is alive
        await ssh.run("echo 'connection_test'", check=True, timeout=5)
        status = "Connected"
    except Exception as e:
        status = f"Connection issue: {str(e)}"
        _log_error(ctx, "ssh_connection_info", str(e))

    _log_tool_call(ctx, "ssh_connection_info", {}, status)

    cal = ctx.request_context.lifespan_context.calibration  # type: ignore[union-attr]
    cal_line = (
        f"Display Calibration: xdotool={cal.xdotool_w}x{cal.xdotool_h}, "
        f"screenshot={cal.screenshot_w}x{cal.screenshot_h}, "
        f"scale=({cal.scale_x:.4f}, {cal.scale_y:.4f})"
    )

    info = f"""SSH Connection Information:
Host: {VM_HOST}
Port: {VM_PORT}
User: {VM_USER}
Display: {VM_DISPLAY}
Status: {status}
Identity File: {VM_IDENTITY or "Not specified (using password/agent)"}
{cal_line}"""

    return info


@mcp.tool()
async def display_calibration_info(
    recalibrate: bool = False,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Show current display calibration data (scale factors between xdotool
    and screenshot coordinate spaces).

    Set recalibrate=True to re-probe the display (useful after xrandr
    resolution changes mid-session).

    Returns:
        Calibration details including xdotool geometry, screenshot
        dimensions, and computed scale factors.
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]

    if recalibrate:
        app_ctx.calibration = await _calibrate_display(app_ctx.ssh)

    cal = app_ctx.calibration
    _log_tool_call(ctx, "display_calibration_info", {"recalibrate": recalibrate})

    no_scale = cal.scale_x == 1.0 and cal.scale_y == 1.0
    scaling = "1:1 (no scaling)" if no_scale else "active"
    lines = [
        f"Display Calibration ({scaling}):",
        f"  xdotool geometry: {cal.xdotool_w}x{cal.xdotool_h}",
        f"  Screenshot pixels: {cal.screenshot_w}x{cal.screenshot_h}",
        f"  Scale X: {cal.scale_x:.4f}",
        f"  Scale Y: {cal.scale_y:.4f}",
    ]
    if recalibrate:
        lines.append("  (recalibrated)")
    return "\n".join(lines)


# ---------- Project Tools ----------


def _get_project(ctx: Context[ServerSession, AppContext]) -> Project:
    """Get the current project or raise an error if none is active."""
    project = ctx.request_context.lifespan_context.project  # type: ignore[union-attr]
    if project is None:
        raise ValueError("No project initialized. Call project_init first.")
    return project


def _get_project_optional(
    ctx: Context[ServerSession, AppContext] | None,
) -> Project | None:
    """Get the current project if one exists, otherwise None."""
    if ctx is None:
        return None
    return ctx.request_context.lifespan_context.project  # type: ignore[union-attr]


def _log_tool_call(
    ctx: Context[ServerSession, AppContext] | None,
    tool_name: str,
    params: dict,
    result: str | None = None,
) -> None:
    """Log a tool call to the project log if a project is active."""
    project = _get_project_optional(ctx)
    if project is None:
        return

    # Format parameters, truncating long values
    param_strs = []
    for k, v in params.items():
        v_str = str(v)
        if len(v_str) > 100:
            v_str = v_str[:100] + "..."
        param_strs.append(f"{k}={v_str}")
    params_str = ", ".join(param_strs) if param_strs else ""

    log_msg = f"TOOL: {tool_name}({params_str})"
    if result:
        result_str = result if len(result) <= 200 else result[:200] + "..."
        log_msg += f" -> {result_str}"

    project._log(log_msg)


def _log_error(
    ctx: Context[ServerSession, AppContext] | None, tool_name: str, error: str
) -> None:
    """Log an error to the project log if a project is active."""
    project = _get_project_optional(ctx)
    if project is None:
        return
    project._log(f"ERROR in {tool_name}: {error}", level="ERROR")


@mcp.tool()
async def project_init(
    name: str,
    description: str = "",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Initialize a new project. MUST be called before screenshots and other operations.

    Projects organize all outputs into a timestamped folder with:
    - screenshots/ - All screenshots from take_screenshot()
    - logs/ - Automatic logging of all tool calls
    - results/ - Saved outputs via project_save_result()
    - advice/ - Tips saved via project_save_advice()

    Recommended workflow:
    1. project_init("task-name", "description") - Start here
    2. take_screenshot() - Now available
    3. ... do work ... (all actions auto-logged)
    4. project_save_advice() - Save lessons learned
    5. project_save_result() - Save important outputs

    For continuing work: project_list() → project_load("path")

    Args:
        name: Project name (used in folder name)
        description: Optional project description

    Returns:
        Project information including path
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]

    project = Project.create(name, description)
    app_ctx.project = project

    info = project.get_info()
    return f"""Project initialized:
Name: {info["name"]}
Path: {info["path"]}
Description: {info["description"] or "(none)"}

Folders created:
- screenshots/
- logs/
- results/
- advice/"""


@mcp.tool()
async def project_info(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Get information about the current project.

    Returns:
        Project details and statistics
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    info = project.get_info()

    return f"""Project Information:
Name: {info["name"]}
Path: {info["path"]}
Created: {info["created_at"]}
Description: {info["description"] or "(none)"}

Statistics:
- Screenshots: {info["screenshot_count"]}
- Results: {info["result_count"]}
- Log entries: {info["log_entries"]}"""


@mcp.tool()
async def project_log(
    message: str,
    level: str = "INFO",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Add a log entry to the current project.

    Args:
        message: Log message
        level: Log level (INFO, WARNING, ERROR, DEBUG)

    Returns:
        Confirmation message
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    return project.log(message, level)


@mcp.tool()
async def project_read_logs(
    lines: int = 50,
    level_filter: str = "",
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Read the project log file to see what happened.

    All tool calls are automatically logged with:
    - Tool name and parameters
    - Success/failure results
    - Errors with level=ERROR

    This is useful for debugging issues or reviewing what actions were taken.
    Use level_filter="ERROR" to see only errors.

    Args:
        lines: Number of recent log lines to return (default 50)
        level_filter: Optional filter by level (INFO, WARNING, ERROR, DEBUG)

    Returns:
        Recent log entries
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    log_file = project.path / "logs" / "project.log"

    if not log_file.exists():
        return "No log entries yet."

    all_lines = log_file.read_text().strip().split("\n")

    if level_filter:
        all_lines = [line for line in all_lines if f"[{level_filter.upper()}]" in line]

    recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

    if not recent_lines:
        suffix = f" with level {level_filter}" if level_filter else ""
        return f"No log entries found{suffix}."

    return (
        f"Log entries ({len(recent_lines)} of {len(all_lines)} total):\n\n"
        + "\n".join(recent_lines)
    )


@mcp.tool()
async def project_save_result(
    filename: str,
    content: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Save a result file to the current project's results folder.

    Args:
        filename: Name for the result file
        content: Content to save

    Returns:
        Path to saved file
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    result_path = project.save_result(filename, content)
    return f"Result saved to: {result_path}"


@mcp.tool()
async def project_save_advice(
    title: str,
    content: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Save advice/tips for future LLM sessions working with this project.

    Use this to record lessons learned, environment-specific tips, or
    important information that would help future interactions.

    Args:
        title: Short title for the advice (e.g., "Focus management in Citrix")
        content: Detailed advice content (markdown supported)

    Returns:
        Confirmation with path to saved advice file
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    advice_path = project.save_advice(title, content)
    return f"Advice saved: {title}\nPath: {advice_path}"


@mcp.tool()
async def project_read_advice(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Read all advice/tips saved for this project.

    Returns advice from previous sessions that may help with current tasks.

    Returns:
        All saved advice entries formatted for reading
    """
    project = _get_project(ctx)  # type: ignore[arg-type]
    advice_list = project.get_all_advice()

    if not advice_list:
        return "No advice saved for this project yet."

    output = f"## Advice for project '{project.name}' ({len(advice_list)} entries)\n\n"
    for i, advice in enumerate(advice_list, 1):
        output += f"### {i}. {advice['title']}\n"
        output += f"{advice['content']}\n\n"
        output += f"_Source: {advice['file']}_\n\n---\n\n"

    return output


@mcp.tool()
async def project_list(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    List all existing projects.

    Returns:
        List of projects with their paths and creation dates
    """
    projects = []
    for project_dir in sorted(PROJECTS_DIR.iterdir(), reverse=True):
        if project_dir.is_dir() and (project_dir / "metadata.json").exists():
            try:
                proj = Project.load(project_dir)
                projects.append(f"- {proj.name} ({proj.created_at}): {proj.path}")
            except Exception:
                projects.append(f"- (invalid): {project_dir}")

    if not projects:
        return "No projects found."

    return "Projects:\n" + "\n".join(projects)


@mcp.tool()
async def project_load(
    project_path: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Load an existing project by its path.

    When loading, any saved advice is automatically displayed. READ THIS ADVICE
    BEFORE PROCEEDING - it contains lessons learned from previous sessions that
    will help you avoid common mistakes and work more efficiently.

    Use project_list() to see all available projects.

    Args:
        project_path: Full path to the project folder

    Returns:
        Project information including any saved advice
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]

    path = pathlib.Path(project_path)
    if not path.exists():
        return f"Error: Project path not found: {project_path}"

    try:
        project = Project.load(path)
        app_ctx.project = project
        project._log("Project loaded")

        info = project.get_info()
        output = f"""Project loaded:
Name: {info["name"]}
Path: {info["path"]}
Created: {info["created_at"]}
Screenshots: {info["screenshot_count"]}
Results: {info["result_count"]}"""

        # Include advice if any exists
        advice_list = project.get_all_advice()
        if advice_list:
            output += f"\n\n## ⚠️ ADVICE FOR THIS PROJECT ({len(advice_list)} tips)\n"
            output += "Read these tips from previous sessions before proceeding:\n\n"
            for i, advice in enumerate(advice_list, 1):
                output += f"**{i}. {advice['title']}**\n"
                # Show first 200 chars of content
                content_preview = advice["content"][:200]
                if len(advice["content"]) > 200:
                    content_preview += "..."
                output += f"{content_preview}\n\n"

        return output
    except Exception as e:
        return f"Error loading project: {str(e)}"


# ---------- Screenshot tools + resources ----------


async def _capture_screenshot(app_ctx: AppContext) -> tuple[pathlib.Path, str]:
    """Capture a full-screen screenshot into the active project.

    Runs scrot on the VM, downloads the PNG via SFTP, removes the remote temp
    file, and returns (local_path, screenshot_id). Raises ValueError if no
    project is active. Shared by take_screenshot() and the batch handler.
    """
    project = app_ctx.project
    if project is None:
        raise ValueError("No project initialized. Call project_init first.")
    ssh = app_ctx.ssh

    sid = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
    remote_path = f"/tmp/mcp-screenshot-{sid}.png"
    display = shlex.quote(VM_DISPLAY)
    await run_vm_cmd(ssh, f"DISPLAY={display} scrot {shlex.quote(remote_path)}")

    local_path = project.screenshot_path(sid)
    async with ssh.start_sftp_client() as sftp:
        await sftp.get(remote_path, str(local_path))

    # Remove the remote temp file so screenshots don't accumulate in /tmp.
    await run_vm_cmd(ssh, f"rm -f {shlex.quote(remote_path)}")

    project._log(f"Screenshot captured: {sid}")
    return local_path, sid


@mcp.tool()
async def take_screenshot(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> str:
    """
    Take a full-screen screenshot and save it to the current project.
    Requires an active project (call project_init first).

    ⚠️ CRITICAL BEST PRACTICE: ALWAYS screenshot BEFORE actions!

    Workflow:
    1. take_screenshot() - see current state
    2. Analyze the image - identify focus, window state
    3. Perform actions - with proper waits
    4. take_screenshot() - verify result

    Never skip screenshots to "save time" - blind actions lead to errors that
    waste more time. Screenshots help identify:
    - Which window/application has focus
    - Current Vim mode (if in editor)
    - Whether dialogs are open
    - If commands completed successfully

    Returns:
        Screenshot path and resource URI for viewing
    """
    app_ctx = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    local_path, sid = await _capture_screenshot(app_ctx)
    return f"Screenshot captured: {local_path}\nResource URI: vm://screenshot/{sid}"


# Expose screenshots as resources
@mcp.resource("vm://screenshot/{sid}")
async def get_screenshot(sid: str) -> bytes:
    """
    Return a screenshot by ID as binary data.
    Searches in all project folders.
    """
    # Screenshot IDs are generated as a UTC timestamp ("%Y%m%d-%H%M%S-%f"),
    # i.e. only digits and dashes. Reject anything else so a crafted sid
    # (e.g. "../../etc/passwd") cannot escape the screenshots directory.
    if not re.fullmatch(r"[0-9-]+", sid):
        raise FileNotFoundError(f"No screenshot found for id {sid}")

    # Search in all projects for this screenshot
    for project_dir in PROJECTS_DIR.iterdir():
        if project_dir.is_dir():
            screenshot_path = project_dir / "screenshots" / f"{sid}.png"
            if screenshot_path.exists():
                return screenshot_path.read_bytes()

    raise FileNotFoundError(f"No screenshot found for id {sid}")


# ---------- Entrypoint ----------

if __name__ == "__main__":
    # stdio transport; works with most MCP clients
    mcp.run()
