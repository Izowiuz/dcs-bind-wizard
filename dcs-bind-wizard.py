#!/usr/bin/env python3
"""Interactive DCS World bindings wizard + diff.lua generator for HOTAS on Linux.

TUI mode (default): full-screen terminal wizard. Flow:

    1. game folder comes from --game-dir (remembered in the results file,
       so you only pass it once); the Saved Games folder inside the Proton
       prefix is derived from it
    2. pick the aircraft (installed modules are discovered automatically)
    3. bind the essentials — or pick one of the game's own sections, or ALL

'Essentials' is the short list to set up first, and it tries to answer
the question a new module actually raises — not 'which button do I
press' but 'what is this thing and do I need it?':

    * the commands are ranked by how many of the factory HOTAS profiles
      shipped with the module bind each one, so a Hornet opens on
      trigger, trim, sensor control and TDC rather than on the 800-odd
      switches of its cockpit;
    * they are grouped in the order you learn an aircraft — fly it,
      take off and land, fight with it, sensors and radio;
    * the selected row explains itself in the three lines above the key
      legend: what the control does, where it sits in the real aircraft
      and what kind of hardware it wants, and which of your two devices
      the factory profiles put it on, and for a switch with a direction
      (trim, the castle hat, weapon select, the TDC) which way it goes.
      All three stay on screen while you are capturing a button — the
      prompt gets its own line — because that is when you need them.

Commands are harvested from the game files themselves: the module's
default.lua is run through a Lua interpreter (when one is installed) for
the full list with today's names and categories, and the factory joystick
profiles supply the hashes of the sim's own commands plus that popularity
score. Either way there is no hardcoded function list — the wizard works
for any installed module.

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
import copy
import curses
import fcntl
import glob
import json
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
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


_INPUT_PROFILE_RE = re.compile(
    r"""\[\s*['"]([^'"]+)['"]\s*\]\s*=\s*[^,}]*?/Input/([^'"]+?)/?['"]""")


def input_profiles(module_dir):
    """Input folder name -> the unit name DCS saves that aircraft under.

    Straight out of the module's entry.lua, where the two are spelled out
    side by side — and they differ often enough to matter: the Hornet
    ships `["FA-18C_hornet"] = .. '/Input/FA-18C/'`, so its user profiles
    live in Config/Input/FA-18C_hornet while its factory ones are under
    Input/FA-18C.
    """
    entry = os.path.join(module_dir, "entry.lua")
    if not os.path.exists(entry):
        return {}
    text = open(entry, encoding="utf-8", errors="replace").read()
    block = text.split("InputProfiles", 1)
    if len(block) < 2:
        return {}
    block = block[1].split("}", 1)[0]
    return {os.path.basename(path): unit
            for unit, path in _INPUT_PROFILE_RE.findall(block)}


def discover_aircraft(cfg):
    """aircraft key (Input folder name) ->
    {'display', 'factory_dir', 'input_id'}."""
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
        module_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            joy_dir)))
        out[key] = {"display": display, "factory_dir": joy_dir,
                    "input_id": input_profiles(module_dir).get(key, key)}
    # aircraft the user has flown but whose module folder we did not match
    known = {a["input_id"] for a in out.values()} | set(out)
    for d in sorted(glob.glob(os.path.join(cfg["saved_games"], "Config",
                                           "Input", "*", "joystick"))):
        key = os.path.basename(os.path.dirname(d))
        if key not in known:
            out[key] = {"display": key, "factory_dir": None,
                        "input_id": key}
    return out


def input_id(cfg, aircraft):
    """The folder DCS itself reads this aircraft's user profiles from."""
    return (discover_aircraft(cfg).get(aircraft) or {}).get(
        "input_id", aircraft)


LUA_HARVEST = r"""
-- Dump every bindable command of a DCS module, one TSV row each:
--     <kind> <TAB> <hash> <TAB> <name> <TAB> <category>
-- kind is 'k'/'a' for key/axis commands whose ids are plain numbers (the
-- module's own cockpit commands) and 'K'/'A' for the ones wired to an
-- engine constant (iCommand...) that only the running game knows -- those
-- rows carry an empty hash and get matched back to one by name.
--
-- The module's default.lua is normal Lua, it just expects the game's
-- globals to exist; every unknown global becomes a sentinel table that
-- survives being called and indexed, so the file runs to completion.
local game_dir, folder_arg = ...
folder = folder_arg

local sentinel_mt
local function sentinel(sym)
    return setmetatable({__sym = sym}, sentinel_mt)
end
sentinel_mt = {
    __call = function(t, ...) return sentinel(t.__sym .. "()") end,
    __index = function(t, k)
        return sentinel(t.__sym .. "." .. tostring(k))
    end,
    __add = function(t, o) return sentinel("sum") end,
    __sub = function(t, o) return sentinel("diff") end,
    __unm = function(t) return sentinel("neg") end,
    __len = function(t) return 0 end,
    __tostring = function(t) return "<" .. t.__sym .. ">" end,
}
setmetatable(_G, {__index = function(t, k)
    local s = sentinel(k)
    rawset(t, k, s)
    return s
end})

_ = function(s) return s end
require = function(m) return sentinel("require:" .. tostring(m)) end
function join(dst, src)
    for _i, v in ipairs(src) do dst[#dst + 1] = v end
    return dst
end
function external_profile(rel)
    return assert(loadfile(game_dir .. "/" .. rel))()
end

-- DCS serializes the ids with Lua 5.1 number formatting: 1.0 -> "1"
local function num(v)
    if v == nil then return "nil" end
    if type(v) == "number" then return string.format("%.14g", v) end
    return nil                          -- an engine constant: unresolved
end
local function category(c)
    if type(c) == "table" then return tostring(c[1] or "") end
    return tostring(c or "")
end
local function clean(s)
    return (tostring(s):gsub("[\t\r\n]", " "))
end

local res = assert(loadfile(folder .. "default.lua"))()

for _i, c in ipairs(res.keyCommands or {}) do
    if c.name then
        local d, p, u = num(c.down), num(c.pressed), num(c.up)
        local cd = num(c.cockpit_device_id)
        local vd, vp, vu = num(c.value_down), num(c.value_pressed),
                           num(c.value_up)
        if d and p and u and cd and vd and vp and vu then
            print(("k\t%s\t%s\t%s"):format(
                "d" .. d .. "p" .. p .. "u" .. u .. "cd" .. cd ..
                "vd" .. vd .. "vp" .. vp .. "vu" .. vu,
                clean(c.name), clean(category(c.category))))
        else
            print(("K\t\t%s\t%s"):format(clean(c.name),
                                         clean(category(c.category))))
        end
    end
end
for _i, c in ipairs(res.axisCommands or {}) do
    if c.name then
        local a, cd = num(c.action), num(c.cockpit_device_id)
        if a and cd then
            print(("a\t%s\t%s\t%s"):format("a" .. a .. "cd" .. cd,
                                           clean(c.name),
                                           clean(category(c.category))))
        else
            print(("A\t\t%s\t%s"):format(clean(c.name),
                                         clean(category(c.category))))
        end
    end
end
"""


def lua_binary():
    for name in ("lua", "luajit", "lua5.4", "lua5.3", "lua5.2", "lua5.1"):
        found = shutil.which(name)
        if found:
            return found
    return None


def lua_commands(cfg, factory_dir):
    """[(hash or None, name, category, kind)] from the module's default.lua.

    Returns None when there is no Lua interpreter on the box or the file
    refuses to run — the caller then falls back to the factory profiles
    alone.
    """
    binary = lua_binary()
    if not binary or not factory_dir:
        return None
    if not os.path.exists(os.path.join(factory_dir, "default.lua")):
        return None
    fd, script = tempfile.mkstemp(suffix=".lua")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(LUA_HARVEST)
        out = subprocess.run([binary, script, cfg["game_dir"],
                              factory_dir + os.sep],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        os.unlink(script)
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] not in ("k", "a", "K", "A"):
            continue
        kind = "axis" if parts[0] in ("a", "A") else "button"
        rows.append((parts[1] or None, parts[2], parts[3], kind))
    return rows or None


def _is_latin(name):
    """DCS ships Russian-language factory profiles too — a Latin name for
    the same command always wins over a Cyrillic one."""
    return not re.search("[\u0400-\u04ff]", name)


def _norm(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


STICK_AXES = ("a2001cdnil", "a2002cdnil")                    # pitch, roll
THROTTLE_AXES = ("a2004cdnil", "a2005cdnil", "a2006cdnil")   # thrust x3


def profile_role(bound, removed):
    """'stick' / 'throttle' / both / neither, for one factory profile.

    A profile says what its device is by what it does with the four axes
    DCS hands to every joystick: a stick claims pitch and roll and throws
    thrust away, a throttle does the opposite, and a button panel drops
    the lot.
    """
    axes = set(STICK_AXES) | set(THROTTLE_AXES)
    if axes <= removed:
        return ()                                   # MFD / UFC / panel
    roles = []
    if set(STICK_AXES) & bound or set(THROTTLE_AXES) <= removed:
        roles.append("stick")
    if set(THROTTLE_AXES) & bound or set(STICK_AXES) <= removed:
        roles.append("throttle")
    return tuple(roles)


def scan_profiles(dirs):
    """(hash -> name, hash -> profiles binding it, hash -> {role: count}).

    'Binds' means the profile added or changed the command rather than
    only stripping a DCS default off it; the serializer sorts the keys of
    a block, so "added"/"changed" always sit before ["name"] and
    "removed" after it. Which device each profile is for is worked out
    from its axes, so the counts also answer "stick or throttle?".
    """
    names, votes, where = {}, {}, {}
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.diff.lua"))):
            text = open(path, encoding="utf-8", errors="replace").read()
            bound, removed = set(), set()
            for m in _HASH_RE.finditer(text):
                h = m.group(1)
                block = text[m.end():m.end() + 3000]
                nm = _DIFF_NAME_RE.search(block)
                head = block[:nm.start()] if nm else block
                if nm and (h not in names or (_is_latin(nm.group(1))
                                              and not _is_latin(names[h]))):
                    names[h] = nm.group(1)
                if '"added"' in head or '"changed"' in head:
                    bound.add(h)
                elif '"removed"' in block:
                    removed.add(h)
            for h in bound:
                votes[h] = votes.get(h, 0) + 1
            for role in profile_role(bound, removed):
                for h in bound:
                    where.setdefault(h, {})
                    where[h][role] = where[h].get(role, 0) + 1
    return names, votes, where


_ENGINE_INDEX = {}


def engine_hash_index(cfg):
    """normalized name -> hash, for the sim's own commands only.

    Their ids live in the exe, so the only place a hash for, say, 'Gear
    Up' can be found is a profile that binds it — and since those ids are
    global, any module's factory profiles will do. Cockpit commands are
    left out on purpose: their ids are per-module, so an identical name
    in another module would mean something else entirely.
    """
    key = cfg["game_dir"]
    if key not in _ENGINE_INDEX:
        dirs = [a["factory_dir"] for a in discover_aircraft(cfg).values()
                if a["factory_dir"]]
        names = scan_profiles(dirs)[0]
        _ENGINE_INDEX[key] = {_norm(n): h for h, n in names.items()
                              if "cdnil" in h and _is_latin(n)}
    return _ENGINE_INDEX[key]


# Which way the switch goes, for the commands that live on a hat. The
# name wins over the symbol where it is explicit, because it describes
# the movement ('PULL(CLIMB)') while the symbol only names the function
# ('STICK_TRIMMER_UP') — you pull a trim hat back to climb.
HAT_MOVES = [
    # diagonals need the two words next to each other, so that a roll
    # trim ('LEFT WING DOWN') does not read as one
    (r"\bup[ -]+left\b|\bleft[ -]+up\b", "UP-LEFT, the diagonal"),
    (r"\bup[ -]+right\b|\bright[ -]+up\b", "UP-RIGHT, the diagonal"),
    (r"\bdown[ -]+left\b|\bleft[ -]+down\b", "DOWN-LEFT, the diagonal"),
    (r"\bdown[ -]+right\b|\bright[ -]+down\b", "DOWN-RIGHT, the diagonal"),
    # explicit next: 'LEFT WING DOWN' is a roll trim, not a nose-down one
    (r"nose ?up|climb|pull\b", "PULL back (nose up)"),
    (r"nose ?down|descend|push\b", "PUSH forward (nose down)"),
    (r"\bfwd\b|forward", "FORWARD, away from you"),
    (r"\baft\b", "AFT, towards you"),
    (r"left", "LEFT"),
    (r"right", "RIGHT"),
    (r"trim.*\bup\b", "PULL back (nose up)"),
    (r"trim.*\bdown\b", "PUSH forward (nose down)"),
    (r"\bup\b", "UP"),
    (r"\bdown\b", "DOWN"),
    (r"\bin\b", "INBOARD (towards your left)"),
    (r"\bout\b", "OUTBOARD"),
    (r"depress|press", "PRESS the hat in"),
]


def hat_move(name, symbol_dir):
    for text_ in (name, symbol_dir):
        for pattern, move in HAT_MOVES:
            if text_ and re.search(pattern, text_, re.I):
                return move
    return ""


_SYMBOL_RE = re.compile(r"_commands\.([A-Z][A-Z0-9_]*)")
DIRECTION_WORDS = ("FWD", "FORWARD", "AFT", "UP", "DOWN", "LEFT", "RIGHT",
                   "IN", "OUT", "DEPRESS", "PRESS")


def symbol_directions(factory_dir):
    """command name -> the direction its own symbol ends in.

    default.lua writes the switch out as e.g. STICK_WEAPON_SELECT_FWD on
    the same line as the name 'Select Sparrow' — which is the only place
    in the game files where that switch's physical direction is spelled
    out at all. Aircraft whose commands live in the exe (the FC3-era
    ones) have no symbols, but their names carry the direction instead.
    """
    out = {}
    path = os.path.join(factory_dir or "", "default.lua")
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        nm = _NAME_RE.search(line)
        if not nm:
            continue
        for sym in _SYMBOL_RE.findall(line):
            head, _, tail = sym.rpartition("_")
            if tail in DIRECTION_WORDS:
                out.setdefault(_unescape(nm.group(1)), (tail, head))
                break
    return out


def switch_family(name, symbol_head):
    """The physical switch a directional command belongs to.

    Its siblings are what tell a four-way hat (Fwd/Aft/Left/Right) from a
    plain two-position panel switch (Up/Down) — the module's symbols group
    them when it has any (STICK_WEAPON_SELECT_*), the names when it does
    not ('Trim Hat - NOSE UP' and friends).
    """
    if symbol_head:
        return symbol_head
    base = name.split(" - ")[0] if " - " in name else name
    return _norm(re.sub(r"\b(%s)\b" % "|".join(DIRECTION_WORDS), "", base,
                        flags=re.I))


def harvest_commands(cfg, aircraft_key, factory_dir):
    """hash -> {'name', 'kind', 'category', 'votes'} for one aircraft.

    Two sources, and the wizard needs both:

    * the module's default.lua, run through a Lua interpreter with the
      game's globals stubbed out — it lists *every* bindable command with
      the name and category the game shows today, and for cockpit
      commands it carries the ids the diff.lua hash is built from;
    * the factory joystick profiles shipped with the module, which supply
      the hashes of the commands wired to engine constants (the sim's own
      pitch/thrust/gear/view commands, whose numbers live in the exe),
      and how many profiles bind each command — the popularity score the
      Essentials list is built from.

    With no Lua interpreter around the profiles are the only source, i.e.
    exactly what this did before: fewer commands, and whatever name and
    category the profile author happened to use.
    """
    user_dirs = glob.glob(os.path.join(cfg["saved_games"], "Config",
                                       "Input", input_id(cfg, aircraft_key),
                                       "*"))
    names, votes, where = scan_profiles([factory_dir] if factory_dir
                                       else [])
    user_names = scan_profiles(user_dirs)[0]
    for h, name in user_names.items():
        names.setdefault(h, name)
    commands = {h: {"name": name} for h, name in names.items()}

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
        info["votes"] = votes.get(h, 0)
        info["where"] = where.get(h, {})

    # default.lua on top: current names and categories for the commands we
    # already have, plus every command no factory profile ever bound
    by_name = {}
    for h, name in names.items():
        by_name.setdefault(_norm(name), h)
    for hash_, name, category, kind in lua_commands(cfg, factory_dir) or []:
        if hash_ is None:                  # engine command: hash by name
            hash_ = (by_name.get(_norm(name))
                     or engine_hash_index(cfg).get(_norm(name)))
            if hash_ is None or hash_.startswith("a") != (kind == "axis"):
                continue
        info = commands.setdefault(hash_, {"votes": votes.get(hash_, 0),
                                          "where": where.get(hash_, {})})
        info["name"] = name
        info["kind"] = kind
        info["category"] = ("Axes" if kind == "axis"
                            else category or info.get("category", "Other"))
    directions = symbol_directions(factory_dir)
    for info in commands.values():
        info["dir"], head = directions.get(info["name"], ("", ""))
        info["family"] = switch_family(info["name"], head)
    families = {}
    for info in commands.values():
        move = hat_move(info["name"], info["dir"])
        if move:
            families.setdefault(info["family"], set()).add(move)
    for info in commands.values():
        info["ways"] = len(families.get(info["family"], ()))
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


ESSENTIALS_MAX = 80
CORE_AXES = ("Pitch", "Roll", "Rudder", "Thrust")
CORE_EXTRA = ("wheel brake",)      # you cannot land without them, yet half
                                   # the factory profiles leave them alone

THEMES = [
    ("fly", "FLY IT — nothing else matters until these work"),
    ("land", "TAKE OFF AND LAND"),
    ("fight", "FIGHT WITH IT"),
    ("sensors", "SENSORS AND RADIO"),
    ("cockpit", "COCKPIT AND VIEWS"),
    ("other", "POPULAR IN THE FACTORY PROFILES (no plain-English hint yet)"),
]

# What a control actually does, for someone who has never flown the type:
# the command names come from the module, these are matched onto them by
# concept, so gear/flaps/trim/countermeasures/radio explain themselves in
# any aircraft. First match wins — keep the broad patterns last.
HINTS = [
    (r"^pitch$", "fly",
     "Stick fore and aft: pull = nose up.",
     "The stick itself — its Y axis."),
    (r"^roll$", "fly",
     "Stick left and right: banks the jet.",
     "The stick itself — its X axis."),
    (r"^rudder$|rudder (left|right)$", "fly",
     "Yaw. Keeps you straight on the runway and pulls the nose around.",
     "Pedals if you have them, otherwise the stick's twist axis."),
    (r"^thrust", "fly",
     "Engine power.",
     "The throttle lever — both levers if the jet has Thrust Left/Right."),
    (r"trim", "fly",
     "Trims stick forces away so the jet flies hands-off. Used all day.",
     "On the jet: the TRIM hat on the front of the grip. Give it a "
     "4-way hat."),
    (r"paddle", "fly",
     "Kicks the autopilot off. The 'get me out of this' switch.",
     "On the jet: the paddle behind the grip. Use a base or pinky lever."),
    (r"atc |automatic throttle|autothrottle|auto throttle", "fly",
     "Autothrottle: holds your approach speed for you — a big help on "
     "the ball.",
     "On the jet: a button on the inboard side of the left throttle."),
    (r"autopilot", "fly", "Autopilot master switch.", ""),
    (r"war emergency power", "fly",
     "WEP: emergency overboost. Minutes only, then the engine is scrap.",
     "On the jet: shove the throttle past the gate. Any throttle "
     "button."),
    (r"engine rpm", "fly",
     "Propeller RPM — set together with the throttle.",
     "On the jet: the blue lever. A second throttle lever or a rotary."),

    (r"wheel ?brake", "land",
     "Wheel brakes: hold to slow down.",
     "On the jet: toe brakes. A brake lever on the stick base stands in."),
    (r"anti ?skid", "land",
     "Anti-skid braking — and part of the Hornet's launch bar / hook "
     "logic.",
     "A console switch: keyboard is fine."),
    (r"parking brake", "land",
     "Parking brake: set before start, release before you taxi.",
     "A cockpit handle: keyboard is fine."),
    (r"speed ?brake|air ?brake", "land",
     "Airbrake: slows you down. Out on approach, in for the go-around.",
     "On the jet: under your left thumb on the throttle. A 2-way there."),
    (r"flap", "land",
     "Flaps — lift at low speed. HALF for takeoff, FULL in the landing "
     "pattern.",
     "On the jet: a lever on the left console. A spare 2-way switch on "
     "the throttle."),
    (r"gear", "land",
     "Landing gear up/down. Every jet has a gear limit speed — 250 kt in "
     "the Hornet.",
     "On the jet: the gear handle, left panel. A spare 2-way, or keyboard."),
    (r"nose ?wheel steer|undesignate", "land",
     "Steers the nosewheel on the ground, undesignates a target in the "
     "air.",
     "On the jet: the small button on the front of the grip, under your "
     "thumb."),
    (r"hook bypass", "land",
     "Tells the hook logic whether you are trapping at a FIELD or on the "
     "CARRIER.",
     "A cockpit switch: keyboard is fine."),
    (r"hook", "land",
     "Tailhook — down for a carrier trap or a field arrestment.",
     "On the jet: a handle on the right panel. A spare throttle "
     "switch."),
    (r"launch bar", "land",
     "Catapult launch bar: down to hook into the shuttle before the cat "
     "shot.",
     "On the jet: a switch on the left console. A spare throttle "
     "switch."),
    (r"catapult hook-?up", "land",
     "Hooks the jet onto the catapult shuttle on the boat.",
     "A ground-crew call: keyboard is fine."),
    (r"ball|lso", "land",
     "The 'ball' call to the LSO, three quarters of a mile behind the "
     "boat.",
     "A radio call: keyboard is fine."),

    (r"^\(\d+\) ", "fight",
     "Master mode selector: navigation, air-to-ground, gun modes.",
     "Number keys in the real jet too — a stick hat if you have room."),
    (r"gun trigger.*(first|1st)", "fight",
     "Trigger's first detent: gun camera only, no rounds.",
     "On the jet: the trigger, halfway. Stage 1 of a two-stage "
     "trigger."),
    (r"gun trigger|weapon fire|^cannon$|^fire\b", "fight",
     "Fires the gun or the selected weapon — the trigger's full pull.",
     "On the jet: the trigger. Your stick's trigger."),
    (r"weapon release|pickle", "fight",
     "Pickle button: releases the selected air-to-ground weapon.",
     "On the jet: the red button on top of the grip, under your thumb."),
    (r"select (gun|amraam|sidewinder|sparrow|missile)|weapon (select|"
     r"change)", "fight",
     "Jumps straight to that missile or the gun without touching an MFD.",
     "On the jet: the four-way switch on the head of the grip. A stick "
     "hat."),
    (r"master arm", "fight",
     "MASTER ARM — nothing leaves the jet until this says ARM.",
     "A guarded switch on the panel, not a HOTAS control: keyboard is "
     "fine."),
    (r"master mode.*a/a|air.to.air mode", "fight",
     "A/A master mode: sets the whole jet up for air-to-air in one "
     "press.",
     "Panel buttons under the HUD — often moved to spare throttle "
     "buttons."),
    (r"master mode.*a/g|air.to.ground mode", "fight",
     "A/G master mode: sets the jet up for bombing and strafing.",
     "Panel buttons under the HUD — often moved to spare throttle "
     "buttons."),
    (r"dispens|countermeasure|chaff|flare", "fight",
     "Countermeasures: chaff against radar missiles, flares against heat "
     "seekers.",
     "On the jet: the CMS switch, outboard side of the throttle. A "
     "4-way hat."),
    (r"jettison", "fight",
     "Jettison: throws stores off the jet. Emergency weight loss.",
     "A panel button: keyboard is fine."),
    (r"ecm|jamm", "fight", "Jammer — noise against enemy radars.",
     "A panel switch: keyboard is fine."),
    (r"gunsight|reticle|pipper|aiming", "fight",
     "Gunsight / aiming reticle setting.", ""),

    (r"sensor control switch", "sensors",
     "The 'castle': hands control to the radar, the pod, helmet or MFD.",
     "On the jet: the hat on TOP of the grip. A 4-way hat that presses."),
    (r"designator controller|^tdc", "sensors",
     "TDC: drives the radar and targeting cursor. Depress = designate.",
     "On the jet: the thumb slew on the left throttle — a mini-stick "
     "or hat."),
    (r"cage", "sensors",
     "Uncages a seeker so a Sidewinder or Maverick can look around on "
     "its own.",
     "On the jet: a button under your index finger on the throttle."),
    (r"radar elevation", "sensors",
     "Tilts the radar beam — you aim it at the altitude you expect the "
     "target.",
     "On the jet: the antenna wheel on the throttle. A rotary or an axis."),
    (r"target lock|lock target", "sensors",
     "Locks whatever sits under the aiming pipper.",
     "A thumb button on the grip in the real jet too."),
    (r"unlock", "sensors", "Breaks the lock and goes back to searching.",
     "Next to the lock button — keep the pair together."),
    (r"laser", "sensors",
     "Laser ranger / designator: needed for guided bombs and accurate "
     "gun ranging.",
     "A console switch — a spare throttle button works."),
    (r"raid|fov", "sensors",
     "Field of view / raid expand for the sensor you are driving.",
     "On the jet: a button on the throttle grip."),
    (r"display zoom", "sensors",
     "Zooms the sensor picture (not the camera) — the TV or radar "
     "display.", ""),
    (r"hmd|helmet", "sensors",
     "Helmet-mounted display: the brightness knob doubles as its "
     "on/off.",
     "A panel knob: keyboard is fine."),
    (r"electro.optical|shkval|i-251|flir|night vision|goggle|lltv",
     "sensors",
     "TV or infrared sensor: the picture you aim with, and see at night.",
     "A console switch: keyboard is fine; the cursor goes on the "
     "throttle."),
    (r"recce|event mark", "sensors",
     "Marks a reconnaissance point. Safe to ignore while you are "
     "learning.", ""),
    (r"channel selector|preset", "sensors",
     "Dials the radio's preset channel.",
     "A panel knob: keyboard is fine."),
    (r"comm|radio|voip|intercom|mids", "sensors",
     "Radio: push-to-talk and the comms menu — ATC, wingmen, tankers.",
     "On the jet: the radio switch on the throttle. Two buttons will "
     "do."),
    (r"waypoint|steer ?point|navigation mode", "sensors",
     "Steps through your waypoints / picks the navigation mode.",
     "Panel or UFC: keyboard is fine."),

    (r"zoom", "cockpit",
     "Zooms the view. You spot and read gauges with it.",
     "A spare axis (rotary or lever), or two buttons."),
    (r"kneeboard", "cockpit",
     "Kneeboard pages: checklists, charts and your own notes.",
     "Keyboard is fine."),
    (r"master caution", "cockpit",
     "Silences the master caution light once you have read what broke.",
     "On the jet: the light you punch on the panel. A spare button."),
    (r"canopy", "cockpit", "Opens and closes the canopy.",
     "Keyboard is fine."),
    (r"wing fold", "cockpit", "Folds the wings — carrier deck parking.",
     "Keyboard is fine."),
    (r"engine.*(start|stop|crank)|apu|electric power|battery|generator",
     "cockpit",
     "Startup switch, part of the cold-and-dark sequence.",
     "Console switches: keyboard is fine."),
    (r"exterior light|external light|position light|formation light",
     "cockpit",
     "External lights — on the boat this is also how you salute the "
     "catapult crew.",
     "On the jet: a fingertip switch on the throttle."),
    (r"light|illuminat|dimmer|brightness", "cockpit",
     "Lighting or display brightness.",
     "Panel knobs: keyboard is fine."),
    (r"hud", "cockpit", "HUD symbology or brightness.",
     "A panel knob: keyboard is fine."),
    (r"view|camera|snap", "cockpit",
     "Camera control — mostly redundant if you have head tracking.",
     "Head tracking, or a spare hat."),
    (r"mode", "fight", "Master mode / weapon mode selector.", ""),
]


def hint_for(name):
    """(theme, what it does, where it lives on the real aircraft)."""
    for pattern, theme, text, place in HINTS:
        if re.search(pattern, name, re.I):
            return theme, text, place
    return "other", "", ""


def where_text(info):
    """'the factory profiles put this on the stick' — derived, not guessed."""
    seen = info.get("where") or {}
    stick, throttle = seen.get("stick", 0), seen.get("throttle", 0)
    total = stick + throttle
    if not total:
        return ""
    if stick >= 2 * throttle:
        return "factory profiles: STICK (%d of %d)" % (stick, total)
    if throttle >= 2 * stick:
        return "factory profiles: THROTTLE (%d of %d)" % (throttle, total)
    return ("factory profiles: split, stick %d / throttle %d"
            % (stick, throttle))


def build_guide(commands):
    """hash -> {'theme', 'hint', 'where', 'note'} — what the table shows
    about a command besides its name."""
    guide = {}
    for h, c in commands.items():
        theme, hint, place = hint_for(c["name"])
        where = where_text(c)
        # a switch with a direction says which way it goes; how many
        # siblings it has is what separates a four-way hat from a plain
        # two-position toggle
        if re.search(r"hat|mini-stick", place, re.I):
            move = hat_move(c["name"], c.get("dir"))
            ways = c.get("ways", 0)
            if move and ways > 1:
                where = "%s: %s%s" % ("hat" if ways > 2 else "2-way switch",
                                      move,
                                      "  ·  " + where if where else "")
        guide[h] = {
            "theme": theme,
            "hint": hint or ("%s — no hint for this one yet"
                             % c.get("category", "?")),
            "place": place,
            "where": where,
            "note": "%d profiles" % c["votes"] if c["votes"] else "",
        }
    return guide


def essentials(commands, guide):
    """[(theme title, [(hash, name, kind)])]: the list to bind first.

    Which controls matter on a HOTAS is a question the game files already
    answer: every module ships a pile of factory profiles for real sticks
    and throttles, and a command that eleven of them put on a button is a
    command that belongs on a button. So this is just the module's own
    commands ranked by how many of its profiles bind them — no hardcoded
    per-aircraft table — led by the axes DCS hands to every device, and
    split into the order you actually learn an aircraft in.

    The cut is relative to the busiest command, because how many profiles
    a module ships (and how many of those are for panels that bind next
    to nothing) varies wildly from module to module.
    """
    order = {name: i for i, name in enumerate(CORE_AXES)}
    pinned = sorted((h for h, c in commands.items()
                     if c["kind"] == "axis" and c["name"] in order),
                    key=lambda h: order[commands[h]["name"]])
    pinned += sorted((h for h, c in commands.items() if h not in pinned
                      and _norm(c["name"]).startswith(CORE_EXTRA)),
                     key=lambda h: (-commands[h]["votes"],
                                    commands[h]["name"].lower()))[:2]
    top = max([c["votes"] for c in commands.values()] or [0])
    floor = max(2, -(-top // 3)) if top > 3 else 1
    rest = [h for h, c in commands.items()
            if c["votes"] >= floor and h not in pinned]
    rest.sort(key=lambda h: (-commands[h]["votes"],
                             commands[h]["name"].lower()))

    chosen = pinned + rest[:ESSENTIALS_MAX]
    sections = []
    for theme, title in THEMES:
        items = [(h, commands[h]["name"], commands[h]["kind"])
                 for h in chosen if guide[h]["theme"] == theme]
        if items:
            sections.append((title, items))
    return sections


def _int_keys(v):
    if isinstance(v, dict):
        return {(int(k) if isinstance(k, str) and k.isdigit() else k):
                _int_keys(x) for k, x in v.items()}
    return v


def parse_diff_lua(text):
    """Parse a DCS diff.lua into a Python dict (the format is regular
    enough for a regex translation to JSON)."""
    body = text.split("local diff =", 1)[1].rsplit("return diff", 1)[0]
    s = re.sub(r'\[\s*"((?:[^"\\]|\\.)*)"\s*\]\s*=', r'"\1":', body)
    s = re.sub(r"\[(\d+)\]\s*=", r'"\1":', s)
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return _int_keys(json.loads(s.strip()))


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


def build_diffs(results, aircraft, devs):
    """Modeled bindings overlaid on the last --sync snapshot (if any).

    Returns {role: {'axisDiffs': ..., 'keyDiffs': ...}}."""
    ac = results.get("aircraft", {}).get(aircraft, {})
    bindings = {h: r for h, r in ac.items()
                if r and not h.startswith("_")}
    if not bindings and not ac.get("_snapshot"):
        raise RuntimeError("nothing bound for %s yet" % aircraft)
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

    # overlay onto the snapshot of the installed state (--sync), so
    # bindings made in the DCS UI survive regeneration
    snapshot = ac.get("_snapshot") or {}
    for role in diffs:
        if role not in snapshot:
            continue
        merged = copy.deepcopy(snapshot[role])
        for table in ("axisDiffs", "keyDiffs"):
            merged.setdefault(table, {})
            merged[table].update(diffs[role][table])
        diffs[role] = merged
    return diffs


def render_diff(diff):
    """diff dict -> file content, byte-compatible with DCS's serializer
    (sorted keys, tab indent, no trailing newline)."""
    body = lua({"axisDiffs": diff.get("axisDiffs", {}),
                "keyDiffs": diff.get("keyDiffs", {})}, 1)
    return "local diff = %s\nreturn diff" % body


def joystick_dir(cfg, aircraft):
    return os.path.join(cfg["saved_games"], "Config", "Input",
                        input_id(cfg, aircraft), "joystick")


def generate(results, cfg, aircraft):
    """Build and install the per-device diff.lua files. Returns summary."""
    if dcs_running():
        raise RuntimeError("DCS is running — quit the game first "
                           "(it overwrites Config/Input on exit)")
    devs = resolve_devices(results)
    diffs = build_diffs(results, aircraft, devs)
    out_dir = joystick_dir(cfg, aircraft)
    os.makedirs(out_dir, exist_ok=True)
    lines = []
    for role, info in devs.items():
        path = os.path.join(out_dir, info["dcs_id"] + ".diff.lua")
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_diff(diffs[role]))
        n = (len(diffs[role]["axisDiffs"]) + len(diffs[role]["keyDiffs"]))
        lines.append("%s: %d entries -> %s" % (role, n, path))
    lines.append("backups: *.bak next to each file (when one existed)")
    stale = os.path.join(cfg["saved_games"], "Config", "Input", aircraft,
                         "joystick")
    if os.path.normpath(stale) != os.path.normpath(out_dir) and \
            os.path.isdir(stale):
        lines.append("note: DCS does not read %s — leftovers from an "
                     "older run there can be deleted" % stale)
    lines.append("Start DCS and check Options -> Controls -> %s" % aircraft)
    return lines


def sync(results, cfg, aircraft):
    """Import the installed diff.lua files back into the results file.

    Every entry the wizard can model becomes a normal binding (so the TUI
    shows it); the full parsed files are kept as a snapshot that
    build_diffs() overlays, so nothing tuned in the DCS UI is ever lost.
    Safe to run while DCS is running (read-only on game files).
    """
    devs = resolve_devices(results)
    out_dir = joystick_dir(cfg, aircraft)
    new = {}
    snapshot = {}
    lines = []
    for role, info in devs.items():
        path = os.path.join(out_dir, info["dcs_id"] + ".diff.lua")
        if not os.path.exists(path):
            lines.append("%s: no installed diff.lua — skipped" % role)
            continue
        parsed = parse_diff_lua(open(path, encoding="utf-8").read())
        snapshot[role] = parsed
        rev = {ABS_TO_DCS[c]: i for i, c in enumerate(info["axmap"])
               if c in ABS_TO_DCS}
        n = 0
        for table, kind in (("axisDiffs", "axis"), ("keyDiffs", "button")):
            for h, e in parsed.get(table, {}).items():
                items = e.get("added") or e.get("changed") or {}
                first = items.get(1) if isinstance(items, dict) else None
                key = (first or {}).get("key")
                if not key:
                    continue
                name = e.get("name", h)
                if kind == "axis":
                    if key not in rev:
                        continue
                    new[h] = {"name": name, "role": role, "type": "axis",
                              "index": rev[key],
                              "invert": bool((first.get("filter") or {})
                                             .get("invert"))}
                else:
                    m = re.match(r"JOY_BTN(\d+)$", key)
                    if not m:
                        continue          # keyboard-style combos stay
                    new[h] = {"name": name, "role": role, "type": "button",
                              "index": int(m.group(1)) - 1}
                n += 1
        lines.append("%s: %d bindings imported" % (role, n))
    # a device with no installed file says nothing about its bindings —
    # keep whatever the wizard already had for it rather than dropping it
    previous = results.setdefault("aircraft", {}).get(aircraft, {})
    kept = 0
    for h, r in previous.items():
        if r and not h.startswith("_") and r.get("role") not in snapshot:
            new.setdefault(h, r)
            kept += 1
    if kept:
        lines.append("kept %d binding(s) for devices with no installed "
                     "file" % kept)
    results["aircraft"][aircraft] = new
    new["_snapshot"] = snapshot

    # round-trip check: regenerating now must reproduce the files exactly
    diffs = build_diffs(results, aircraft, devs)
    for role, info in devs.items():
        if role not in snapshot:
            continue
        path = os.path.join(out_dir, info["dcs_id"] + ".diff.lua")
        same = render_diff(diffs[role]) == open(path, encoding="utf-8").read()
        lines.append("%s: round-trip %s" % (role,
                                            "OK" if same else "MISMATCH!"))
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
    mark = "? " if r.get("proposed") else ""
    if r["type"] == "button":
        return "%s%s BTN%d" % (mark, r["role"], r["index"] + 1)
    axmap = results.get("_devices", {}).get(r["role"], {}).get("axmap")
    key = ""
    if axmap and r["index"] < len(axmap) and axmap[r["index"]] in ABS_TO_DCS:
        key = " " + ABS_TO_DCS[axmap[r["index"]]]
    return "%s%s axis %d%s%s" % (mark, r["role"], r["index"], key,
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


def propose_into(bindings, results, path, commands, guide, used, tui):
    """Fill every unbound command from the hardware map.

    propose.py knows the shape of each control on the devices -- which buttons
    are one hat, which trigger stages are cumulative, what a little finger
    reaches -- and the module already says what each command wants. Seeding
    turns this screen from twenty-six presses into twenty-six confirmations.
    Anything already bound is left alone, and a proposal is marked `?` until
    you press a button over it.
    """
    if commands is None:
        return "propose: no commands loaded"
    try:
        import importlib.util
        import sys as _sys
        spec = importlib.util.spec_from_file_location(
            "dcspropose", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "propose.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        recs = mod.seed(_sys.modules[__name__], commands, guide)
    except SystemExit as e:
        return "propose: %s" % e
    except Exception as e:
        return "propose: %s (is ../sim-device-map there?)" % e

    added = 0
    for h, r in recs.items():
        if h in bindings:
            continue                      # never overwrite what you chose
        key = (r["role"], r["type"], r["index"])
        if r["type"] == "button" and key in used:
            continue                      # that button already carries something
        bindings[h] = r
        used.setdefault(key, r["name"])
        added += 1
    save(results, path)
    return ("proposed %d binding%s from the device map — marked ?, press a "
            "button over any you disagree with" % (added, "" if added == 1
                                                   else "s"))


def run_table(tui, devices, results, bindings, used, sections, path,
              heading, guide=None, commands=None):
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

    def draw(status=""):
        h, _ = tui.scr.getmaxyx()
        visible = max(4, h - 8)
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
                note = (guide or {}).get(a, {}).get("note", "")
                tui._put(y, 2, "%-44s %-13s %s" % (b[:44], note, current),
                         attr)
        # three rows that belong to the selected command and nothing
        # else — what it does, where it sits in the real aircraft, which
        # of your devices the factory profiles put it on. They stay put
        # while you are capturing a button, which is exactly when you are
        # staring at the stick wondering which switch this was.
        g = (guide or {}).get(rows[sel][1], {})
        for j, line in enumerate([g.get("hint", ""), g.get("place", ""),
                                  g.get("where", "")]):
            tui._put(h - 5 + j, 0, line)
        tui._put(h - 2, 0, status)          # prompt / result of the last key
        tui._put(h - 1, 0, "arrows = move, RETURN = bind, P = propose, "
                           "c/C = confirm one/all, I = invert, X = clear, "
                           "ESC = back")
        tui.scr.refresh()

    status = ""
    while True:
        draw(status)
        k = tui.key(0.5)
        if k is None:
            continue
        if k == "esc":
            return
        if k == "up":
            move(-1)
            status = ""
        elif k == "down":
            move(+1)
            status = ""
        elif k in ("p", "P"):
            status = propose_into(bindings, results, path, commands, guide,
                                  used, tui)
        elif k == "c":
            # accepting a proposal is a decision, not a capture: pressing the
            # button again just to agree with it would be the whole point lost
            _, h_, name, _ = rows[sel]
            r = bindings.get(h_)
            if not r:
                status = "%s: nothing to confirm" % name
            elif not r.get("proposed"):
                status = "%s: already yours" % name
            else:
                r.pop("proposed")
                save(results, path)
                status = "%s: confirmed — %s" % (name, describe(results, r))
                move(+1)
        elif k == "C":
            n = 0
            for _what, h_, _n, _k in [r for r in rows if r[0] == "item"]:
                r = bindings.get(h_)
                if r and r.get("proposed"):
                    r.pop("proposed")
                    n += 1
            if n:
                save(results, path)
            status = ("confirmed %d proposal%s in this section"
                      % (n, "" if n == 1 else "s")) if n else \
                     "nothing left to confirm here"
        elif k in ("x", "X"):
            _, h_, name, _ = rows[sel]
            bindings.pop(h_, None)
            for snap in (bindings.get("_snapshot") or {}).values():
                for table in ("axisDiffs", "keyDiffs"):
                    snap.get(table, {}).pop(h_, None)
            save(results, path)
            status = "%s: cleared (DCS default, if any, comes back)" % name
        elif k in ("i", "I"):
            _, h_, name, _ = rows[sel]
            r = bindings.get(h_)
            if r and r["type"] == "axis":
                r["invert"] = not r.get("invert")
                save(results, path)
                status = "%s: %s" % (name, describe(results, r))
        elif k == "enter":
            _, h_, name, kind = rows[sel]
            while True:
                prompt = ("move the axis you want for: %s"
                          if kind == "axis"
                          else "press the button you want for: %s") % name
                draw("-> %s   (ESC = cancel)" % prompt)
                got = wait_input(devices, want_axis=(kind == "axis"),
                                 tui=tui)
                if got == "skip":
                    status = "%s: unchanged" % name
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
                    draw("-> captured: %s %s%s  %s"
                         % (d.role, label, dup, opts))
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
                         "index": number}      # no `proposed`: you pressed it
                    if etype == "axis":
                        r["invert"] = invert
                    bindings[h_] = r
                    save(results, path)
                    status = "%s: %s" % (name, describe(results, r))
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
        bindings_clean = {h: r for h, r in bindings.items()
                          if r and not h.startswith("_")}
        display = aircraft_all[aircraft]["display"]

        used = {}
        for h, r in bindings_clean.items():
            if r["type"] == "button":
                used.setdefault((r["role"], "button", r["index"]), r["name"])

        guide = build_guide(commands)
        ess_sections = essentials(commands, guide)
        choice = tui.menu("dcs-bind-wizard — %s" % display, [
            progress_label("Essential binds — start here",
                           *aircraft_stats(bindings_clean, ess_sections)),
            progress_label("All controls, by section",
                           *aircraft_stats(bindings_clean, sections)),
            "Generate diff.lua files",
            "Change aircraft",
            "Quit",
        ])
        if choice in (None, 4):
            return
        if choice == 3:
            aircraft = None
            continue
        if choice == 0:
            run_table(tui, active, results, bindings, used, ess_sections,
                      args.results, "%s — essential binds" % display,
                      guide=guide, commands=commands)
            continue
        if choice == 2:
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
                      args.results, heading, guide=guide, commands=commands)


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
    ap.add_argument("-s", "--sync", action="store_true",
                    help="import the installed diff.lua files into the "
                         "results file (absorbs changes made in the DCS "
                         "UI) and exit")
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

    if args.generate or args.sync:
        aircraft = args.aircraft or cfg.get("aircraft")
        if not aircraft:
            sys.exit("Pass --aircraft (e.g. -a su-25T).")
        try:
            if args.sync:
                lines = sync(results, cfg, aircraft)
                results["_config"] = cfg
                save(results, args.results)
            else:
                lines = generate(results, cfg, aircraft)
            for line in lines:
                print(line)
        except (RuntimeError, OSError, ValueError) as e:
            sys.exit("ERROR: %s" % e)
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("Run this in a regular terminal (the wizard is a TUI), "
                 "or use --generate for headless generation.")
    curses.wrapper(tui_main, args, results, cfg)


if __name__ == "__main__":
    main()
