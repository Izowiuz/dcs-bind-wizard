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
import importlib.util
import json
import os
import re
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
    # "on the throttle" is where it lives on the real jet, not a claim that you
    # must reach it mid-manoeuvre; only fingers and the grip mean that
    reflex = bool(re.search(r'\bgrip\b|thumb|finger|trigger|paddle|slew',
                            place))
    return shape, dev, reflex


def families(cmds, guide, chosen):
    """Group a hat's four commands into one thing to place.

    A four-way hat reaches the wizard as four separate commands; placing them
    one at a time would scatter them across four unrelated buttons.
    """
    groups, singles = collections.OrderedDict(), []
    for h in chosen:
        c = cmds[h]
        fam = c.get('family')
        shape, dev, reflex = wants(c, guide[h])
        if shape is None:
            continue
        if fam and (c.get('ways') or 0) > 1 and shape in ('hat4', 'hat2'):
            key = (fam, shape)
            groups.setdefault(key, {'shape': shape, 'dev': dev,
                                    'reflex': reflex, 'members': [],
                                    'votes': 0})
            g = groups[key]
            g['members'].append(h)
            g['votes'] = max(g['votes'], c['votes'])
        else:
            singles.append((h, shape, dev, reflex, c['votes']))
    out = []
    for (fam, shape), g in groups.items():
        out.append({'what': fam, 'shape': shape, 'dev': g['dev'],
                    'reflex': g['reflex'], 'members': g['members'],
                    'votes': g['votes']})
    for h, shape, dev, reflex, votes in singles:
        out.append({'what': cmds[h]['name'], 'shape': shape, 'dev': dev,
                    'reflex': reflex, 'members': [h], 'votes': votes})
    out.sort(key=lambda x: -x['votes'])
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


def lay_out(module, ctrl, members, cmds, press_only=False):
    """hash -> button, respecting which way each one points."""
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
    return (None, [])


def score(ctrl, need, role):
    if ctrl.kind != need['shape']:
        if not (need['shape'] == 'hat2' and ctrl.kind == 'switch2'):
            return None
    if len(ctrl.bindable_buttons) < len(need['members']):
        return None
    regrip = ctrl.reach == 'needs letting go'
    if need['reflex'] and regrip:
        return None
    s = 100
    if need['dev'] == role:
        s += 40
    elif need['dev'] and need['dev'] != role:
        s -= 50
    if not regrip:
        s += 15
    s -= 3 * (len(ctrl.bindable_buttons) - len(need['members']))
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

    for need in families(cmds, guide, chosen):
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
                         and len(ctrl.bindable_buttons) > 1).values():
            occupied.add((role, b))
    still = []
    for need in sorted(unplaced, key=lambda n: -n['votes']):
        if len(need['members']) != 1 or need['shape'] not in ('button', 'hat2'):
            still.append(need)
            continue
        best, best_s = None, None
        for i, (role, c) in enumerate(pool):
            if c.push is None or (role, c.push) in occupied:
                continue
            regrip = c.reach == 'needs letting go'
            if need['reflex'] and regrip:
                continue
            sc = 60 + (30 if need['dev'] == role else 0)
            # borrowing is for leftovers: a control you must let go to reach is
            # the RIGHT home for a cold-start switch, and keeps the thumb spots
            # free for things you need in the air
            sc += 25 if (regrip and not need['reflex']) else 0
            if best_s is None or sc > best_s:
                best, best_s = (role, c), sc
        if best is None:
            still.append(need)
            continue
        occupied.add((best[0], best[1].push))
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
                        and len(ctrl.bindable_buttons) > 1)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-a', '--aircraft', default='FA-18C')
    ap.add_argument('--game-dir', help='where DCS is installed')
    ap.add_argument('--why', action='store_true')
    ap.add_argument('--sheet', nargs='?', const='', metavar='PATH',
                    help='write KNEEBOARD-<module>.md from what is bound')
    ap.add_argument('--html', nargs='?', const='', metavar='PATH',
                    help='the same, laid out in columns for a second screen')
    ap.add_argument('--check', action='store_true',
                    help='compare against dcs-bind-wizard-results.json')
    args = ap.parse_args()

    module = wizard()

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
                        and len(c.bindable_buttons) > 1)
        for h in need['members']:
            b = spots.get(h)
            where = c.direction(b) if b is not None else '?'
            print(f'      {cmds[h]["name"][:46]:48s} -> {str(b):>3s} '
                  f'{where}')
        if args.why:
            print(f'      wants {need["shape"]}'
                  + (f', {need["dev"]}' if need['dev'] else '')
                  + (', in flight' if need['reflex'] else '')
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
