"""evdiff.py - event-level diff between two FL Studio .flp files (used to reverse-engineer the format).
    python3 evdiff.py BEFORE.flp AFTER.flp
"""
import os, sys, difflib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flp_inspect import read_events
def show(e):
    eid, d = e
    if eid < 192: return f"{eid} = {int.from_bytes(d,'little')}"
    if eid in (192,193,194,195,196,197,198,199,203,204,205,206,207,231,239,241,201,202):
        try: return f"{eid} '{d.decode('utf-16-le').rstrip(chr(0))}'"
        except: pass
    return f"{eid} [{len(d)}] {d.hex()}"
a, ha = read_events(sys.argv[1]); b, hb = read_events(sys.argv[2])
print(ha, hb)
sa = [show(e) for e in a]; sb = [show(e) for e in b]
sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
for op, i1, i2, j1, j2 in sm.get_opcodes():
    if op == 'equal': continue
    print(f"--- {op} a[{i1}:{i2}] b[{j1}:{j2}]  (context before: {sa[i1-1][:60] if i1 else ''})")
    for x in sa[i1:i2]: print("  - " + x[:400])
    for x in sb[j1:j2]: print("  + " + x[:400])
