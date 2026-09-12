# Kneeboard — FA-18C

What sits under which finger. Button numbers are the ones DCS shows,
one higher than the OS number the device map uses.

**Generated** by `./propose.py -a FA-18C --sheet` from the results file
and `../sim-device-map`. A `?` is proposed and not yet confirmed.

## R-VPC Stick WarBRD-D

| Control | DCS | Command |
|---|---|---|
| Main trigger — second | `BTN4` | Gun Trigger - SECOND DETENT (Press to shoot) |
| Mini-stick — push | `BTN6` | Exterior Lights Switch - OFF |
| Thumb top button | `BTN7` | Weapon Release Button |
| Top thumb hat — push | `BTN8` | Exterior Lights Switch - ON |
| Top thumb hat — up | `BTN9` | Trimmer Switch - PUSH(DESCEND) |
| Top thumb hat — left | `BTN10` | Trimmer Switch - LEFT WING DOWN |
| Top thumb hat — down | `BTN11` | Trimmer Switch - PULL(CLIMB) |
| Top thumb hat — right | `BTN12` | Trimmer Switch - RIGHT WING DOWN |
| Bottom thumb hat — up | `BTN15` | Select Gun |
| Bottom thumb hat — left | `BTN16` | Select AMRAAM |
| Bottom thumb hat — down | `BTN17` | Select Sidewinder |
| Bottom thumb hat — right | `BTN18` | Select Sparrow |
| Grip thumb hat — push | `BTN23` | Sensor Control Switch - Depress |
| Grip thumb hat — up | `BTN24` | Sensor Control Switch - Fwd |
| Grip thumb hat — left | `BTN25` | Sensor Control Switch - Left |
| Grip thumb hat — down | `BTN26` | Sensor Control Switch - Aft |
| Grip thumb hat — right | `BTN27` | Autopilot/Nosewheel Steering Disengage (Paddle) Switch |
| Grip pinky button | `BTN31` | RECCE Event Mark Switch |

## L-VPC VMAX Prime Throttle

| Control | DCS | Command |
|---|---|---|
| Pinky button | `BTN1` | Cage/Uncage Button |
| Left side dial — push | `BTN2` | COMM Switch - MIDS B |
| Middle finger button | `BTN3` | Wheel Brake - ON/OFF |
| Middle finger hat — up | `BTN5` | FLAP Switch - HALF |
| Middle finger hat — down | `BTN7` | FLAP Switch - FULL |
| Thumb two-way hat — push | `BTN10` | RAID/FLIR FOV Select Button |
| Thumb two-way hat — forward | `BTN11` | Speed Brake Switch - EXTEND |
| Thumb two-way hat — back | `BTN13` | Speed Brake Switch - RETRACT |
| Thumb mini-stick — push | `BTN15` | Throttle Designator Controller - Depress |
| Thumb button | `BTN16` | ATC Engage/Disengage Switch |
| Bottom thumb button | `BTN17` | Arresting Hook Handle - Up |
| Keyboard B1 button | `BTN23` | Master Mode Button - A/A |
| Keyboard B2 button | `BTN24` | Master Mode Button - A/G |
| Keyboard B3 button | `BTN25` | COMM Switch - COMM 1 (call radio menu) |
| Keyboard B4 button | `BTN26` | COMM Switch - COMM 2 (call radio menu) |
| Keyboard B5 button | `BTN27` | Launch Bar Control Switch - EXTEND |
| Keyboard B6 button | `BTN28` | Launch Bar Control Switch - RETRACT |
| Big red button | `BTN29` | MASTER CAUTION Reset Button |
| T5 rocker — up | `BTN38` | Landing Gear Control Handle - DOWN |
| T5 rocker — down | `BTN39` | Landing Gear Control Handle - UP |
| APU button | `BTN40` | COMM Switch - MIDS A |
| E1 encoder — push | `BTN41` | Throttle (Left) - OFF(hold)<>IDLE |
| E2 encoder — push | `BTN44` | Throttle (Right) - OFF(hold)<>IDLE |

## Axes

| Control | Axis | Command |
|---|---|---|
| Main stick, left/right | `stick 0` | Roll |
| Main stick, fore/aft | `stick 1` | Pitch (inverted) |
| Stick twist | `stick 2` | Rudder |
| Analogue lever on the grip | `stick 5` | Wheel Brake |
| Thumb mini-stick | `throttle 0` | Throttle Designator Controller - Horizontal Axis |
| Thumb mini-stick | `throttle 1` | Throttle Designator Controller - Vertical Axis |
| Left throttle lever | `throttle 2` | Thrust |
| Left throttle lever | `throttle 2` | Thrust Left (inverted) |
| Right throttle lever | `throttle 3` | Thrust Right (inverted) |
| Right side dial | `throttle 6` | Zoom View |

## Still unbound

Essentials with no control yet, most-wanted first.

- Undesignate/Nose Wheel Steer Switch — button, 11 factory profiles
- Sensor Control Switch - Right — button, 10 factory profiles
- Dispense Switch - Aft(FLARE)/Center(OFF) — button, 8 factory profiles
- Arresting Hook Handle - Down — button, 7 factory profiles
- Dispense Switch - Forward(CHAFF)/Center(OFF) — button, 7 factory profiles
- Radar Elevation Control — axis, 6 factory profiles
- Emergency Jettison Button — button, 5 factory profiles
- HMD OFF/BRT Knob — axis, 5 factory profiles
- UFC COMM 1 Channel Selector Knob - CCW/Decrease — button, 5 factory profiles
- UFC COMM 1 Channel Selector Knob - CW/Increase — button, 5 factory profiles
- UFC COMM 1 Channel Selector Knob - PULL — button, 5 factory profiles
- UFC COMM 2 Channel Selector Knob - CCW/Decrease — button, 5 factory profiles
- UFC COMM 2 Channel Selector Knob - CW/Increase — button, 5 factory profiles
- UFC COMM 2 Channel Selector Knob - PULL — button, 5 factory profiles
- FLAP Switch - AUTO — button, 4 factory profiles
- FLAP Switch - Up — button, 4 factory profiles
- Landing Gear Control Handle - UP/DOWN — button, 4 factory profiles
- Master Arm Switch - ARM — button, 4 factory profiles
- Master Arm Switch - SAFE — button, 4 factory profiles
- HMD OFF/BRT Knob - CCW/Decrease — button, 4 factory profiles
- HMD OFF/BRT Knob - CW/Increase — button, 4 factory profiles
- Wing Fold Control Handle - FOLD — button, 4 factory profiles
- Wing Fold Control Handle - HOLD — button, 4 factory profiles
- Wing Fold Control Handle - PULL/STOW — button, 4 factory profiles
- Wing Fold Control Handle - SPREAD — button, 4 factory profiles
