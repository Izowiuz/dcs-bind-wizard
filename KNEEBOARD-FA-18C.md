# Kneeboard — FA-18C

What sits under which finger. Button numbers are the ones DCS shows,
one higher than the OS number the device map uses.

**Generated** by `./propose.py -a FA-18C --sheet` from the results file
and `../sim-device-map`. A `?` is proposed and not yet confirmed.

## R-VPC Stick WarBRD-D

| Control | DCS | Command |
|---|---|---|
| Main trigger — second | `BTN4` | Gun Trigger - SECOND DETENT (Press to shoot) |
| Thumb top button | `BTN7` | Undesignate/Nose Wheel Steer Switch |
| Top thumb hat — push | `BTN8` | RECCE Event Mark Switch |
| Top thumb hat — up | `BTN9` | Trimmer Switch - PUSH(DESCEND) |
| Top thumb hat — left | `BTN10` | Trimmer Switch - LEFT WING DOWN |
| Top thumb hat — down | `BTN11` | Trimmer Switch - PULL(CLIMB) |
| Top thumb hat — right | `BTN12` | Trimmer Switch - RIGHT WING DOWN |
| Thumb bottom button | `BTN13` | Weapon Release Button |
| Bottom thumb hat — up | `BTN15` | Select Sparrow |
| Bottom thumb hat — left | `BTN16` | Select AMRAAM |
| Bottom thumb hat — down | `BTN17` | Select Gun |
| Bottom thumb hat — right | `BTN18` | Select Sidewinder |
| Grip thumb hat — push | `BTN23` | Sensor Control Switch - Depress |
| Grip thumb hat — up | `BTN24` | Sensor Control Switch - Fwd |
| Grip thumb hat — left | `BTN25` | Sensor Control Switch - Left |
| Grip thumb hat — down | `BTN26` | Sensor Control Switch - Aft |
| Grip thumb hat — right | `BTN27` | Sensor Control Switch - Right |
| Paddle button | `BTN32` | Autopilot/Nosewheel Steering Disengage (Paddle) Switch |

## L-VPC VMAX Prime Throttle

| Control | DCS | Command |
|---|---|---|
| Pinky button | `BTN1` | ATC Engage/Disengage Switch |
| Middle finger button | `BTN3` | Master Mode Button - A/A |
| Middle finger hat — push | `BTN4` | COMM Switch - COMM 1 (call radio menu) |
| Middle finger hat — up | `BTN5` | COMM Switch - COMM 2 (call radio menu) |
| Middle finger hat — right | `BTN6` | COMM Switch - MIDS A |
| Middle finger hat — down | `BTN7` | COMM Switch - MIDS B |
| Thumb two-way hat — push | `BTN10` | RAID/FLIR FOV Select Button |
| Thumb two-way hat — forward | `BTN11` | Dispense Switch - Aft(FLARE)/Center(OFF) |
| Thumb two-way hat — back | `BTN13` | Dispense Switch - Forward(CHAFF)/Center(OFF) |
| Thumb mini-stick — push | `BTN15` | Throttle Designator Controller - Depress |
| Thumb button | `BTN16` | Master Mode Button - A/G |
| Bottom thumb button | `BTN17` | Cage/Uncage Button |
| Keyboard B1 button | `BTN23` | Launch Bar Control Switch - EXTEND |
| Keyboard B2 button | `BTN24` | Launch Bar Control Switch - RETRACT |
| Keyboard B3 button | `BTN25` | Wheel Brake - ON/OFF |
| Keyboard B4 button | `BTN26` | MASTER CAUTION Reset Button |
| Keyboard B5 button | `BTN27` | Exterior Lights Switch - OFF |
| Keyboard B6 button | `BTN28` | Exterior Lights Switch - ON |
| T1 rocker — up | `BTN30` | Arresting Hook Handle - Up |
| T1 rocker — down | `BTN31` | Arresting Hook Handle - Down |
| T2 rocker — up | `BTN32` | Speed Brake Switch - EXTEND |
| T3 rocker — up | `BTN34` | Speed Brake Switch - RETRACT |
| T4 rocker — up | `BTN36` | Landing Gear Control Handle - UP |
| T4 rocker — down | `BTN37` | Landing Gear Control Handle - DOWN |
| T5 rocker — up | `BTN38` | Throttle (Left) - OFF(hold)<>IDLE |
| T5 rocker — down | `BTN39` | Throttle (Right) - OFF(hold)<>IDLE |
| Mode selector — 4 | `BTN48` | FLAP Switch - Up |
| Mode selector — 3 | `BTN49` | FLAP Switch - AUTO |
| Mode selector — 2 | `BTN50` | FLAP Switch - HALF |
| Mode selector — 1 | `BTN51` | FLAP Switch - FULL |

## Axes

| Control | Axis | Command |
|---|---|---|
| Main stick, left/right | `stick 0` | Roll |
| Main stick, fore/aft | `stick 1` | Pitch (inverted) |
| Stick twist | `stick 2` | Rudder |
| Analogue lever on the grip | `stick 5` | Wheel Brake |
| Thumb mini-stick | `throttle 0` | Throttle Designator Controller - Horizontal Axis |
| Thumb mini-stick | `throttle 1` | Throttle Designator Controller - Vertical Axis |
| Left throttle lever | `throttle 2` | Thrust Left |
| Right throttle lever | `throttle 3` | Thrust Right |
| Right side dial | `throttle 6` | Zoom View |

## Still unbound

Essentials with no control yet, most-wanted first.

- Radar Elevation Control — axis, 6 factory profiles
- Emergency Jettison Button — button, 5 factory profiles
- HMD OFF/BRT Knob — axis, 5 factory profiles
- UFC COMM 1 Channel Selector Knob - CCW/Decrease — button, 5 factory profiles
- UFC COMM 1 Channel Selector Knob - CW/Increase — button, 5 factory profiles
- UFC COMM 1 Channel Selector Knob - PULL — button, 5 factory profiles
- UFC COMM 2 Channel Selector Knob - CCW/Decrease — button, 5 factory profiles
- UFC COMM 2 Channel Selector Knob - CW/Increase — button, 5 factory profiles
- UFC COMM 2 Channel Selector Knob - PULL — button, 5 factory profiles
- Landing Gear Control Handle - UP/DOWN — button, 4 factory profiles
- Master Arm Switch - ARM — button, 4 factory profiles
- Master Arm Switch - SAFE — button, 4 factory profiles
- HMD OFF/BRT Knob - CCW/Decrease — button, 4 factory profiles
- HMD OFF/BRT Knob - CW/Increase — button, 4 factory profiles
- Wing Fold Control Handle - FOLD — button, 4 factory profiles
- Wing Fold Control Handle - HOLD — button, 4 factory profiles
- Wing Fold Control Handle - PULL/STOW — button, 4 factory profiles
- Wing Fold Control Handle - SPREAD — button, 4 factory profiles
- Thrust — axis, 2 factory profiles
