#!/usr/bin/env python3
"""Interactive DCS World bindings wizard + diff.lua generator for HOTAS on Linux.

TUI mode (default): full-screen terminal wizard. Flow:

    1. game folder comes from --game-dir (remembered in the results file,
       so you only pass it once); the Saved Games folder inside the Proton
       prefix is derived from it
    2. pick the aircraft (installed modules are discovered automatically)
    3. pick a mapping section to (re)bind — or ALL

Commands are harvested from the game files themselves: factory joystick
profiles shipped with each module provide the command hashes, and the
module's default.lua provides the human categories. This means the wizard
works for any installed module without a hardcoded function list.

Bindings are edited in a table: every command of the section is a row
showing its current assignment straight from the results file. Keys:

    arrows  move between commands
    RETURN  (re)bind the selected command — then press the physical
            button / move the axis; after accepting, the cursor moves
            to the next row so you can chain RETURN-capture-RETURN
    I       invert an axis (stored or freshly captured)
    X       clear the binding (the DCS default, if any, comes back)
    ESC     back / cancel / redo

Results are saved after every change, so quitting any time is safe.

Generator mode (--generate): headless; builds one diff.lua per device and
writes them into Saved Games/DCS/Config/Input/<aircraft>/joystick/.
DCS overwrites those files on exit, so generation refuses to run while
the game is running. Existing files are backed up as *.bak first.

The generator also cleans up DCS's per-device defaults (it assigns
pitch/roll/rudder/thrust and fire/weapon-change/cannon to EVERY joystick
device), so a stick and a throttle never fight over the same axis.

Usage:
    dcs-bind-wizard.py                      # TUI wizard
    dcs-bind-wizard.py --reset              # wizard from scratch
    dcs-bind-wizard.py -r other.json        # use a different results file
    dcs-bind-wizard.py -g -a su-25T         # write the diff.lua files
"""

import argparse
import curses
import fcntl
import glob
import json
import os
import re
import select
import shutil
import struct
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESULTS = os.path.join(SCRIPT_DIR, "dcs-bind-wizard-results.json")

STEAMAPPS = os.path.expanduser("~/.local/share/Steam/steamapps")
DCS_APPID = "223750"
DEFAULT_GAME_DIR = os.path.join(STEAMAPPS, "common/DCSWorld")

JS_EVENT_FMT = "IhBB"          # time, value, type, number
JS_EVENT_SIZE = struct.calcsize(JS_EVENT_FMT)
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80

JSIOCGAXES = 0x80016A11
JSIOCGBUTTONS = 0x80016A12
JSIOCGNAME = 0x80806A13        # 128 bytes
JSIOCGAXMAP = 0x80406A32       # u8[64]: js axis index -> ABS_* code

AXIS_THRESHOLD = 14000         # out of +-32767
DEBOUNCE = 0.7

# ABS_* code -> axis name as DCS sees it under Wine (HID-usage order)
ABS_TO_DCS = {0: "JOY_X", 1: "JOY_Y", 2: "JOY_Z",
              3: "JOY_RX", 4: "JOY_RY", 5: "JOY_RZ",
              6: "JOY_SLIDER1", 7: "JOY_SLIDER2"}

# DCS assigns these to EVERY joystick device it sees (DefaultAssignments.lua
# + base_joystick_binding.lua); the generator removes them wherever they
# would conflict with the wizard's bindings.
DEFAULT_AXIS_KEYS = {"Pitch": "JOY_Y", "Roll": "JOY_X",
                     "Rudder": "JOY_RZ", "Thrust": "JOY_Z"}
DEFAULT_BUTTON_KEYS = {"Weapon Fire": "JOY_BTN1", "Weapon Change": "JOY_BTN4",
                       "Cannon": "JOY_BTN5"}

# axis tuning applied by command name: (curvature, deadzone).  Values match
# Eagle Dynamics' own VPC WarBRD profile; tweak in-game via Axis Tune.
AXIS_FILTERS = {"Pitch": (0.12, 0.03), "Roll": (0.12, 0.03),
                "Rudder": (0.15, 0.05)}
SLIDER_PREFIX = "Thrust"       # Thrust / Thrust Left / Thrust Right

CATEGORY_ORDER = ["Axes", "Flight Control", "Systems", "Autopilot", "Modes",
                  "Weapons", "Sensors", "Countermeasures", "View", "Cockpit"]


# ---------------------------------------------------------------- devices --

class Device:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        buf = bytearray(128)
        fcntl.ioctl(self.fd, JSIOCGNAME, buf)
        self.name = buf.split(b"\0", 1)[0].decode(errors="replace")
        self.n_axes = struct.unpack("B", fcntl.ioctl(self.fd, JSIOCGAXES, b"\0"))[0]
        self.n_buttons = struct.unpack("B", fcntl.ioctl(self.fd, JSIOCGBUTTONS, b"\0"))[0]
        self.axis_vals = {}
        self.role = None           # assigned during detection

    def read_events(self):
        events = []
        while True:
            try:
                data = os.read(self.fd, JS_EVENT_SIZE * 64)
            except BlockingIOError:
                break
            if not data:
                break
            for off in range(0, len(data) - JS_EVENT_SIZE + 1, JS_EVENT_SIZE):
                _, value, etype, number = struct.unpack_from(JS_EVENT_FMT, data, off)
                init = bool(etype & JS_EVENT_INIT)
                etype &= ~JS_EVENT_INIT
                if etype == JS_EVENT_AXIS:
                    self.axis_vals[number] = value
                    if not init:
                        events.append(("axis", number, value))
                elif etype == JS_EVENT_BUTTON and not init:
                    events.append(("button", number, value))
        return events


def axis_map(fd, n_axes):
    """js axis index -> ABS_* code, via JSIOCGAXMAP."""
    buf = bytearray(64)
    fcntl.ioctl(fd, JSIOCGAXMAP, buf)
    return list(buf[:n_axes])


def dcs_running():
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as f:
                if b"DCS.exe" in f.read():
                    return True
        except OSError:
            continue
    return False


# --------------------------------------------------------- game data mining --

_NAME_RE = re.compile(r"name\s*=\s*_\('((?:[^'\\]|\\.)*)'\)")
_CATEGORY_RE = re.compile(r"category\s*=\s*(?:\{\s*)?_\('((?:[^'\\]|\\.)*)'\)")
_HASH_RE = re.compile(r'\["(a\d+[^"]*|d(?:\d+|nil)p[^"]*)"\]\s*=\s*\{')
_DIFF_NAME_RE = re.compile(r'\["name"\]\s*=\s*"([^"]+)"')
_LOG_DEVICE_RE = re.compile(r"created \[(.+?)\] with full id \[(.+?)\],JOYSTICK")


def _unescape(s):
    return s.replace("\\'", "'").replace('\\"', '"')


def discover_aircraft(cfg):
    """aircraft key (Input folder name) -> {'display', 'factory_dir'}."""
    out = {}
    pattern = os.path.join(cfg["game_dir"], "Mods", "aircraft", "*",
                           "Input", "*", "joystick", "default.lua")
    for default_lua in sorted(glob.glob(pattern)):
        joy_dir = os.path.dirname(default_lua)
        key = os.path.basename(os.path.dirname(joy_dir))
        display = key
        name_lua = os.path.join(os.path.dirname(joy_dir), "name.lua")
        if os.path.exists(name_lua):
            m = _NAME_RE.search("name = " +
                                open(name_lua, encoding="utf-8").read()
                                .replace("return", "", 1))
            if m:
                display = _unescape(m.group(1))
        out[key] = {"display": display, "factory_dir": joy_dir}
    # aircraft the user has flown but whose module folder we did not match
    for d in sorted(glob.glob(os.path.join(cfg["saved_games"], "Config",
                                           "Input", "*", "joystick"))):
        key = os.path.basename(os.path.dirname(d))
        out.setdefault(key, {"display": key, "factory_dir": None})
    return out


def harvest_commands(cfg, aircraft_key, factory_dir):
    """hash -> {'name', 'kind', 'category'} for one aircraft.

    Hashes and names come from every *.diff.lua we can find (factory
    profiles + the user's own saved ones); categories come from the
    module's default.lua and the shared base binding files.
    """
    commands = {}
    diff_dirs = []
    if factory_dir:
        diff_dirs.append(factory_dir)
    diff_dirs.append(os.path.join(cfg["saved_games"], "Config", "Input",
                                  aircraft_key, "joystick"))
    for d in diff_dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.diff.lua"))):
            text = open(path, encoding="utf-8", errors="replace").read()
            for m in _HASH_RE.finditer(text):
                nm = _DIFF_NAME_RE.search(text[m.end():m.end() + 2000])
                if nm and m.group(1) not in commands:
                    commands[m.group(1)] = {"name": nm.group(1)}

    categories = {}
    cat_files = []
    if factory_dir:
        cat_files.append(os.path.join(factory_dir, "default.lua"))
    cat_files += sorted(glob.glob(os.path.join(
        cfg["game_dir"], "Config", "Input", "Aircrafts", "*.lua")))
    for path in cat_files:
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            if line.lstrip().startswith("--"):
                continue
            nm, cat = _NAME_RE.search(line), _CATEGORY_RE.search(line)
            if nm:
                categories.setdefault(
                    _unescape(nm.group(1)),
                    _unescape(cat.group(1)) if cat else "Other")

    def tokens(name):
        return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", name.lower())}

    token_map = [(tokens(n), c) for n, c in categories.items()]

    def category_for(name):
        """Exact match first; factory profiles often carry outdated labels
        (e.g. 'Trim Hat - NOSE UP' vs today's 'Trim: Nose Up'), so fall
        back to the best token-subset match against current game names."""
        if name in categories:
            return categories[name]
        mine = tokens(name)
        best, best_n = None, 1
        for toks, cat in token_map:
            if len(toks) > best_n and toks <= mine:
                best, best_n = cat, len(toks)
        if best:
            return best
        # last resort: strongest token overlap, but only when every
        # equally-good candidate agrees on the category
        best_n, cats = 1, set()
        for toks, cat in token_map:
            n = len(toks & mine)
            if n > best_n:
                best_n, cats = n, {cat}
            elif n == best_n:
                cats.add(cat)
        return cats.pop() if len(cats) == 1 else "Other"

    for h, info in commands.items():
        info["kind"] = "axis" if h.startswith("a") else "button"
        info["category"] = ("Axes" if info["kind"] == "axis"
                            else category_for(info["name"]))
    return commands


def build_sections(commands):
    """[(category, [(hash, name, kind), ...]), ...] in a sensible order."""
    by_cat = {}
    for h, info in commands.items():
        by_cat.setdefault(info["category"], []).append(
            (h, info["name"], info["kind"]))
    for items in by_cat.values():
        items.sort(key=lambda x: x[1].lower())

    def order(cat):
        return (CATEGORY_ORDER.index(cat) if cat in CATEGORY_ORDER
                else len(CATEGORY_ORDER), cat.lower())
    return [(cat, by_cat[cat]) for cat in sorted(by_cat, key=order)]


def dcs_device_ids(saved_games):
    """DCS short device name -> 'Name {GUID}' full id (diff.lua file stem).

    Read from the newest dcs.log; falls back to existing diff.lua names.
    """
    out = {}
    log = os.path.join(saved_games, "Logs", "dcs.log")
    if os.path.exists(log):
        for line in open(log, encoding="utf-8", errors="replace"):
            m = _LOG_DEVICE_RE.search(line)
            if m:
                out[m.group(1)] = m.group(2)
    for path in glob.glob(os.path.join(saved_games, "Config", "Input",
                                       "*", "joystick", "*.diff.lua")):
        stem = os.path.basename(path)[:-len(".diff.lua")]
        m = re.match(r"(.+?) \{[0-9A-Fa-f-]+\}$", stem)
        if m:
            out.setdefault(m.group(1), stem)
    return out


# --------------------------------------------------------------- generator --

def lua(v, indent=1):
    pad = "\t" * indent
    if isinstance(v, dict):
        lines = ["{"]
        for k in sorted(v):
            key = "[%d]" % k if isinstance(k, int) else '["%s"]' % k
            lines.append('%s%s = %s,' % (pad, key, lua(v[k], indent + 1)))
        lines.append("\t" * (indent - 1) + "}")
        return "\n".join(lines)
    if isinstance(v, list):
        return lua({i + 1: x for i, x in enumerate(v)}, indent)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return repr(v)


def make_filter(name, invert):
    curvature, deadzone = AXIS_FILTERS.get(name, (0.0, 0.0))
    return {"curvature": [curvature], "deadzone": deadzone,
            "hardwareDetent": False, "hardwareDetentAB": 0,
            "hardwareDetentMax": 0, "invert": bool(invert),
            "saturationX": 1, "saturationY": 1,
            "slider": name.startswith(SLIDER_PREFIX)}


def vanilla_filter():
    return make_filter("", False)


def resolve_devices(results):
    """role -> {'dcs_id', 'axmap', 'name'}; refreshes axmap from live
    devices when missing."""
    out = {}
    saved = results.get("_devices", {})
    for role in ("stick", "throttle"):
        info = dict(saved.get(role, {}))
        if not info.get("axmap") or not info.get("dcs_id"):
            for path in sorted(glob.glob("/dev/input/js*")):
                try:
                    dev = Device(path)
                except OSError:
                    continue
                if dev.name == info.get("name"):
                    info.setdefault("axmap", axis_map(dev.fd, dev.n_axes))
                os.close(dev.fd)
        if info.get("dcs_id") and info.get("axmap"):
            out[role] = info
    missing = {"stick", "throttle"} - set(out)
    if missing:
        raise RuntimeError(
            "cannot resolve devices for: %s — run the TUI wizard once "
            "with the devices plugged in (and after DCS has seen them "
            "at least once, so their ids appear in dcs.log)"
            % ", ".join(sorted(missing)))
    return out


def device_axis_keys(info):
    return {ABS_TO_DCS[c] for c in info["axmap"] if c in ABS_TO_DCS}


def generate(results, cfg, aircraft):
    """Build and install the per-device diff.lua files. Returns summary."""
    if dcs_running():
        raise RuntimeError("DCS is running — quit the game first "
                           "(it overwrites Config/Input on exit)")
    bindings = {h: r for h, r in results.get("aircraft", {})
                .get(aircraft, {}).items() if r}
    if not bindings:
        raise RuntimeError("nothing bound for %s yet" % aircraft)
    devs = resolve_devices(results)
    diffs = {role: {"axisDiffs": {}, "keyDiffs": {}} for role in devs}

    def other(role):
        return "throttle" if role == "stick" else "stick"

    for h, r in bindings.items():
        role, name = r["role"], r["name"]
        table = "axisDiffs" if r["type"] == "axis" else "keyDiffs"
        entry = diffs[role][table].setdefault(h, {"name": name})
        if r["type"] == "axis":
            axmap = devs[role]["axmap"]
            if r["index"] >= len(axmap) or axmap[r["index"]] not in ABS_TO_DCS:
                raise RuntimeError("%s: cannot map %s axis %d — axmap=%s"
                                   % (name, role, r["index"], axmap))
            key = ABS_TO_DCS[axmap[r["index"]]]
            filt = make_filter(name, r.get("invert"))
            default = DEFAULT_AXIS_KEYS.get(name)
            if key == default:
                if filt != vanilla_filter():
                    entry["changed"] = [{"key": key, "filter": filt}]
            else:
                entry["added"] = [{"key": key, "filter": filt}]
                if default and default in device_axis_keys(devs[role]):
                    entry.setdefault("removed", []).append({"key": default})
            if default and default in device_axis_keys(devs[other(role)]):
                oentry = diffs[other(role)]["axisDiffs"].setdefault(
                    h, {"name": name})
                oentry.setdefault("removed", []).append({"key": default})
        else:
            key = "JOY_BTN%d" % (r["index"] + 1)
            default = DEFAULT_BUTTON_KEYS.get(name)
            if key != default:
                entry["added"] = [{"key": key}]
                if default:
                    entry.setdefault("removed", []).append({"key": default})
            if default:
                oentry = diffs[other(role)]["keyDiffs"].setdefault(
                    h, {"name": name})
                oentry.setdefault("removed", []).append({"key": default})

    # drop entries that ended up empty (e.g. a pure-default axis)
    for role in diffs:
        for table in ("axisDiffs", "keyDiffs"):
            diffs[role][table] = {
                h: e for h, e in diffs[role][table].items()
                if set(e) - {"name"}}

    out_dir = os.path.join(cfg["saved_games"], "Config", "Input",
                           aircraft, "joystick")
    os.makedirs(out_dir, exist_ok=True)
    lines = []
    for role, info in devs.items():
        path = os.path.join(out_dir, info["dcs_id"] + ".diff.lua")
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        body = lua({"axisDiffs": diffs[role]["axisDiffs"],
                    "keyDiffs": diffs[role]["keyDiffs"]}, 1)
        # no trailing newline: byte-compatible with DCS's own serializer
        with open(path, "w", encoding="utf-8") as f:
            f.write("local diff = %s\nreturn diff" % body)
        n = (len(diffs[role]["axisDiffs"]) + len(diffs[role]["keyDiffs"]))
        lines.append("%s: %d entries -> %s" % (role, n, path))
    lines.append("backups: *.bak next to each file (when one existed)")
    lines.append("Start DCS and check Options -> Controls -> %s" % aircraft)
    return lines


# -------------------------------------------------------------------- TUI --

class Tui:
    def __init__(self, scr):
        self.scr = scr
        self.title = ""
        self.lines = []

    def key(self, timeout=0.0):
        """'enter' / 'esc' / 'up' / 'down' / printable char / None."""
        deadline = time.monotonic() + timeout
        while True:
            c = self.scr.getch()
            if c == -1:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.02)
                continue
            if c in (10, 13, curses.KEY_ENTER):
                return "enter"
            if c == 27:
                return "esc"
            if c == curses.KEY_UP:
                return "up"
            if c == curses.KEY_DOWN:
                return "down"
            if 32 <= c < 127:
                return chr(c)

    def _put(self, y, x, text, attr=curses.A_NORMAL):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h:
            try:
                self.scr.addstr(y, x, text[:max(0, w - x - 1)], attr)
            except curses.error:
                pass

    def menu(self, title, items, index=0, footer="arrows = move, "
             "RETURN = select, ESC = back"):
        index = max(0, min(index, len(items) - 1))
        top = 0
        while True:
            h, _ = self.scr.getmaxyx()
            visible = max(3, h - 4)
            if index < top:
                top = index
            elif index >= top + visible:
                top = index - visible + 1
            self.scr.erase()
            self._put(0, 0, title, curses.A_BOLD)
            for row, i in enumerate(range(top,
                                          min(len(items), top + visible))):
                attr = curses.A_REVERSE if i == index else curses.A_NORMAL
                self._put(2 + row, 2, items[i], attr)
            self._put(h - 1, 0, "%s  (%d/%d)" % (footer, index + 1,
                                                 len(items)))
            self.scr.refresh()
            k = self.key(0.5)
            if k == "up":
                index = (index - 1) % len(items)
            elif k == "down":
                index = (index + 1) % len(items)
            elif k == "enter":
                return index
            elif k == "esc":
                return None

    def page(self, title):
        self.title = title
        self.lines = []
        self._redraw()

    def log(self, line=""):
        self.lines.append(line)
        self._redraw()

    def _redraw(self):
        h, _ = self.scr.getmaxyx()
        self.scr.erase()
        self._put(0, 0, self.title, curses.A_BOLD)
        for i, ln in enumerate(self.lines[-(h - 2):]):
            self._put(2 + i, 0, ln)
        self.scr.refresh()

    def wait_any_key(self):
        self.log("")
        self.log("-- press RETURN or ESC to continue --")
        while self.key(0.5) not in ("enter", "esc"):
            pass


# ----------------------------------------------------------- capture logic --

def drain(devices, tui, seconds=DEBOUNCE):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([d.fd for d in devices], [], [], 0.05)
        for d in devices:
            if d.fd in r:
                d.read_events()
    while tui.key(0.0):
        pass


def wait_input(devices, want_axis, tui):
    """Wait for a joystick press / axis move, or ESC. No time limit.

    Returns (dev, kind, index) or "skip".
    """
    for d in devices:
        d.read_events()
    baseline = {d.path: dict(d.axis_vals) for d in devices}
    while True:
        r, _, _ = select.select([d.fd for d in devices], [], [], 0.05)
        if tui.key(0.0) == "esc":
            return "skip"
        for d in devices:
            if d.fd not in r:
                continue
            for kind, number, value in d.read_events():
                if kind == "button" and value == 1 and not want_axis:
                    return d, "button", number
                if kind == "axis" and want_axis:
                    base = baseline[d.path].get(number, 0)
                    if abs(value - base) > AXIS_THRESHOLD:
                        return d, "axis", number


def detect_device(devices, label, tui):
    tui.log("--> press any button on your %s:" % label)
    while True:
        got = wait_input(devices, want_axis=False, tui=tui)
        if got == "skip":
            continue                       # ESC is meaningless here
        d = got[0]
        if d.role is not None:
            tui.log("    that came from the %s — try again on the %s"
                    % (d.role, label))
            drain(devices, tui)
            continue
        tui.log("    OK: %s" % d.name)
        tui.log("")
        drain(devices, tui)
        return d


def detect_devices_screen(tui, cfg, results):
    devices = []
    for path in sorted(glob.glob("/dev/input/js*")):
        try:
            devices.append(Device(path))
        except OSError:
            pass
    tui.page("Device detection")
    for d in devices:
        tui.log("  %s: %s  (%d axes, %d buttons)"
                % (d.path, d.name, d.n_axes, d.n_buttons))
    tui.log("")
    if len(devices) < 2:
        tui.log("Need at least two joystick devices — check connections.")
        tui.wait_any_key()
        return None
    stick = detect_device(devices, "FLIGHT STICK", tui)
    stick.role = "stick"
    throttle = detect_device(devices, "THROTTLE", tui)
    throttle.role = "throttle"

    ids = dcs_device_ids(cfg["saved_games"])
    devinfo = {}
    for d in (stick, throttle):
        dcs_id = None
        for short, full in ids.items():
            if short in d.name:
                dcs_id = full
                break
        devinfo[d.role] = {"name": d.name, "dcs_id": dcs_id,
                           "axmap": axis_map(d.fd, d.n_axes)}
        if not dcs_id:
            tui.log("WARNING: no DCS id found for '%s' — start DCS once "
                    "so it lands in dcs.log, then rerun." % d.name)
    results["_devices"] = devinfo
    if any(not v["dcs_id"] for v in devinfo.values()):
        tui.wait_any_key()
    return [stick, throttle]


# ------------------------------------------------------------------- flows --

def resolve_config(args, cfg):
    """Validate --game-dir / remembered config; exits with a clear message."""
    game = args.game_dir or cfg.get("game_dir") or (
        DEFAULT_GAME_DIR if os.path.isdir(DEFAULT_GAME_DIR) else None)
    if not game:
        sys.exit("Pass --game-dir /path/to/steamapps/common/DCSWorld "
                 "(remembered in the results file afterwards).")
    game = os.path.abspath(os.path.expanduser(game))
    if not os.path.isdir(os.path.join(game, "Mods", "aircraft")):
        sys.exit("%s\ndoes not look like a DCS World install "
                 "(missing Mods/aircraft)." % game)
    # <steamapps>/common/DCSWorld -> <steamapps>/compatdata/223750/...
    steamapps = os.path.dirname(os.path.dirname(game))
    derived = os.path.join(steamapps, "compatdata", DCS_APPID, "pfx",
                           "drive_c", "users", "steamuser", "Saved Games",
                           "DCS")
    saved = args.saved_games or (cfg.get("saved_games")
                                 if not args.game_dir else None) or derived
    if not os.path.isdir(saved):
        sys.exit("Saved Games folder not found:\n%s\nRun the game once so "
                 "it creates it, or pass --saved-games." % saved)
    cfg.update({"game_dir": game, "saved_games": saved})
    return cfg


def describe(results, r):
    if not r:
        return "(unset)"
    if r["type"] == "button":
        return "%s BTN%d" % (r["role"], r["index"] + 1)
    axmap = results.get("_devices", {}).get(r["role"], {}).get("axmap")
    key = ""
    if axmap and r["index"] < len(axmap) and axmap[r["index"]] in ABS_TO_DCS:
        key = " " + ABS_TO_DCS[axmap[r["index"]]]
    return "%s axis %d%s%s" % (r["role"], r["index"], key,
                               " (inverted)" if r.get("invert") else "")


def save(results, path):
    with open(path, "w") as f:
        json.dump(results, f, indent=2)


def section_stats(bindings, items):
    bound = sum(1 for h, _, _ in items if bindings.get(h))
    return bound, len(items)


def aircraft_stats(bindings, sections):
    bound = total = 0
    for _, items in sections:
        b, n = section_stats(bindings, items)
        bound, total = bound + b, total + n
    return bound, total


def progress_label(title, bound, total):
    return "%s  [%d/%d]" % (title, bound, total)


def run_table(tui, devices, results, bindings, used, sections, path,
              heading):
    """Arrow-key table over all commands of the given sections."""
    rows = []                             # ("header", ...) / ("item", ...)
    for sec_title, items in sections:
        rows.append(("header", sec_title, None, None))
        for h, name, kind in items:
            rows.append(("item", h, name, kind))
    item_rows = [i for i, r in enumerate(rows) if r[0] == "item"]
    if not item_rows:
        return
    sel = item_rows[0]
    state = {"top": 0}

    def move(step):
        nonlocal sel
        pos = item_rows.index(sel)
        pos = max(0, min(len(item_rows) - 1, pos + step))
        sel = item_rows[pos]

    def draw(status_lines):
        h, _ = tui.scr.getmaxyx()
        visible = max(4, h - 6)
        top = state["top"]
        if sel < top:
            top = sel
        elif sel >= top + visible:
            top = sel - visible + 1
        state["top"] = top
        tui.scr.erase()
        tui._put(0, 0, heading, curses.A_BOLD)
        for row, i in enumerate(range(top, min(len(rows), top + visible))):
            what, a, b, _ = rows[i]
            y = 2 + row
            if what == "header":
                tui._put(y, 0, "--- %s ---" % a, curses.A_BOLD)
            else:
                current = describe(results, bindings.get(a))
                attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
                tui._put(y, 2, "%-44s %s" % (b[:44], current), attr)
        for j, line in enumerate(status_lines[:2]):
            tui._put(h - 3 + j, 0, line)
        tui._put(h - 1, 0, "arrows = move, RETURN = bind, I = invert, "
                           "X = clear, ESC = back")
        tui.scr.refresh()

    status = []
    while True:
        draw(status)
        k = tui.key(0.5)
        if k is None:
            continue
        if k == "esc":
            return
        if k == "up":
            move(-1)
            status = []
        elif k == "down":
            move(+1)
            status = []
        elif k in ("x", "X"):
            _, h_, name, _ = rows[sel]
            if h_ in bindings:
                del bindings[h_]
                save(results, path)
            status = ["%s: cleared (DCS default, if any, comes back)" % name]
        elif k in ("i", "I"):
            _, h_, name, _ = rows[sel]
            r = bindings.get(h_)
            if r and r["type"] == "axis":
                r["invert"] = not r.get("invert")
                save(results, path)
                status = ["%s: %s" % (name, describe(results, r))]
        elif k == "enter":
            _, h_, name, kind = rows[sel]
            while True:
                prompt = ("move the axis you want for: %s"
                          if kind == "axis"
                          else "press the button you want for: %s") % name
                draw(["-> " + prompt, "   waiting...  (ESC = cancel)"])
                got = wait_input(devices, want_axis=(kind == "axis"),
                                 tui=tui)
                if got == "skip":
                    status = ["%s: unchanged" % name]
                    break
                d, etype, number = got
                invert = bool(bindings.get(h_, {}).get("invert")) \
                    if kind == "axis" else False
                key_ = (d.role, etype, number)
                dup = ("  WARNING: same as %s!" % used[key_]
                       if key_ in used and used[key_] != name
                       and etype == "button" else "")
                drain(devices, tui)
                accept = None
                while accept is None:
                    label = ("BTN%d" % (number + 1) if etype == "button"
                             else "axis %d%s" % (number,
                                                 " (inverted)" if invert
                                                 else ""))
                    opts = ("[RETURN = accept, ESC = redo, I = invert]"
                            if etype == "axis"
                            else "[RETURN = accept, ESC = redo]")
                    draw(["-> " + prompt,
                          "   captured: %s %s%s  %s"
                          % (d.role, label, dup, opts)])
                    kk = tui.key(0.5)
                    if kk == "enter":
                        accept = True
                    elif kk == "esc":
                        accept = False
                    elif kk in ("i", "I") and etype == "axis":
                        invert = not invert
                if accept:
                    used[key_] = name
                    r = {"name": name, "role": d.role, "type": etype,
                         "index": number}
                    if etype == "axis":
                        r["invert"] = invert
                    bindings[h_] = r
                    save(results, path)
                    status = ["%s: %s" % (name, describe(results, r))]
                    move(+1)                       # the NEXT command
                    break
            drain(devices, tui)


def tui_main(scr, args, results, cfg):
    curses.curs_set(0)
    scr.nodelay(True)
    scr.keypad(True)
    try:
        curses.set_escdelay(50)
    except AttributeError:
        pass
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        pass
    tui = Tui(scr)

    results["_config"] = cfg
    save(results, args.results)

    active = detect_devices_screen(tui, cfg, results)
    if active is None:
        return
    save(results, args.results)

    aircraft_all = discover_aircraft(cfg)
    if not aircraft_all:
        tui.page("Aircraft")
        tui.log("No aircraft modules found under %s" % cfg["game_dir"])
        tui.wait_any_key()
        return

    aircraft = cfg.get("aircraft")
    while True:
        keys = sorted(aircraft_all, key=lambda k:
                      aircraft_all[k]["display"].lower())
        if aircraft not in aircraft_all:
            idx = tui.menu("Pick an aircraft",
                           [aircraft_all[k]["display"] for k in keys],
                           footer="arrows = move, RETURN = select, "
                                  "ESC = quit")
            if idx is None:
                return
            aircraft = keys[idx]
            cfg["aircraft"] = aircraft
            results["_config"] = cfg
            save(results, args.results)

        commands = harvest_commands(cfg, aircraft,
                                    aircraft_all[aircraft]["factory_dir"])
        sections = build_sections(commands)
        bindings = results.setdefault("aircraft", {}).setdefault(aircraft, {})
        bindings_clean = {h: r for h, r in bindings.items() if r}
        display = aircraft_all[aircraft]["display"]

        used = {}
        for h, r in bindings_clean.items():
            if r["type"] == "button":
                used.setdefault((r["role"], "button", r["index"]), r["name"])

        choice = tui.menu("dcs-bind-wizard — %s" % display, [
            progress_label("Bind controls",
                           *aircraft_stats(bindings_clean, sections)),
            "Generate diff.lua files",
            "Change aircraft",
            "Quit",
        ])
        if choice in (None, 3):
            return
        if choice == 2:
            aircraft = None
            continue
        if choice == 1:
            tui.page("Generate — %s" % display)
            try:
                for line in generate(results, cfg, aircraft):
                    tui.log(line)
            except (RuntimeError, OSError) as e:
                tui.log("ERROR: %s" % e)
            tui.wait_any_key()
            continue
        while True:
            labels = ([progress_label("ALL sections",
                                      *aircraft_stats(bindings, sections))]
                      + [progress_label(title,
                                        *section_stats(bindings, items))
                         for title, items in sections]
                      + ["<- back"])
            sc = tui.menu("%s — mapping sections" % display, labels)
            if sc is None or sc == len(labels) - 1:
                break
            chosen = sections if sc == 0 else [sections[sc - 1]]
            heading = "%s — %s" % (display, "all sections" if sc == 0
                                   else chosen[0][0])
            run_table(tui, active, results, bindings, used, chosen,
                      args.results, heading)


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(
        description="DCS World HOTAS bindings wizard and diff.lua generator")
    ap.add_argument("-r", "--results", default=DEFAULT_RESULTS,
                    help="results JSON: wizard state / generator input "
                         "(default: next to this script)")
    ap.add_argument("--reset", action="store_true",
                    help="delete the results file and start from scratch")
    ap.add_argument("-g", "--generate", action="store_true",
                    help="generate the diff.lua files and exit")
    ap.add_argument("-a", "--aircraft", default=None,
                    help="aircraft key for --generate (e.g. su-25T); "
                         "defaults to the one last used in the TUI")
    ap.add_argument("--game-dir", default=None,
                    help="DCS World game folder (steamapps/common/DCSWorld);"
                         " auto-detected or remembered afterwards")
    ap.add_argument("--saved-games", default=None,
                    help="Saved Games/DCS folder inside the Proton prefix "
                         "(default: derived from --game-dir)")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.results):
        os.remove(args.results)

    results = {}
    if os.path.exists(args.results):
        with open(args.results) as f:
            results = json.load(f)
    cfg = resolve_config(args, dict(results.get("_config", {})))

    if args.generate:
        aircraft = args.aircraft or cfg.get("aircraft")
        if not aircraft:
            sys.exit("Pass --aircraft (e.g. -a su-25T).")
        try:
            for line in generate(results, cfg, aircraft):
                print(line)
        except (RuntimeError, OSError) as e:
            sys.exit("ERROR: %s" % e)
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("Run this in a regular terminal (the wizard is a TUI), "
                 "or use --generate for headless generation.")
    curses.wrapper(tui_main, args, results, cfg)


if __name__ == "__main__":
    main()
