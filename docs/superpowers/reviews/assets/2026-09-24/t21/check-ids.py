#!/usr/bin/env python3
"""T21 id check for the UI/UX Snow White review.

Reads one file, 2026-09-24-ui-ux-snow-white-review.md:
  - every section except T21 (T1-T20, with T11 merged), for the id universe;
  - the T21 section, for the ranked table, the candidate plans and the routing.

Checks on the ranked table (header '| # | ID | ...'):
  - one row per F-* id, and the ID column equals the set of F-* ids
    mentioned anywhere in the review doc outside T21. Ids that are
    mentioned but never given a definition line (a '##'-'#####' heading or a
    '**' line starting with the id)
    are withdrawn or unused (F-T8-10, F-T15-2);
  - every ID cited anywhere in T21 is known (typo guard);
  - each row's Sev equals the severity written in its finding's own section
    (first token of its '- **Severity:**' bullet), or '—' when the section gives
    none (merged "not counted", withdrawn, unused);
  - ranked rows carry one of the four groups, ranks run 1..n, and rows are
    sorted by severity (S1, S2, S3, D), then effort (S, M, L), then id.
Checks on the unranked rows ('#' is '—'):
  - a row whose 'Duplicates merged' cell starts with "unused" is exactly
    one of the mentioned-only ids above, and every mentioned-only id has
    such a row;
  - every other unranked row names at least one F-* id in that cell, each id
    it names is a ranked row, and that ranked row's cell names it back.
Checks on routing ('Lands in' column, candidate plans, P-routing table):
  - every 'Lands in' cell is '—' on an unranked row; on a ranked row it is
    one or more '; '-separated segments, each "<plan> T<n>[, T<n>...]" with
    optional ranges "T<a>–T<b>" (a <= b) and an optional trailing
    "(part)", and nothing else;
  - every target it names is a real task row: a P1-P5 target in that plan
    file's '| ID | Task |' table, a CP target in that candidate plan's task
    table;
  - a ranked row whose group is 'behaviour defect → P-plan' lands first in
    a P-plan and is a row of the P-routing table with the same Sev and the
    same 'Lands in' (ignoring "(part)"); every other ranked row lands first
    in a CP plan, is in that plan's 'Findings covered' list, and is cited by
    id in every CP task row its 'Lands in' names;
  - every ranked id is covered exactly once (one CP list or the routing
    table), each 'Findings covered (N)' count is right, and every candidate
    plan has 1-29 task rows with no repeated ID;
  - T21 is the last '## ' section of the doc, and no '<!--' followed by
    optional whitespace (newlines included) and PENDING is left anywhere from
    its heading to the end of the file.
Section boundaries ('## ' headings) ignore lines inside ``` or ~~~ fences. A
fence closes only on a line of the same character, at least as long, indented
at most 3 columns more than its opener; a backtick opener whose info string
holds a backtick is not a fence. HTML comments are not special: a '## ' line
inside one is a heading, so after T21 it fails the last-section check (fail
closed rather than hiding text). Effort cells start with S, M or L followed by
' —' or the end of the cell. Table rows shorter than the header are padded
with empty cells, so they fail the checks instead of crashing.

Usage (from anywhere): python3 docs/superpowers/reviews/assets/2026-09-24/t21/check-ids.py
Exit status 0 when every check passes.
"""
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # docs/superpowers/reviews
DOC = ROOT / "2026-09-24-ui-ux-snow-white-review.md"
PLANS = ROOT.parent / "plans"
cp_rows = {}                                        # (CP plan, task id) -> row text
P_FILES = {"P1": "2026-09-25-correctness-and-hardening.md",
           "P2": "2026-09-25-p2-ci-release-hardening.md",
           "P3": "2026-09-25-p3-egui-036-port.md",
           "P4": "2026-09-25-p4-transfer-protocol-v2.md",
           "P5": "2026-09-25-p5-explorer-features.md"}

ID = re.compile(r"F-T\d+-\d+")
DEF = re.compile(r"^(?:#{2,5} |\*\*)(F-T\d+-\d+)\b")
SEVLINE = re.compile(r"^\s*-\s*\*\*Severity:?\*\*:?\s*(S1|S2|S3|D)\b")
GROUPS = {"usability/a11y fixes", "Snow White restyle",
          "causal motion port", "behaviour defect → P-plan"}
P_GROUP = "behaviour defect → P-plan"
SEV = {"S1": 0, "S2": 1, "S3": 2, "D": 3}
EFF = {"S": 0, "M": 1, "L": 2}
PLAN_HEAD = re.compile(r"^#### (CP-[A-Z]\d?) — ")
PENDING = re.compile(r"<!--\s*PENDING")
TREF = r"T\d+(?:–T\d+)?"
SEGMENT = re.compile(rf"^(P[1-5]|CP-[A-Z]\d?) ({TREF}(?:, {TREF})*)(?: \(part\))?$")
EFFORT = re.compile(r"^([SML])(?: —|$)")


def key(i):
    t, n = i[3:].split("-")
    return (int(t), int(n))


def sections(text):
    """Split into (heading, lines) at '## ' lines outside fences."""
    out, fence = [("", [])], None          # fence: (char run, opener indent)
    for line in text.split("\n"):
        body = line.lstrip()
        indent = len(line) - len(body)
        m = re.match(r"(`{3,}|~{3,})(.*)$", body)
        if fence:
            if (m and m.group(1)[0] == fence[0][0] and len(m.group(1)) >= len(fence[0])
                    and not m.group(2).strip() and indent <= fence[1] + 3):
                fence = None
        elif m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1), indent)
        elif line.startswith("## "):
            out.append((line, []))
        out[-1][1].append(line)
    return out


def strip_section(text, name):
    return "\n".join(l for head, body in sections(text)
                     if not head.startswith(f"## {name} ") for l in body)


def definitions(text):
    """id -> severity token ('—' if the finding's block has none)."""
    lines, found = text.split("\n"), {}
    for n, line in enumerate(lines):
        m = DEF.match(line)
        if not m:
            continue
        sev = "—"
        for nxt in lines[n + 1:]:
            if DEF.match(nxt) or nxt.startswith("## ") or nxt.startswith("### "):
                break
            s = SEVLINE.match(nxt)
            if s:
                sev = s.group(1)
                break
        found[m.group(1)] = sev
    return found


def t21_text():
    """T21's lines, plus everything from its heading to the end of the file."""
    text = DOC.read_text()
    parts = sections(text)
    found = [k for k, (head, _) in enumerate(parts) if head.startswith("## T21 ")]
    if len(found) != 1:
        sys.exit(f"FAIL: expected one T21 section, found {len(found)}")
    k = found[0]
    after = [head for head, _ in parts[k + 1:]]
    if after:
        sys.exit(f"FAIL: T21 must be the last section; found after it: {after[0][:60]}")
    tail = text[text.index(parts[k][0]):]
    return "\n".join(parts[k][1]), tail, "review doc, section T21"


def targets(land):
    """'P3 T14; P1 T16' -> [('P3', 'T14'), ('P1', 'T16')]; None if malformed."""
    out = []
    for seg in land.split("; "):
        m = SEGMENT.match(seg)
        if not m:
            return None
        for ref in m.group(2).split(", "):
            a, _, b = ref.partition("–")
            lo, hi = int(a[1:]), int((b or a)[1:])
            if lo > hi:
                return None
            out += [(m.group(1), f"T{n}") for n in range(lo, hi + 1)]
    return out


def norm_land(land):
    return " ".join(land.replace("(part)", "").split())


def p_plan_tasks():
    return {p: {r.get("ID") for r in table((PLANS / f).read_text().split("\n"), "| ID | Task |")}
            for p, f in P_FILES.items()}


def table(lines, header_prefix):
    rows, cols = [], None
    for line in lines:
        if cols is None:
            if line.startswith(header_prefix):
                cols = [c.strip() for c in line.strip().strip("|").split("|")]
            continue
        if not line.startswith("|"):
            break
        if set(line.replace("|", "").strip()) <= set("-: "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(dict(zip(cols, cells + [""] * (len(cols) - len(cells)))))
    return rows


def plan_sections(text):
    """CP plan -> (declared count, covered ids, task row count); plus routing ids."""
    lines = text.split("\n")
    heads = [(n, m.group(1)) for n, l in enumerate(lines) if (m := PLAN_HEAD.match(l))]
    routing_at = next((n for n, l in enumerate(lines) if l.startswith("#### P-routing")), None)
    if routing_at is None:
        sys.exit("FAIL: no '#### P-routing' table in T21")
    plans = {}
    cp_rows.clear()
    for start, name in heads:
        end = next((n for n in range(start + 1, len(lines))
                    if lines[n].startswith("#### ") or lines[n].startswith("### ")), len(lines))
        body = lines[start:end]
        cov = next((l for l in body if l.startswith("**Findings covered")), None)
        m = cov and re.match(r"\*\*Findings covered \((\d+)\):\*\*", cov)
        if not m:
            sys.exit(f"FAIL: {name} has no '**Findings covered (N):**' line")
        ids = ID.findall(cov[m.end():])
        task_rows = table(body, "| ID | Task |")
        tasks = [r.get("ID") for r in task_rows]
        plans[name] = (int(m.group(1)), ids, tasks)
        cp_rows.update({(name, r.get("ID")): " ".join(r.values()) for r in task_rows})
    return plans, table(lines[routing_at:], "| Finding |")


def report(label, items):
    print(f"{label}: {len(items)}" + (f" -> {', '.join(items)}" if items else ""))
    return not items


def main():
    ok = True
    doc = strip_section(DOC.read_text(), "T21")
    mentioned = set(ID.findall(doc))
    sev_of = definitions(doc)
    unused = sorted(mentioned - set(sev_of), key=key)
    t11 = "merged" if "\n## T11 " in doc else "MISSING"
    print(f"source: review doc, all sections except T21 (T11 {t11})")
    print(f"universe: {len(mentioned)} ids ({len(sev_of)} with a definition line; "
          f"mentioned only: {', '.join(unused) or 'none'})")
    ok &= t11 == "merged"

    text, tail, where = t21_text()
    rows = table(text.split("\n"), "| # | ID |")
    ids = [r["ID"] for r in rows]
    print(f"ranked table ({where}): {len(rows)} rows")
    ok &= report("duplicate row keys", [i for i, c in Counter(ids).items() if c > 1])
    ok &= report("malformed ID cells", [i for i in ids if not ID.fullmatch(i)])
    ok &= report("ids missing from the table", sorted(mentioned - set(ids), key=key))
    ok &= report("table ids not in the review", sorted(set(ids) - mentioned, key=key))
    ok &= report("ids cited in T21 but unknown", sorted(set(ID.findall(text)) - mentioned, key=key))
    ok &= report("severity differs from the finding's section",
                 [f"{r['ID']} ({r['Sev']} vs {sev_of.get(r['ID'], '—')})" for r in rows
                  if r["Sev"] != sev_of.get(r["ID"], "—")])

    ranked = [r for r in rows if r["#"] != "—"]
    ranked_ids = {r["ID"] for r in ranked}
    unranked = [r for r in rows if r["#"] == "—"]
    print(f"ranked rows: {len(ranked)}; merged/withdrawn/unused rows: {len(unranked)}")
    unused_rows = [r["ID"] for r in unranked if r["Duplicates merged"].startswith("unused")]
    ok &= report("'unused' rows that differ from the mentioned-only ids",
                 sorted(set(unused_rows) ^ set(unused), key=key))
    by_id = {r["ID"]: r for r in rows}
    ok &= report("merged/withdrawn rows not pointing only at ranked ids",
                 [r["ID"] for r in unranked if r["ID"] not in unused_rows
                  and (not (named := ID.findall(r["Duplicates merged"]))
                       or not set(named) <= ranked_ids)])
    ok &= report("merged/withdrawn rows whose target does not name them back",
                 [f"{r['ID']} -> {t}" for r in unranked if r["ID"] not in unused_rows
                  for t in ID.findall(r["Duplicates merged"])
                  if t in ranked_ids and r["ID"] not in ID.findall(by_id[t]["Duplicates merged"])])
    ok &= report("ranked rows with an unknown group", [r["ID"] for r in ranked if r["Group"] not in GROUPS])
    ok &= report("ranked rows with an unknown Sev or Effort",
                 [r["ID"] for r in ranked if r["Sev"] not in SEV or not EFFORT.match(r["Effort"])])
    ok &= report("unranked rows with a 'Lands in' other than '—'",
                 [r["ID"] for r in unranked if r["Lands in"] != "—"])
    seq = [r["#"] for r in ranked] == [str(n) for n in range(1, len(ranked) + 1)]
    order = [(SEV.get(r["Sev"], 9), EFF.get(r["Effort"][:1], 9), key(r["ID"])) for r in ranked]
    srt = order == sorted(order)
    print("ranks run 1..n:", "yes" if seq else "NO")
    print("sorted by severity, then effort, then id:", "yes" if srt else "NO")
    ok &= seq and srt

    plans, routing_rows = plan_sections(text)
    routing = {r["Finding"]: r for r in routing_rows}
    p_tasks = p_plan_tasks()
    known = {**p_tasks, **{name: set(tasks) for name, (_, _, tasks) in plans.items()}}
    covered = Counter(i for _, lst, _ in plans.values() for i in lst) + Counter(r["Finding"] for r in routing_rows)
    p_rows = {r["ID"] for r in ranked if r["Group"] == P_GROUP}
    bad_target, bad_land, malformed = [], [], []
    for r in ranked:
        land = r["Lands in"]
        tg = targets(land)
        if tg is None:
            malformed.append(r["ID"])
            continue
        bad_target += [f"{r['ID']} ({p} {t})" for p, t in tg if t not in known.get(p, ())]
        first = tg[0][0] if tg else ""
        if r["Group"] == P_GROUP:
            rt = routing.get(r["ID"])
            if (first not in P_FILES or rt is None or rt.get("Sev") != r["Sev"]
                    or norm_land(rt.get("Lands in", "")) != norm_land(land)):
                bad_land.append(r["ID"])
        elif (first not in plans or r["ID"] not in plans[first][1]
              or any(p in plans and r["ID"] not in ID.findall(cp_rows.get((p, t), ""))
                     for p, t in tg)):
            bad_land.append(r["ID"])
    ok &= report("malformed 'Lands in' cells", malformed)
    ok &= report("'Lands in' targets that are not task rows", bad_target)
    ok &= report("rows whose 'Lands in' disagrees with the plans", bad_land)
    ok &= report("ranked ids not covered exactly once",
                 sorted([i for i in (r["ID"] for r in ranked) if covered[i] != 1], key=key))
    ok &= report("covered ids that are not ranked", sorted(set(covered) - {r["ID"] for r in ranked}, key=key))
    ok &= report("routing rows that are not behaviour defects", sorted(set(routing) - p_rows, key=key))
    for name, (declared, lst, tasks) in plans.items():
        good = (declared == len(lst) == len(set(lst)) and 0 < len(tasks) < 30
                and len(tasks) == len(set(tasks)))
        print(f"{name}: {len(lst)} findings (declared {declared}), {len(tasks)} task rows",
              "" if good else "<- FAIL")
        ok &= good
    print(f"P-routing: {len(routing)} findings")
    ok &= report("PENDING markers from T21 to the end of the file",
                 [" ".join(tail[m.start():m.start() + 40].split()) for m in PENDING.finditer(tail)])

    print("group counts:", dict(Counter(r["Group"] for r in ranked)))
    print("severity counts:", dict(Counter(r["Sev"] for r in ranked)))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
