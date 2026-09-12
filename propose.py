#!/usr/bin/env python3
"""Propose a HOTAS layout for a DCS module, instead of asking for 26 presses.

Both halves of the answer already exist and had never been joined.

The module knows what each command WANTS: `dcs-bind-wizard.py` reads the
factory joystick profiles that ship with it, which gives every command a vote
count (how many profiles bind it), a device (stick or throttle, counted), and a
sentence about the hardware it belongs on -- "the hat on TOP of the grip. A
4-way hat", "the paddle behind the grip", "the trigger", "keyboard is fine".

../sim-device-map knows what the hardware HAS: which buttons form one hat,
which trigger stages are cumulative, what a little finger reaches without
regripping.

So: classify the sentence into a shape, match it against a control of that
shape, hardest-wanted first. The capture flow is still there for anything you
disagree with -- this only means you confirm rather than invent.

    ./propose.py -a FA-18C            # the proposed layout
    ./propose.py -a FA-18C --check    # against what you already bound
    ./propose.py -a FA-18C --why      # and why each control was chosen
"""

import argparse
import collections
import datetime
import importlib.util
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
#: the shared hardware map. Sibling directory by default; SIM_DEVICE_MAP
#: overrides it, for a clone that does not sit next to this one.
MAP = os.environ.get('SIM_DEVICE_MAP') or os.path.normpath(
    os.path.join(HERE, '..', 'sim-device-map'))
if not os.path.isdir(MAP):
    raise SystemExit(
        f'no device map at {MAP}\n'
        'clone sim-device-map next to this repo, or set SIM_DEVICE_MAP')
if MAP not in sys.path:
    sys.path.insert(0, MAP)
import devicemap                                            # noqa: E402


def wizard():
    """The wizard itself, imported for its harvest -- it is the thing that
    knows how to read a module's commands and profiles."""
    spec = importlib.util.spec_from_file_location(
        'dcswiz', os.path.join(HERE, 'dcs-bind-wizard.py'))
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ['dcs-bind-wizard']
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    finally:
        sys.argv = argv
    return mod


#: What the module's own prose is asking for. Ordered: first match wins, so
#: the specific phrases sit above the general ones.
SHAPES = [
    (r'keyboard is fine',                       None),
    (r"\btrigger\b",                            'trigger'),
    (r'\bpaddle\b',                             'paddle'),
    (r'slew|mini-?stick|thumb slew',            'ministick'),
    (r'4-way hat|four-way|\bhat\b',             'hat4'),
    (r'rotary|antenna wheel|\bknob\b',          'dial'),
    (r'2-position|two-position|2-way|two-way',  'hat2'),
    (r'toe brakes|brake lever',                 'axis'),
    (r'spare axis|the throttle lever|its [XY] axis|twist', 'axis'),
]


def wants(cmd, g):
    """(shape, device or None, must be reachable in flight)."""
    place = (g.get('place') or '').lower()
    ways = cmd.get('ways') or 0
    shape = None
    for pattern, s in SHAPES:
        if re.search(pattern, place):
            shape = s
            break
    else:
        shape = 'button'
    # The command's own kind wins over the prose, both ways. "toe brakes. A
    # brake lever on the stick base" is the place for BOTH `Wheel Brake` (an
    # axis) and `Wheel Brake - ON/OFF` (a button), and each wants its own kind
    # of home.
    if shape is not None:
        if cmd['kind'] == 'axis':
            shape = 'axis'
        elif shape == 'axis':
            shape = 'hat2' if (cmd.get('ways') or 0) > 1 else 'button'
    if shape == 'hat4' and 1 < ways <= 2:
        shape = 'hat2'                  # its siblings say it is a 2-way switch
    if shape is None:
        return None, None, False

    where = g.get('where') or ''
    dev = ('stick' if 'STICK' in where else
           'throttle' if 'THROTTLE' in where else None)
    urgency = URGENCY.get(g.get('theme'), 3)
    if GRIP_PROSE.search(place):
        urgency = 0
    # "on the throttle" is where it lives on the real jet, not a claim that you
    # must reach it mid-manoeuvre; only fingers and the grip mean that
    # The prose says where a control sits on the real jet; the theme says
    # whether you touch it with a MiG on your tail. Lights are a fingertip
    # switch on the throttle and still have no business on a thumb hat.
    return shape, dev, urgency


def _drop_combined(members, cmds):
    """A switch family often ships its positions AND a command that does both:
    `- UP`, `- DOWN` and `- UP/DOWN`. Binding the pair makes the combined one
    redundant, and it would eat a third slot on a two-position switch.
    """
    tails = {}
    for h in members:
        name = cmds[h]['name']
        tail = name.rsplit(' - ', 1)[-1] if ' - ' in name else name
        tails[tail.strip().lower()] = h
    drop = set()
    for tail, h in tails.items():
        if '/' in tail:
            parts = [p.strip() for p in tail.split('/')]
            if all(p in tails for p in parts):
                drop.add(h)
    return [h for h in members if h not in drop] or members


def families(cmds, guide, chosen):
    """Group a hat's four commands into one thing to place.

    A four-way hat reaches the wizard as four separate commands; placing them
    one at a time would scatter them across four unrelated buttons.
    """
    groups, singles = collections.OrderedDict(), []
    for h in chosen:
        c = cmds[h]
        fam = c.get('family')
        shape, dev, urgency = wants(c, guide[h])
        if shape is None:
            continue
        # Siblings of one physical switch belong together whatever shape they
        # were classified as. Left and right engine cutoff are a pair: split
        # across two devices they are worse than anywhere together, because
        # your hand has to learn two places for one idea.
        if fam and (c.get('ways') or 0) > 1 and shape in ('hat4', 'hat2',
                                                          'button'):
            key = (fam, shape)
            groups.setdefault(key, {'shape': shape, 'dev': dev,
                                    'urgency': urgency, 'members': [],
                                    'votes': 0})
            g = groups[key]
            g['members'].append(h)
            g['votes'] = max(g['votes'], c['votes'])
        else:
            singles.append((h, shape, dev, urgency, c['votes']))
    def label_for(fam, members):
        """`switch_family` strips the direction words, which sometimes leaves
        nothing useful: the engine cutoff pair comes out called "throttle".
        The names themselves read better."""
        names = [cmds[h]['name'] for h in members]
        head = names[0]
        for n in names[1:]:
            i = 0
            while i < min(len(head), len(n)) and head[i] == n[i]:
                i += 1
            head = head[:i]
        head = head.rstrip(' -(,')
        return head if len(head) >= 4 else fam

    out = []
    for (fam, shape), g in groups.items():
        members = _drop_combined(g['members'], cmds)
        fam = label_for(fam, members)
        if shape == 'button' and len(members) > 1:
            # a pair of buttons that are one switch wants one switch
            shape = 'hat2' if len(members) == 2 else 'hat4'
        out.append({'what': fam, 'shape': shape, 'dev': g['dev'],
                    'urgency': g['urgency'], 'members': members,
                    'votes': g['votes']})
    for h, shape, dev, urgency, votes in singles:
        out.append({'what': cmds[h]['name'], 'shape': shape, 'dev': dev,
                    'urgency': urgency, 'members': [h], 'votes': votes})
    # most urgent first, and only then by how many factory profiles agree
    out.sort(key=lambda x: (x['urgency'], -x['votes']))
    return out


AXIS_FOR = {'Pitch': 'stick-y', 'Roll': 'stick-x', 'Rudder': 'twist',
            'Thrust': 'throttle-lever'}

#: The module says which way a switch goes in its own words; the map says which
#: way each button points. Joining them is what stops a trim hat coming out
#: scrambled -- both halves were there and the first version zipped them in
#: arbitrary order.
MOVE_TO_DIR = [
    (r'press|depress',                          'push'),
    # sideways first: "INBOARD (towards your left)" contains "towards you",
    # which would otherwise read as aft and scramble the whole hat
    (r'inboard|\bleft\b',                       'left'),
    (r'outboard|\bright\b',                     'right'),
    (r'forward|fwd|away from you|\bpush\b',     'up'),
    (r'\baft\b|towards you\b|\bpull\b|\bback\b',  'down'),
    (r'\bup\b|climb',                           'up'),
    (r'\bdown\b|descend',                       'down'),
]

#: Starting a cold aircraft needs controls no factory profile bothers to bind,
#: because a profile is written for a jet that is already running. They sit
#: below the vote floor and would never reach the essentials on their own.
COLD_START = [
    r'throttle \((left|right)\).*off\(hold\)',
    r'engine crank switch - (left|right)$',
    r'apu control sw',
]


#: a two-stage trigger names its detents
STAGE_WORDS = [(r'first|1st', 0), (r'second|2nd', 1), (r'third|3rd', 2)]


def direction_of(module, cmd):
    move = module.hat_move(cmd['name'], cmd.get('dir')) or cmd['name']
    for pattern, d in MOVE_TO_DIR:
        if re.search(pattern, move, re.I):
            return d
    return None


def stage_of(cmd):
    for pattern, i in STAGE_WORDS:
        if re.search(pattern, cmd['name'], re.I):
            return i
    return None


def lay_out(module, ctrl, members, cmds, press_only=False, borrowed=None):
    """hash -> button, respecting which way each one points."""
    if borrowed is not None and len(members) == 1:
        return {members[0]: borrowed}
    if press_only and ctrl.push is not None and len(members) == 1:
        return {members[0]: ctrl.push}
    out, left = {}, []
    used = set()
    dirs = list(ctrl.dirs or ctrl.stages or ctrl.positions or [])
    for h in members:
        c = cmds[h]
        want = None
        if ctrl.kind == 'trigger':
            i = stage_of(c)
            if i is not None and i < len(ctrl.buttons):
                want = ctrl.buttons[i]
        else:
            d = direction_of(module, c)
            if d == 'push' and ctrl.push is not None:
                want = ctrl.push
            elif d and d in dirs:
                want = ctrl.buttons[dirs.index(d)]
        if want is not None and want not in used:
            out[h] = want
            used.add(want)
        else:
            left.append(h)
    spare = [b for b in ctrl.bindable_buttons if b not in used]
    for h, b in zip(left, spare):
        out[h] = b
    return out


def resolve_axis(devs, need, cmds):
    """Which axis on which device a flight or slew command belongs to."""
    name = cmds[need['members'][0]]['name']
    want = AXIS_FOR.get(name.split(' - ')[0].strip())
    if want == 'stick-x':
        return ('stick', devs['stick'].axes(kind='stick-x'))
    if want == 'stick-y':
        return ('stick', devs['stick'].axes(kind='stick-y'))
    if want == 'twist':
        return ('stick', devs['stick'].axes(kind='twist'))
    low = name.lower()
    # Two levers, two engines. A cold start in the Hornet runs them up one at a
    # time, so the combined Thrust axis is not enough -- and binding it as well
    # would have both fighting for the same engines.
    if low.startswith('thrust'):
        levers = devs['throttle'].axes(kind='lever')
        side = ('right' if 'right' in low else
                'left' if 'left' in low else None)
        if side:
            match = [a for a in levers if side in (a.label or '').lower()]
            return ('throttle', match or levers[:1])
        return ('throttle', [])          # combined: superseded by the pair
    if want == 'throttle-lever':
        return ('throttle', [a for a in devs['throttle'].axes(kind='lever')
                             if 'left' in (a.label or '').lower()])
    if 'designator' in low or 'slew' in low:
        # the clue is in the name: it hangs off the throttle
        g = next(iter(devs['throttle'].groups('ministick')), None)
        if g and len(g.axes) == 2:
            i = 1 if re.search(r'vert', low) else 0
            return ('throttle', [devs['throttle'].axis(g.axes[i])])
    if 'brake' in low:
        return ('stick', [a for a in devs['stick'].axes()
                          if a.safe_for_absolute])
    if 'zoom' in low:
        return ('throttle', [a for a in devs['throttle'].axes(kind='dial')])
    # a warbird flies on three levers: throttle, propeller RPM, mixture
    if re.search(r'\brpm\b|prop(eller)? pitch|mixture|supercharger', low):
        spare = [a for a in devs['throttle'].axes()
                 if a.kind in ('lever', 'slider', 'dial')
                 and 'throttle lever' not in (a.label or '').lower()]
        # steadiest first: something that stays where you leave it
        spare.sort(key=lambda a: (a.kind != 'lever', a.index))
        return ('throttle', spare[:1])
    return (None, [])


#: WHEN you touch a command, from the module's own theme. This is the thing
#: that decides how good a home it deserves -- and the wizard already works it
#: out. The first version read the word "finger" out of a sentence describing
#: where a switch sits on the real jet, which is a different question, and
#: every fix to it moved the symptom somewhere else.
URGENCY = {'fight': 0, 'fly': 0,        # in a turn
           'land': 1,                   # on approach, hands busy but there is time
           'sensors': 2,                # in the air
           'cockpit': 3, 'other': 3}    # on the ramp

#: how precious a control is, from the map's own words
REACH_TIER = [('thumb', 0), ('index finger', 0),
              ('without releasing', 1),
              ('needs letting go', 3)]

#: the worst reach an urgency can live with...
MAX_REACH = {0: 1, 1: 3, 2: 1, 3: 3}
#: ...and the best it may take. Without a floor, something you do on the ramp
#: grabs a pinky lever the moment the panel buttons run out, and the fallback
#: pass -- which can see spare positions on idle rockers -- never gets a look.
MIN_REACH = {0: 0, 1: 0, 2: 0, 3: 2}


#: When the module says a command sits ON THE GRIP, the aircraft's own
#: designers already answered this question -- your hand is there anyway. That
#: beats the theme, which only says when you touch it.
#:
#: Deliberately narrow: "a fingertip switch on the throttle" describes the
#: throttle body, not the grip, and exterior lights have no business on a thumb
#: hat however fingertip-operated they are.
GRIP_PROSE = re.compile(
    r'on the grip|under your thumb|front of the grip|top of the grip'
    r'|behind the grip|the grip in the real jet', re.I)


def reach_tier(ctrl):
    for word, tier in REACH_TIER:
        if word in (ctrl.reach or ''):
            return tier
    return 2                             # unknown: somewhere in between


#: what may stand in for what, when the exact shape is gone or too small
FITS = {
    'hat2': ('hat2', 'switch2', 'switch3', 'hat4', 'selector'),
    'hat4': ('hat4', 'hat8', 'selector'),
    'button': ('button', 'paddle'),
    'paddle': ('paddle', 'button'),
    'trigger': ('trigger',),
    'ministick': ('ministick',),
    'dial': ('dial', 'encoder'),
}


def score(ctrl, need, role):
    ok = FITS.get(need['shape'], (need['shape'],))
    if ctrl.kind not in ok:
        return None
    if len(ctrl.bindable_buttons) < len(need['members']):
        return None
    tier = reach_tier(ctrl)
    if not (MIN_REACH[need['urgency']] <= tier <= MAX_REACH[need['urgency']]):
        return None                      # wrong kind of home for this
    # Take the LEAST precious control that still does the job. Needs are placed
    # most-urgent first, so the thumb positions are already spoken for by the
    # time anything from the ramp gets a look -- and it has no reason to want
    # one anyway.
    s = 100 + 12 * tier
    if need['dev'] == role:
        s += 40
    elif need['dev'] and need['dev'] != role:
        s -= 50
    if ctrl.kind == need['shape']:
        s += 20                          # the shape it actually asked for
    s -= 4 * (len(ctrl.bindable_buttons) - len(need['members']))
    return s


def results_path():
    return os.path.join(HERE, 'dcs-bind-wizard-results.json')


def load_cfg(module, game_dir=None):
    """The wizard remembers where the game is in the results file; reuse that
    rather than making you pass it again."""
    try:
        cfg = dict(json.load(open(results_path()))['_config'])
    except (OSError, KeyError):
        cfg = {}
    if game_dir:
        cfg['game_dir'] = game_dir
    if not cfg.get('game_dir'):
        sys.exit('pass --game-dir, or run the wizard once so it remembers')
    return cfg


def candidates(module, cmds, guide):
    """The commands worth placing: the module's own essentials, plus the few
    a cold start needs that no factory profile bothers to bind."""
    chosen = [h for _t, items in module.essentials(cmds, guide)
              for h, _n, _k in items]
    for h, c in cmds.items():
        if h in chosen:
            continue
        if any(re.search(p, c['name'], re.I) for p in COLD_START):
            chosen.append(h)
    return chosen


def place(module, cmds, guide, chosen):
    """[(need, (role, control) or (role, [axis]) or None, score)], unplaced."""
    devs = {d.kind: d for d in devicemap.load_all()}
    pool = [(role, c) for role, d in devs.items()
            for c in d.groups(bindable=True)]
    taken, out, unplaced = set(), [], []
    needs = families(cmds, guide, chosen)

    # A trigger has stages and more than one command wants it: on the Su-25T
    # the cannon and the selected weapon both belong there, lighter pull first.
    # Letting the first comer take the whole control put the jet's main fire
    # command on a hat direction.
    trigger_needs = [n for n in needs if n['shape'] == 'trigger'
                     and len(n['members']) == 1]
    if len(trigger_needs) > 1:
        spot = next(((i, r, c) for i, (r, c) in enumerate(pool)
                     if c.kind == 'trigger'), None)
        if spot:
            i, role, ctrl = spot
            taken.add(i)
            free = list(ctrl.buttons)
            named, rest = {}, []
            for n in trigger_needs:
                st = stage_of(cmds[n['members'][0]])
                if st is not None and st < len(free) and free[st] not in named:
                    named[free[st]] = n
                else:
                    rest.append(n)
            rest.sort(key=lambda n: -n['votes'])
            for n in rest:
                spare = [b for b in free if b not in named]
                if not spare:
                    break
                named[spare[0]] = n
            for b, n in named.items():
                n['borrowed'] = b
                out.append((n, (role, ctrl), 200))
                needs.remove(n)

    for need in needs:
        if need['shape'] == 'axis':
            out.append((need, resolve_axis(devs, need, cmds), None))
            continue
        best, best_s = None, None
        for i, (role, c) in enumerate(pool):
            if i in taken:
                continue
            s = score(c, need, role)
            if s is not None and (best_s is None or s > best_s):
                best, best_s = i, s
        if best is None:
            unplaced.append(need)
            continue
        taken.add(best)
        role, c = pool[best]
        out.append((need, (role, c), best_s))

    # A hat carries its directions AND a press. If the need that took it had
    # nothing for the press, that press is still a button -- and a cold-start
    # switch is exactly the sort of thing that should have one rather than
    # nothing. What counts as taken is where the buttons actually LANDED, not
    # how many commands a need had: a single command on a multi-button control
    # goes on its press.
    occupied = set()
    for n, p, _ in out:
        if not p or n['shape'] == 'axis':
            continue
        role, ctrl = p
        for b in lay_out(module, ctrl, n['members'], cmds,
                         press_only=len(n['members']) == 1
                         and len(ctrl.bindable_buttons) > 1,
                         borrowed=n.get('borrowed')).values():
            occupied.add((role, b))
    still = []
    for need in sorted(unplaced, key=lambda n: (n['urgency'], -n['votes'])):
        if len(need['members']) != 1:
            still.append(need)
            continue
        best, best_s, best_btn = None, None, None
        for i, (role, c) in enumerate(pool):
            # a trigger's stages are the gun, and a selector's positions are one
            # switch: their spare capacity is not spare
            if c.kind in ('trigger', 'selector'):
                continue
            tier = reach_tier(c)
            if not (MIN_REACH[need['urgency']] <= tier
                    <= MAX_REACH[need['urgency']]):
                continue
            # a press nobody claimed, or a spare position on a control that
            # nothing took at all -- four idle two-way rockers should not sit
            # there while a cold-start switch goes homeless
            spare = [b for b in c.bindable_buttons if (role, b) not in occupied]
            if not spare:
                continue
            btn = c.push if (c.push is not None
                             and (role, c.push) not in occupied) else spare[0]
            whole = i not in taken
            sc = 60 + 12 * tier + (30 if need['dev'] == role else 0)
            sc -= 15 if whole else 0     # prefer borrowing over opening a new one
            if best_s is None or sc > best_s:
                best, best_s, best_btn = (role, c), sc, btn
        if best is None:
            still.append(need)
            continue
        occupied.add((best[0], best_btn))
        need['borrowed'] = best_btn
        out.append((need, best, best_s))
    return out, still


def propose(module, key, game_dir=None):
    cfg = load_cfg(module, game_dir)
    ac = module.discover_aircraft(cfg)
    if key not in ac:
        sys.exit(f'no such module: {key} (have {", ".join(ac)})')
    cmds = module.harvest_commands(cfg, key, ac[key]['factory_dir'])
    guide = module.build_guide(cmds)
    out, unplaced = place(module, cmds, guide, candidates(module, cmds, guide))
    return cmds, guide, out, unplaced


def seed(module, cmds, guide, chosen=None):
    """{command hash: the same record the capture screen writes}.

    Marked `proposed` so the screen can show what you have not confirmed yet;
    capturing over one drops the mark.
    """
    if chosen is None:
        chosen = candidates(module, cmds, guide)
    out, _unplaced = place(module, cmds, guide, chosen)
    recs = {}
    for need, pair, _s in out:
        if not pair:
            continue
        role, what = pair
        if need['shape'] == 'axis':
            axes = what
            if not axes:
                continue
            name = cmds[need['members'][0]]['name']
            recs[need['members'][0]] = {
                'name': name, 'role': role, 'type': 'axis',
                'index': axes[0].index, 'invert': _inverted(name),
                'proposed': True}
            continue
        ctrl = what
        spots = lay_out(module, ctrl, need['members'], cmds,
                        press_only=len(need['members']) == 1
                        and len(ctrl.bindable_buttons) > 1,
                        borrowed=need.get('borrowed'))
        for h, b in spots.items():
            recs[h] = {'name': cmds[h]['name'], 'role': role,
                       'type': 'button', 'index': b, 'proposed': True}
    return recs


def _inverted(name):
    """Pitch wants inverting everywhere: stick back is nose up."""
    return name.split(' - ')[0].strip() == 'Pitch'


def _esc(t):
    return (str(t).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


def bound_rows(module, key, cmds, guide):
    """What is actually bound, joined with what the hardware map calls it.

    The results file is the source of truth, not the proposal: half of it is
    yours by the time you read a sheet.
    """
    binds = json.load(open(results_path()))['aircraft'].get(key, {})
    devs = {d.kind: d for d in devicemap.load_all()}
    rows = {'stick': [], 'throttle': [], 'axes': []}
    for h, r in binds.items():
        if not isinstance(r, dict) or 'role' not in r:
            continue
        d = devs.get(r['role'])
        theme = (guide.get(h) or {}).get('theme', '')
        mark = '?' if r.get('proposed') else ''
        if r['type'] == 'axis':
            a = d.axis(r['index']) if d else None
            g = d.axis_group(r['index']) if d else None
            label = (g.label if g else (a.label if a else f'axis {r["index"]}'))
            rows['axes'].append((r['role'], r['index'], label, r['name'],
                                 ' (inverted)' if r.get('invert') else '', mark))
            continue
        g = d.group_of(r['index']) if d else None
        part = g.direction(r['index']) if g else ''
        label = g.label if g else f'button {r["index"]}'
        rows[r['role']].append((r['index'], label, part, r['name'], theme, mark))
    for k in ('stick', 'throttle'):
        rows[k].sort()
    rows['axes'].sort()
    return rows, devs


def unbound(module, cmds, guide, key):
    binds = json.load(open(results_path()))['aircraft'].get(key, {})
    out = []
    for _t, items in module.essentials(cmds, guide):
        for h, name, kind in items:
            if h not in binds:
                out.append((name, kind, cmds[h]['votes']))
    out.sort(key=lambda x: -x[2])
    return out


def write_sheet(module, key, cmds, guide, path):
    rows, devs = bound_rows(module, key, cmds, guide)
    L = [f'# Kneeboard — {key}', '',
         'What sits under which finger. Button numbers are the ones DCS shows,',
         'one higher than the OS number the device map uses.', '',
         '**Generated** by `./propose.py -a %s --sheet` from the results file'
         % key,
         'and `../sim-device-map`. A `?` is proposed and not yet confirmed.', '']
    for role in ('stick', 'throttle'):
        d = devs.get(role)
        if not rows[role]:
            continue
        L += [f'## {d.product if d else role}', '',
              '| Control | DCS | Command |', '|---|---|---|']
        for idx, label, part, name, theme, mark in rows[role]:
            what = label + (f' — {part}' if part else '')
            L.append(f'| {what} | `BTN{idx + 1}` | {name}'
                     f'{" ?" if mark else ""} |')
        L.append('')
    if rows['axes']:
        L += ['## Axes', '', '| Control | Axis | Command |', '|---|---|---|']
        for role, idx, label, name, inv, mark in rows['axes']:
            L.append(f'| {label} | `{role} {idx}` | {name}{inv}'
                     f'{" ?" if mark else ""} |')
        L.append('')
    left = unbound(module, cmds, guide, key)
    if left:
        L += ['## Still unbound', '',
              'Essentials with no control yet, most-wanted first.', '']
        for name, kind, votes in left:
            L.append(f'- {name} — {kind}, {votes} factory profiles')
        L.append('')
    open(path, 'w', encoding='utf-8').write('\n'.join(L))
    return path, sum(len(rows[k]) for k in rows)


def write_html(module, key, cmds, guide, path):
    import datetime
    rows, devs = bound_rows(module, key, cmds, guide)

    def panel(role):
        d = devs.get(role)
        body = []
        for idx, label, part, name, theme, mark in rows[role]:
            what = _esc(label) + (f' <em>{_esc(part)}</em>' if part else '')
            unsure = ' <em>?</em>' if mark else ''
            body.append(f'<tr><td class="c">{what}</td>'
                        f'<td class="n">{idx + 1}</td>'
                        f'<td>{_esc(name)}{unsure}</td></tr>')
        return (f'<div class="panel"><h2>{_esc(d.product if d else role)}'
                f'<small>DCS button numbers</small></h2><table>'
                f'<tr><th>Control</th><th>BTN</th><th class="a">Command</th>'
                f'</tr>' + ''.join(body) + '</table></div>')

    arows = ''.join(
        f'<tr><td class="c">{_esc(label)}</td>'
        f'<td class="n">{_esc(role[:3])} {idx}</td>'
        f'<td>{_esc(name)}{_esc(inv)}{" <em>?</em>" if mark else ""}</td></tr>'
        for role, idx, label, name, inv, mark in rows['axes'])
    axes_panel = ('<div class="panel"><h2>Axes</h2><table><tr><th>Control</th>'
                  '<th>#</th><th class="a">Command</th></tr>'
                  + arows + '</table></div>')

    left = unbound(module, cmds, guide, key)
    urows = ''.join(
        f'<tr><td class="c">{_esc(n)}</td><td class="n">{v}</td>'
        f'<td class="h">{_esc(k)}</td></tr>' for n, k, v in left[:18])
    unbound_panel = ('<div class="panel"><h2>Still unbound<small>most-wanted'
                     ' first</small></h2><table><tr><th>Command</th>'
                     '<th>Profiles</th><th>Kind</th></tr>'
                     + (urows or '<tr><td colspan="3">nothing</td></tr>')
                     + '</table></div>')

    tpl = open(os.path.join(HERE, 'sheet-template.html'), encoding='utf-8').read()
    n = sum(len(rows[k]) for k in rows)
    out = (tpl.replace('__MODULE__', _esc(key))
              .replace('__STICK__', panel('stick'))
              .replace('__THROTTLE__', panel('throttle'))
              .replace('__AXES__', axes_panel)
              .replace('__UNBOUND__', unbound_panel)
              .replace('__STAMP__', f'generated {datetime.date.today()} '
                                    f'· {n} bindings'))
    open(path, 'w', encoding='utf-8').write(out)
    return path, n


def reseed(module, key, cmds, guide):
    """Throw an aircraft's bindings away and lay it out fresh.

    A results file that has been through several device configurations and two
    generations of this matcher is sediment: an old capture numbered against
    hardware that has since been reconfigured, a combined Thrust axis left
    beside the split pair that replaced it, an inversion nobody meant. Patching
    those one at a time is slower and less certain than starting over, because
    everything the proposal knows is now better than what is there.

    Everything comes out marked `proposed`, so the wizard still asks you to
    confirm it -- this replaces the guesses, not your judgement.
    """
    path = results_path()
    data = json.load(open(path))
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = f'{path}.bak.{stamp}'
    shutil.copy2(path, backup)

    before = len(data['aircraft'].get(key, {}))
    recs = seed(module, cmds, guide)
    data.setdefault('aircraft', {})[key] = recs
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    return backup, before, len(recs)


def audit(module, key, cmds, guide):
    """Bindings that no longer match the hardware.

    An old capture is numbered against the device as it was configured then.
    Reconfigure a VIRPIL and the numbers stay put while the buttons move, so a
    binding can end up on a control that does something else -- or on one the
    firmware reports with nothing behind it.
    """
    binds = json.load(open(results_path()))['aircraft'].get(key, {})
    devs = {d.kind: d for d in devicemap.load_all()}
    out = []

    # two commands on one axis is nearly always a leftover: they drive the same
    # surface from the same lever and fight over it
    by_axis = {}
    for h, r in binds.items():
        if isinstance(r, dict) and r.get('type') == 'axis':
            by_axis.setdefault((r['role'], r['index']), []).append(r)
    for (role, idx), rs in by_axis.items():
        if len(rs) > 1:
            others = ', '.join(x['name'] for x in rs[1:])
            out.append((rs[0]['name'], rs[0],
                        f'shares this axis with {others}'))
        # the same lever cannot need inverting for one command and not another
        if len({bool(x.get('invert')) for x in rs}) > 1:
            out.append((rs[0]['name'], rs[0],
                        'inverted for some commands on this axis, not others'))

    for h, r in binds.items():
        if not isinstance(r, dict) or 'role' not in r:
            continue
        d = devs.get(r['role'])
        if not d:
            continue
        if r['type'] == 'axis':
            if d.axis(r['index']) is None:
                out.append((r['name'], r, 'no such axis on this device'))
            continue
        g = d.group_of(r['index'])
        if g is None:
            out.append((r['name'], r, 'no such button'))
        elif not g.bindable:
            out.append((r['name'], r,
                        f'{g.kind} — {g.label}: nothing is behind it'))
        elif h in cmds:
            shape, _dev, _urg = wants(cmds[h], guide.get(h, {}))
            if shape and shape != 'axis' and g.kind != shape:
                # a hat direction is a fine home for a plain button; the other
                # way round is what is worth saying
                if shape in ('paddle', 'trigger', 'ministick'):
                    out.append((r['name'], r,
                                f'wants a {shape}, sits on {g.kind} '
                                f'"{g.label}"'
                                + (f' [{g.direction(r["index"])}]'
                                   if g.direction(r['index']) else '')))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-a', '--aircraft', default='FA-18C')
    ap.add_argument('--game-dir', help='where DCS is installed')
    ap.add_argument('--why', action='store_true')
    ap.add_argument('--sheet', nargs='?', const='', metavar='PATH',
                    help='write KNEEBOARD-<module>.md from what is bound')
    ap.add_argument('--html', nargs='?', const='', metavar='PATH',
                    help='the same, laid out in columns for a second screen')
    ap.add_argument('--reseed', action='store_true',
                    help='throw this module\'s bindings away and lay it out '
                         'fresh (backs the results file up first)')
    ap.add_argument('--audit', action='store_true',
                    help='bindings that no longer match the hardware')
    ap.add_argument('--check', action='store_true',
                    help='compare against dcs-bind-wizard-results.json')
    args = ap.parse_args()

    module = wizard()

    if args.reseed:
        cfg = load_cfg(module, args.game_dir)
        ac = module.discover_aircraft(cfg)
        keys = [args.aircraft] if args.aircraft else list(ac)
        for k in keys:
            if k not in ac:
                sys.exit(f'no such module: {k} (have {", ".join(ac)})')
        first = True
        for k in keys:
            cmds = module.harvest_commands(cfg, k, ac[k]['factory_dir'])
            guide = module.build_guide(cmds)
            backup, before, after = reseed(module, k, cmds, guide)
            if first:
                print(f'  backed up to {os.path.basename(backup)}')
                first = False
            print(f'  {k:9s} {before} bindings -> {after}, all marked ?')
        print('  open the wizard and walk the list: c confirms one, C the '
              'section')
        return

    if args.audit:
        cfg = load_cfg(module, args.game_dir)
        ac = module.discover_aircraft(cfg)
        bad = []
        keys = [args.aircraft] if args.aircraft else list(ac)
        for k in keys:
            if k not in ac:
                continue
            cmds = module.harvest_commands(cfg, k, ac[k]['factory_dir'])
            guide = module.build_guide(cmds)
            for name, r, why in audit(module, k, cmds, guide):
                bad.append((k, name, r, why))
        for k, name, r, why in bad:
            where = (f'{r["role"]} BTN{r["index"] + 1}'
                     if r['type'] == 'button'
                     else f'{r["role"]} axis {r["index"]}')
            print(f'  {k:8s} {name[:44]:46s} {where:16s} {why}')
        print(f'\n  {len(bad)} binding(s) worth a second look')
        return

    if args.sheet is not None or args.html is not None:
        cfg = load_cfg(module, args.game_dir)
        ac = module.discover_aircraft(cfg)
        cmds = module.harvest_commands(cfg, args.aircraft,
                                       ac[args.aircraft]['factory_dir'])
        guide = module.build_guide(cmds)
        if args.sheet is not None:
            path = args.sheet or os.path.join(
                HERE, f'KNEEBOARD-{args.aircraft}.md')
            p, n = write_sheet(module, args.aircraft, cmds, guide, path)
            print(f'wrote {p}: {n} bindings')
        if args.html is not None:
            path = args.html or os.path.join(
                HERE, f'kneeboard-{args.aircraft}.html')
            p, n = write_html(module, args.aircraft, cmds, guide, path)
            print(f'wrote {p}: {n} bindings')
        return

    cmds, guide, out, unplaced = propose(module, args.aircraft, args.game_dir)

    placed = [(n, p, s) for n, p, s in out if p]
    print(f'{args.aircraft}: {len(placed)} controls proposed, '
          f'{len(unplaced)} unplaced\n')
    for need, pair, s in out:
        if need['shape'] == 'axis':
            role, axs = pair if pair else (None, [])
            a = axs[0] if axs else None
            name0 = cmds[need['members'][0]]['name'].lower()
            if a:
                where = f'{role} axis {a.index} — {a.label}'
            elif name0 == 'thrust':
                where = 'left unbound — the two engines are bound separately'
            else:
                where = 'no axis on this hardware'
            print(f'  {need["what"][:38]:40s} {where}')
            print()
            continue
        if pair is None:
            continue
        role, c = pair
        print(f'  {need["what"][:38]:40s} {role:8s} {c.kind:9s} {c.label}')
        spots = lay_out(module, c, need['members'], cmds,
                        press_only=len(need['members']) == 1
                        and len(c.bindable_buttons) > 1,
                        borrowed=need.get('borrowed'))
        for h in need['members']:
            b = spots.get(h)
            where = c.direction(b) if b is not None else '?'
            print(f'      {cmds[h]["name"][:46]:48s} -> {str(b):>3s} '
                  f'{where}')
        if args.why:
            print(f'      wants {need["shape"]}'
                  + (f', {need["dev"]}' if need['dev'] else '')
                  + f", {['in a turn', 'on approach', 'in the air',
                            'on the ramp'][need['urgency']]}"
                  + f'   {need["votes"]} factory profiles   score {s}')
            print(f'      {guide[need["members"][0]]["place"][:74]}')
        print()

    if unplaced:
        print(f'{len(unplaced)} found no control:')
        for n in unplaced:
            print(f'  {n["what"][:40]:42s} wanted {n["shape"]}'
                  + (f' on the {n["dev"]}' if n['dev'] else ''))

    if args.check:
        have = json.load(open(results_path()))['aircraft'].get(
            args.aircraft, {})
        mine = {}
        for need, pair, _ in out:
            if not pair:
                continue
            role, c = pair
            for h, b in lay_out(module, c, need['members'], cmds,
                                press_only=len(need['members']) == 1
                                and len(c.bindable_buttons) > 1).items():
                mine[h] = (role, b)
        same = diff = 0
        print('\n--- against what you bound by hand ---')
        for h, v in have.items():
            if not isinstance(v, dict) or v.get('type') != 'button':
                continue
            if h not in mine:
                continue
            got = (v['role'], v['index'])
            if got == mine[h]:
                same += 1
            else:
                diff += 1
                print(f'  {v["name"][:40]:42s} you: {got[0]} {got[1]:<3} '
                      f'proposed: {mine[h][0]} {mine[h][1]}')
        print(f'\n  {same} identical, {diff} different, '
              f'{len(mine)} proposed in total')


if __name__ == '__main__':
    main()
