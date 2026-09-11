# dcs-bind-wizard

Interactive HOTAS bindings wizard + `diff.lua` generator for **DCS World on
Linux** (Steam/Proton). Sibling of
[elite-dangerous-bind-wizard](../elite-dangerous-bind-wizard) — same
capture-driven TUI, pointed at DCS.

## What it does

- discovers installed aircraft modules straight from the game folder
- harvests every bindable command (hash + name + category) by running the
  module's own `default.lua` through a Lua interpreter with the game's
  globals stubbed out, and fills in the sim's engine commands (whose ids
  live in the exe) from the factory joystick profiles — no hardcoded
  function lists
- **Essentials**: the short list to bind first, ranked by how many of the
  factory HOTAS profiles shipped with the module bind each command — a
  Hornet opens on trigger, trim, sensor control, TDC and gear instead of
  on 849 cockpit switches, and it works the same for a module bought
  tomorrow. The list is grouped in the order you learn an aircraft (fly
  it → take off and land → fight with it → sensors and radio), and the
  selected row explains itself in three lines: what the control does,
  where it sits in the real aircraft and what kind of hardware it wants
  ("the hat on TOP of the grip", "toe brakes — a brake lever stands in",
  "a guarded panel switch: keyboard is fine"), and whether the factory
  profiles put it on the stick or on the throttle — the last one counted
  from those profiles, which know the real HOTAS layout: castle switch
  on the stick 8/8, TDC and cage/uncage on the throttle. Switches with a
  direction also say which way they go — `hat: PULL back (nose up)`,
  `hat: FORWARD, away from you` for Select Sparrow — read off the
  module's own symbols (`STICK_WEAPON_SELECT_FWD`) where the display name
  does not carry it, with four-way hats told from two-position toggles by
  counting the switch's siblings. Those three lines never move: they stay
  up while you are pressing the button, and the prompt and the result of
  the last key get their own line
- full-screen curses wizard: pick an aircraft, bind the essentials (or
  pick one of the game's own sections, or ALL), then chain RETURN → press
  a button / move an axis → RETURN row by row; `I` inverts an axis, `X`
  clears a binding, ESC backs out
- generates one `<Device> {GUID}.diff.lua` per device into
  `Saved Games/DCS/Config/Input/<unit>/joystick/` (device GUIDs are read
  from `dcs.log`), byte-compatible with DCS's own serializer. `<unit>` is
  the name DCS itself saves under, taken from the module's `entry.lua`:
  it is not always the module's own Input folder — the Hornet ships
  `Input/FA-18C` but saves to `Config/Input/FA-18C_hornet`
- cleans up DCS's per-device defaults (pitch/roll/rudder/thrust and
  fire/weapon-change/cannon are assigned to *every* joystick), so a stick
  and a throttle never fight over the same axis
- refuses to write while DCS is running (the game overwrites those files
  on exit) and backs up existing files as `*.bak`

## Usage

```bash
./dcs-bind-wizard.py                 # TUI wizard (auto-detects Steam paths)
./dcs-bind-wizard.py --game-dir ~/.local/share/Steam/steamapps/common/DCSWorld
./dcs-bind-wizard.py -g -a su-25T    # headless: (re)generate diff.lua files
./dcs-bind-wizard.py -s -a su-25T    # sync: absorb changes made in DCS's UI
./dcs-bind-wizard.py --reset         # start from scratch
```

Wizard state lives in `dcs-bind-wizard-results.json` next to the script and
is saved after every change, so quitting at any time is safe.

After tuning things in the DCS UI (new binds, curves, inversions), run
`--sync`: every entry the wizard can model becomes a regular binding, and
the parsed files are kept as a snapshot that generation overlays — so
nothing set in-game is ever lost, byte-for-byte (sync verifies the
round-trip and reports OK/MISMATCH per device).

## Notes

- The full command list needs a `lua` (or `luajit`) binary on PATH — it
  only ever runs the module's `default.lua`, which is data. Without one
  the wizard falls back to the factory profiles alone: fewer commands,
  and whatever name the profile author used (sometimes Russian).
- The sim's own commands (pitch, thrust, gear, views, …) carry ids that
  only the running game knows, so their hashes still have to come from a
  shipped profile that binds them. A handful never do; bind those
  in-game.
- Axis names assume Wine's HID-usage mapping (`ABS_X → JOY_X`, …,
  `ABS_THROTTLE/ABS_RUDDER → JOY_SLIDER1/2`).
- Defaults tuning applied on generation: pitch/roll curvature 0.12 +
  deadzone 0.03 (Eagle Dynamics' own VPC WarBRD profile values), rudder
  0.15/0.05, thrust as slider.

MIT license.
