# dcs-bind-wizard

Interactive HOTAS bindings wizard + `diff.lua` generator for **DCS World on
Linux** (Steam/Proton). Sibling of
[elite-dangerous-bind-wizard](../elite-dangerous-bind-wizard) — same
capture-driven TUI, pointed at DCS.

## What it does

- discovers installed aircraft modules straight from the game folder
- harvests every bindable command (hash + name) from the factory joystick
  profiles shipped with each module, and groups them into the game's own
  categories parsed from `default.lua` — no hardcoded function lists
- full-screen curses wizard: pick an aircraft, pick a section (or ALL),
  then chain RETURN → press a button / move an axis → RETURN row by row;
  `I` inverts an axis, `X` clears a binding, ESC backs out
- generates one `<Device> {GUID}.diff.lua` per device into
  `Saved Games/DCS/Config/Input/<aircraft>/joystick/` (device GUIDs are
  read from `dcs.log`), byte-compatible with DCS's own serializer
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

- Command coverage comes from the factory profiles, so a command nobody
  ever put in a shipped profile will not appear; bind those in-game.
- Axis names assume Wine's HID-usage mapping (`ABS_X → JOY_X`, …,
  `ABS_THROTTLE/ABS_RUDDER → JOY_SLIDER1/2`).
- Defaults tuning applied on generation: pitch/roll curvature 0.12 +
  deadzone 0.03 (Eagle Dynamics' own VPC WarBRD profile values), rudder
  0.15/0.05, thrust as slider.

MIT license.
