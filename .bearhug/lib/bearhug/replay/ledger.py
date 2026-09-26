"""4.5 — the effectiveness ledger.

For every rule with a machine-detectable signature: how often the harness reminded the operator
(a gate fired), how often the rule was observably followed, how often it was observably violated,
and — a finding in its own right, not a gap (docs/METHOD.md) — which rules this reader cannot
measure at all, and why.

THE TRAP THIS MODULE IS BUILT TO AVOID: a naive substring match. `"git push" in command.lower()`
matches `git stash create` (a real corpus hit), `git push --dry-run` (contacts the remote and
writes nothing — project-barracuda's own `hardsafety.py` exempts it), and a heredoc body quoting
a decision record that merely *mentions* `git push`. Every git-command detector here re-derives
`hardsafety.py`'s actual approach — shlex-segmented, heredoc bodies stripped, the verb read from
token POSITION rather than string content — as its own small, self-contained, tested
reimplementation. It is not an import of barracuda's module: bear-hug never depends on the
subject at runtime (docs/CHARTER.md), and a reimplementation the corpus can falsify is worth more
than a claimed byte-for-byte match nobody checked.

THE SECOND TRAP: assuming the brief's suggested signature is the one the harness actually
enforces. "One ask per turn" was written up here as "count `AskUserQuestion` calls per turn" —
but the live gate for decision 0135 (`response-shape.py`) counts ask-SHAPED LINES in the turn's
closing prose text and never inspects the `AskUserQuestion` tool at all. Both are measured below,
kept in separate rows, because they can and do diverge.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from bearhug.model import Evidence, Finding, Severity
from bearhug.replay.dlv import classify_dlv_command
from bearhug.replay.metrics import DISPATCH_TOOLS
from bearhug.replay.shell import segments as _segments
from bearhug.replay.shell import strip_env as _strip_env
from bearhug.replay.shell import strip_heredocs as _strip_heredocs
from bearhug.replay.transcript import Turn, iter_events, iter_turns

# --- gate landing dates, from project-barracuda's own git history --------------------------
# Reported once here rather than re-derived per rule: `git log --diff-filter=A` against
# scripts/hooks/<name>.py in project-barracuda, read-only, on 2026-08-29.

GATE_LANDED = {
    "response-shape": "2026-08-07",  # decision 0135, commit f1805741
    "dlv-verify-gate": "2026-08-21",  # commit 7f1d3652
    "review-gate": "2026-08-26",  # decision 0280, commit d1e251d3
    "hard-safety": "2026-08-27",  # decision 0282, commit ac1ae0e9
}


def _bucket(day: str, landing: str) -> str:
    return "post" if day >= landing else "pre"


# --- shell-command parsing lives in replay/shell.py; the git detectors read verbs from it -----

_GIT_VALUE_FLAGS = ("-C", "-c", "--git-dir", "--work-tree", "--exec-path", "--namespace")
_STASH_READONLY = ("list", "show")


def _git_subcommand(tokens: list[str]) -> str:
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in _GIT_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return ""


def git_hard_safety_hits(command: str) -> list[str]:
    """Rule ids (`git-push` | `git-stash`) a real Bash command would trip.

    `--dry-run`/`-n` exempts `git push` (contacts the remote, writes nothing); `stash list` and
    `stash show` are read-only. Both exemptions mirror `hardsafety.py`'s `_bash_violations`,
    verified against it read-only on 2026-08-29.
    """
    hits = []
    for tokens in _segments(command):
        tokens = _strip_env(tokens)
        if not tokens or tokens[0] != "git":
            continue
        sub = _git_subcommand(tokens)
        if sub == "push":
            if "--dry-run" not in tokens and "-n" not in tokens:
                hits.append("git-push")
        elif sub == "stash":
            idx = tokens.index("stash")
            rest = [t for t in tokens[idx + 1 :] if not t.startswith("-")]
            if not (rest and rest[0] in _STASH_READONLY):
                hits.append("git-stash")
    return hits


def has_dlv_invocation(command: str) -> bool:
    """A real `dlv <subcommand>` session — the shared M10 classifier's `real-session`.

    Before M10 any `dlv` in verb position counted, so `dlv --help` and `dlv version` verified a
    Go write. The runtime evaluator never accepted those as a session, so the ledger now measures
    what the runtime records: an approved subcommand in verb position. A commit message merely
    mentioning `dlv` remains word-only and is not a match.
    """
    return classify_dlv_command(command) == "real-session"


def _go_path(payload: dict) -> str | None:
    path = payload.get("file_path") or payload.get("path")
    return path if isinstance(path, str) and path.endswith(".go") else None


# --- Bash-mediated `.go` writes -----------------------------------------------------------
# CORRECTION mid-build: this repo's own history records the OLD dlv-verify-gate as blind to
# Bash writes (0 of 132 caught, matcher scoped to Edit|Write). The CURRENT dlv-verify-gate.py
# (built 2026-08-21, after that lesson) is a Stop hook that imports `bash_writes_go` from
# project-barracuda's codewrites.py and explicitly checks Bash-mediated `.go` writes too. A
# denominator built from Edit/Write/MultiEdit alone therefore UNDERCOUNTS relative to what the
# real gate checks. Verified twice against the real corpus while building this ledger:
#
# 1. All 8 real dlv-verify-gate reminders landed on turns an Edit/Write-only detector did not
#    even count as "a .go edit" — the redirect/sed/tee/gofmt checks below were added first, and
#    still caught NONE of the 8: every one of those 8 turns instead wrote Go source through an
#    inline `python3 - <<'PY' ... open(p, "w").write(s) ...` heredoc (a `p = "...pkg/thing.go"`
#    literal, then a Python write call) — the dominant Bash-mediated write pattern in this
#    corpus, not a rare case. The three-part check below (interpreter + write-call + `.go`
#    string literal, all required) is a direct reimplementation of codewrites.py's own
#    `_INLINE_INTERP` / `_PY_WRITE` / `_PY_GO_LITERAL`, read-only, 2026-08-29.
# 2. A plain `cat > "$var"/file.go <<'EOF'` (a quoted-variable path PREFIX in front of an
#    unquoted `.go` suffix) still slips past the redirect regex below, because blanking quote
#    CONTENTS to guard against a prose ">" inside a decision record (the reason the blanking
#    exists at all) breaks the token's adjacency to the redirect operator. Confirmed this is not
#    unique to this reimplementation — project-barracuda's own `_path_re` excludes quote
#    characters from the token the identical way. Left unfixed: closing it risks the false-fire
#    this module exists to avoid, and the inline-interpreter check above already accounts for
#    every real miss found in this corpus.

_GO_TOKEN = r"[^\s'\"]*\.go\b"
_BASH_GO_WRITE = (
    re.compile(r">>?\s*" + _GO_TOKEN),
    re.compile(r"\b(?:sed|perl)\s+(?:-\S+\s+)*-i\b[^\n]*" + _GO_TOKEN),
    re.compile(r"\btee\s+(?:-\S+\s+)*" + _GO_TOKEN),
    re.compile(r"\b(?:gofmt|goimports)\s+(?:-\S+\s+)*-w\b[^\n]*" + _GO_TOKEN),
)
_SINGLE_QUOTED = re.compile(r"'[^']*'")
_DOUBLE_QUOTED = re.compile(r'"[^"]*"')

_INLINE_INTERP = re.compile(r"(?:^|[|;&]|&&)\s*(?:python3?|perl)\b[^\n]*(?:<<|-c\b|-e\b)")
_PY_WRITE = re.compile(
    r"open\([^)]*,\s*['\"][wa]['\"]|\.write\(|writelines\(|shutil\.(?:copy|move)"
)
_PY_GO_LITERAL = re.compile(r"['\"][^'\"]*[A-Za-z0-9_-]\.go['\"]")


def _shell_skeleton(command: str) -> str:
    """`command` with heredoc bodies and quoted-string CONTENTS blanked. What is left is the
    part the shell reads as verbs, operators, and unquoted words — the only place a real
    redirect target can live."""
    skeleton = _strip_heredocs(command)
    skeleton = _SINGLE_QUOTED.sub(" '' ", skeleton)
    skeleton = _DOUBLE_QUOTED.sub(' "" ', skeleton)
    return skeleton


def bash_writes_go(command: str) -> bool:
    """A Bash command that writes a `.go` file: a shell-level write (redirect, `sed`/`perl -i`,
    `tee`, `gofmt`/`goimports -w`), or an inline Python/Perl heredoc that opens a `.go`-named
    path in write/append mode. See the module note above for what this still misses."""
    if not isinstance(command, str) or ".go" not in command:
        return False
    skeleton = _shell_skeleton(command)
    if ".go" in skeleton and any(pattern.search(skeleton) for pattern in _BASH_GO_WRITE):
        return True
    return bool(
        _INLINE_INTERP.search(command)
        and _PY_WRITE.search(command)
        and _PY_GO_LITERAL.search(command)
    )


# --- decision 0135's ACTUAL signature: ask-shaped lines in the turn's closing prose ----------
# Reimplemented from project-barracuda's scripts/hooks/response-shape.py, read-only, 2026-08-29.
#
# `_prose_lines`'s fence reader mirrors `bearhug_runtime.evaluators.response_shape` exactly
# (Round 17, 2026-09-15) rather than importing it: this module never depends on the runtime at
# runtime (docs/CHARTER.md; see also the git-command detectors above, which follow the same
# reimplement-and-test-the-fixture rule rather than importing `hardsafety.py`). Before Round 17
# this function still toggled a boolean on any line starting with ```` ``` ````, which is why
# `ask_shape`'s docstring below used to overstate its own accuracy -- see
# `tests/test_replay_ledger.py::test_ledger_and_evaluator_fence_readers_agree` for the fixture
# set that pins the two readers to the same verdicts, including the three CommonMark cases the
# evaluator's Round 17 fix closed (D1 closer-with-info-string, D2 four-space indent, D3 a
# backtick info string containing a backtick).

LONG_CHARS = 1500

_ASK_PATTERNS = re.compile(
    r"(?:want me to\b|should i\b|shall i\b|do you want\b|would you (?:like|rather|prefer)\b|"
    r"your call\b|let me know\b|which (?:one|of these|would)\b|got it\?|ack\b.*\?|"
    r"confirm\b[^.]*\?)",
    re.IGNORECASE,
)
_STOP_REQUEST = re.compile(
    r"(?:\bstop (?:here|there|for now)\b|\bhalt\b|\bthat(?:'s| is) enough\b|"
    r"\bwe(?:'re| are) done\b|\bend (?:the )?(?:session|turn|here)\b|\bhold (?:off|here)\b|"
    r"\bno more (?:questions|cards)\b)",
    re.IGNORECASE,
)

#: Mirrors `bearhug_runtime.evaluators.response_shape._FENCE` exactly (Round 17, 2026-09-15): up
#: to 3 leading spaces (CommonMark caps a fence's own indent there), the marker run (` or ~,
#: three or more, captured for the closer match), and the rest of the line as its info string.
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")


def _prose_lines(text: str) -> list[str]:
    """Drop fenced code and blockquotes before counting ask lines.

    Mirrors `bearhug_runtime.evaluators.response_shape._prose_lines` exactly, including its
    three CommonMark rules: a fence opener must be indented 0-3 spaces; a backtick fence's info
    string must not itself contain a backtick (tilde fences carry no such restriction); and a
    closing fence may carry nothing but trailing whitespace after its marker.
    """
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for raw in text.split("\n"):
        stripped = raw.strip()
        candidate = _FENCE.match(raw)
        marker = candidate.group(1) if candidate else None
        info = candidate.group(2).strip() if candidate else ""
        if fence is None:
            if candidate and not (marker[0] == "`" and "`" in info):
                fence = (marker[0], len(marker))
                continue
        elif (
            marker is not None
            and marker[0] == fence[0]
            and len(marker) >= fence[1]
            and info == ""
        ):
            fence = None
            continue
        if fence is not None or stripped.startswith(">"):
            continue
        out.append(raw)
    return out


def _count_asks(text: str) -> int:
    n = 0
    for line in _prose_lines(text):
        s = line.strip()
        if not s:
            continue
        if s.endswith("?") or _ASK_PATTERNS.search(s):
            n += 1
    return n


def ask_shape(final_text: str, prompt: str) -> str:
    """Classify one turn's closing message the way `response-shape.py` would judge it.

    Returns one of: "no-final-text" (the gate never evaluates these), "bundled" (2+ asks —
    always a violation), "no-engagement" (long, zero asks, and the human did not ask to stop —
    a violation), "ok-stop-requested" (long with no ask, but the human asked the turn to end —
    compliant by 0135's own carve-out), "ok" (one ask, or short with none).
    """
    if not final_text.strip():
        return "no-final-text"
    asks = _count_asks(final_text)
    chars = len(final_text)
    if asks >= 2:
        return "bundled"
    if asks == 0 and chars >= LONG_CHARS:
        if _STOP_REQUEST.search(prompt or ""):
            return "ok-stop-requested"
        return "no-engagement"
    return "ok"


# --- per-turn fact extraction, one pass over one turn's events -------------------------------


@dataclass(slots=True)
class _TurnFacts:
    saw_go_edit: bool = False
    dlv_after_go: bool = False
    review_after_go: bool = False
    ask_calls: int = 0
    git_hits: list[str] = field(default_factory=list)
    last_assistant_text: str = ""
    #: Subagent dispatches per assistant message id — the unit of "concurrent" (M08).
    dispatch_batches: Counter = field(default_factory=Counter)


def _turn_facts(turn: Turn) -> _TurnFacts:
    facts = _TurnFacts()
    for index, event in enumerate(turn.events):
        if event.kind == "assistant" and event.text:
            facts.last_assistant_text = event.text
            continue
        if event.kind != "tool_use":
            continue
        if event.name in DISPATCH_TOOLS:
            # A record without a message id cannot be joined to any other, so it is its own batch.
            facts.dispatch_batches[event.message_id or f"<event:{index}>"] += 1
        if event.name in ("Edit", "Write", "MultiEdit") and _go_path(event.payload):
            facts.saw_go_edit = True
            continue
        if event.name == "AskUserQuestion":
            facts.ask_calls += 1
            continue
        if event.name == "Bash":
            command = event.payload.get("command", "")
            if bash_writes_go(command):
                facts.saw_go_edit = True
            if facts.saw_go_edit and not facts.dlv_after_go and has_dlv_invocation(command):
                facts.dlv_after_go = True
            facts.git_hits.extend(git_hard_safety_hits(command))
            continue
        if event.name == "Agent" and event.payload.get("subagent_type") == "opus-reviewer":
            if facts.saw_go_edit:
                facts.review_after_go = True
            continue
    return facts


# --- the counters a ledger row is built from --------------------------------------------------


@dataclass(slots=True)
class RuleCounts:
    denominator: int = 0
    followed: int = 0
    violated: int = 0
    reminded: int = 0


@dataclass(slots=True)
class LedgerData:
    """Every countable rule's pre/post-gate counts, plus the supplementary metrics that don't
    fit the reminded/followed/violated shape (a NEVER rule has no positive 'followed' event;
    the AskUserQuestion-count metric corresponds to no known enforcement mechanism)."""

    counts: dict[str, dict[str, RuleCounts]]  # rule -> {"pre": ..., "post": ...}
    gate_fired_total: Counter  # every attributed Stop-gate block, corpus-wide (reminded, general)
    ask_call_histogram: Counter  # AskUserQuestion calls per turn: {0: n, 1: n, 2: n, ...}
    turns_measured: int
    date_range: tuple[str, str]
    #: M13: pre/post follow each transcript's resolved harness version, not the wall clock
    attributed: bool = False


RULES = (
    "git-push",
    "git-stash",
    "dlv-before-claiming",
    "review-after-write",
    "one-ask-per-turn",
    "max-two-concurrent-subagents",
)

#: The gate each rule's pre/post split is keyed to (see GATE_LANDED). One table, so the ledger's
#: rendering and the M07 inventory cannot disagree about which script a rule is attributed to.
#: `None` is a prose-only rule: no gate exists, so there is no landing date to split at and its
#: counts live in one `all` window (M13 owns per-transcript harness attribution).
RULE_GATES: dict[str, str | None] = {
    "git-push": "hard-safety",
    "git-stash": "hard-safety",
    "dlv-before-claiming": "dlv-verify-gate",
    "review-after-write": "review-gate",
    "one-ask-per-turn": "response-shape",
    "max-two-concurrent-subagents": None,
}

#: CLAUDE.md §2: "MAX 2 CONCURRENT SUBAGENTS." Read from the snapshotted text, not chosen here.
MAX_CONCURRENT_SUBAGENTS = 2


def compute_ledger(
    paths: list[Path],
    *,
    since: str | None = None,
    attribution: dict[Path, str | None] | None = None,
) -> LedgerData:
    """One pass per transcript. Every rule's counts fall out of the same turn traversal that
    Task A's per-session metrics also use — `iter_turns(iter_events(path))` — so the corpus is
    read once, not once per rule.

    `attribution` (M13) maps each transcript to the date of the harness commit it ran under, or
    None when that could not be resolved. With it, a gated rule's pre/post split follows the
    HARNESS the session actually ran under rather than the turn's wall-clock day, and turns from
    an unresolved transcript land in `unattributed`, excluded from every pre/post claim. Without
    it the split is by landing date, as before.
    """
    counts: dict[str, dict[str, RuleCounts]] = {
        rule: (
            {"pre": RuleCounts(), "post": RuleCounts()}
            | ({"unattributed": RuleCounts()} if attribution is not None else {})
            if RULE_GATES[rule] is not None
            else {"all": RuleCounts()}
        )
        for rule in RULES
    }
    gate_fired_total: Counter = Counter()
    ask_hist: Counter = Counter()
    turns_measured = 0
    first_day = ""
    last_day = ""

    for path in paths:
        harness_day: str | None = None
        if attribution is not None:
            date = attribution.get(path) or attribution.get(Path(path).resolve())
            harness_day = date[:10] if isinstance(date, str) and date else None
        for turn in iter_turns(iter_events(path)):
            day = turn.timestamp[:10]
            if since and day < since:
                continue

            def bucket_for(gate: str, day: str = day, harness_day: str | None = harness_day) -> str:
                if attribution is None:
                    return _bucket(day, GATE_LANDED[gate])
                if harness_day is None:
                    return "unattributed"
                return _bucket(harness_day, GATE_LANDED[gate])

            turns_measured += 1
            if not first_day or (day and day < first_day):
                first_day = day
            if day and day > last_day:
                last_day = day

            blocked = set(turn.blocked_by)
            gate_fired_total.update(blocked)
            facts = _turn_facts(turn)
            ask_hist[facts.ask_calls] += 1

            for hit in facts.git_hits:
                bucket = bucket_for("hard-safety")
                counts[hit][bucket].violated += 1
            if "hard-safety" in blocked:
                bucket = bucket_for("hard-safety")
                counts["git-push"][bucket].reminded += 1
                counts["git-stash"][bucket].reminded += 1

            if facts.saw_go_edit:
                bucket = bucket_for("dlv-verify-gate")
                counts["dlv-before-claiming"][bucket].denominator += 1
                if facts.dlv_after_go:
                    counts["dlv-before-claiming"][bucket].followed += 1
                else:
                    counts["dlv-before-claiming"][bucket].violated += 1

                bucket = bucket_for("review-gate")
                counts["review-after-write"][bucket].denominator += 1
                if facts.review_after_go:
                    counts["review-after-write"][bucket].followed += 1
                else:
                    counts["review-after-write"][bucket].violated += 1

            if "dlv-verify-gate" in blocked:
                bucket = bucket_for("dlv-verify-gate")
                counts["dlv-before-claiming"][bucket].reminded += 1
            if "review-gate" in blocked:
                bucket = bucket_for("review-gate")
                counts["review-after-write"][bucket].reminded += 1

            if facts.dispatch_batches:
                concurrent = counts["max-two-concurrent-subagents"]["all"]
                concurrent.denominator += 1
                if max(facts.dispatch_batches.values()) > MAX_CONCURRENT_SUBAGENTS:
                    concurrent.violated += 1
                else:
                    concurrent.followed += 1

            shape = ask_shape(facts.last_assistant_text, turn.prompt)
            if shape != "no-final-text":
                bucket = bucket_for("response-shape")
                counts["one-ask-per-turn"][bucket].denominator += 1
                if shape in ("ok", "ok-stop-requested"):
                    counts["one-ask-per-turn"][bucket].followed += 1
                else:
                    counts["one-ask-per-turn"][bucket].violated += 1
            if "response-shape" in blocked:
                bucket = bucket_for("response-shape")
                counts["one-ask-per-turn"][bucket].reminded += 1

    return LedgerData(
        counts=counts,
        gate_fired_total=gate_fired_total,
        ask_call_histogram=ask_hist,
        turns_measured=turns_measured,
        date_range=(first_day, last_day),
        attributed=attribution is not None,
    )


# --- rules with a signature that exists but this reader cannot resolve to a rate -------------

UNMEASURABLE_GATES = {
    "go-postedit": (
        "go-postedit.sh's block message reports live gofmt/go vet output. Following or "
        "violating it requires re-running the Go toolchain against the repository state at the "
        "historical commit, which Phase 4 (replay of records already written) cannot do; only "
        "PostToolUse's own COMPLAINT is readable from the transcript, via the same "
        "hook_blocking_error records blocking_firings already attributes."
    ),
    "task-durability": (
        "The rule is that in-flight work is tracked in TaskCreate/TaskUpdate. Whether tracking "
        "WAS warranted for a given turn is a judgment call this reader has no independent way to "
        "make (there is no ground truth for 'this turn needed a task'); only the gate's own "
        "complaint is countable, not compliance or violation."
    ),
    "task-existence": (
        "Same limit as task-durability: the reader can count the gate's complaints but has no "
        "way to independently decide whether a task SHOULD have existed for a given turn."
    ),
    "joinkey-lint": (
        "joinkey-lint.py checks a structural property of committed decision records against the "
        "working tree at check time; a transcript replay sees neither the working tree at that "
        "historical moment nor the lint's structural rule, only its blocking message."
    ),
    "phase-tag": (
        "phase-tag's rule is about a commit-message convention; whether a given commit's phase "
        "tag was CORRECT (not just present) is a judgment this reader has no way to check from "
        "the transcript alone."
    ),
    "debug-first": (
        "debug-first.sh was superseded by dlv-verify-gate.py on 2026-08-21 (see GATE_LANDED); "
        "its advisory (non-blocking) nudges are PostToolUse hook_additional_context records, a "
        "kind blocking_firings does not walk (it only sees hook_blocking_error), so this reader "
        "cannot even count its reminders without adding a second event kind. This entry should "
        "read 0 reminders here: a prior version of hooks.SIGNATURES checked 'debug-first' before "
        "'dlv' and mis-attributed all 8 real dlv-verify-gate blocks in the corpus to this gate "
        "(dlv-verify-gate's own message opens 'debug-first gate — ...'), which read as a live "
        "gate with a healthy signal next to a suspicious dlv-verify-gate zero. Fixed 2026-08-29; "
        "see tests/test_replay_transcript.py's regression test."
    ),
}


def build_findings(
    data: LedgerData,
    *,
    snapshot_id: str,
    since: str | None,
    validated: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Findings for the four measured rules, plus one per gate this module cannot resolve to a
    rate — per METHOD.md, a rule that lands in `unmeasurable` is a finding, not a gap.

    `validated` names the rules whose classifiers M14 has validated against APPROVED thresholds
    (`groundtruth.validated_rules`). Only those rows carry a violation RATE; every other row keeps
    counts alone, as Sam ruled on 2026-09-01: until then no rate is published anywhere.
    """
    window = f"{data.date_range[0]}..{data.date_range[1]}"
    if since:
        window = f"{since}..{data.date_range[1]} (--since {since})"
    findings: list[Finding] = []

    def _row(rule: str, gate: str, description: str, limit: str, *, is_never: bool = False):
        pre, post = data.counts[rule]["pre"], data.counts[rule]["post"]
        landed = GATE_LANDED[gate]
        severity = Severity.INFO
        if post.violated > 0:
            severity = Severity.BROKEN  # the gate exists and the rule was still broken after it
        detail = (
            f"gate `{gate}` landed {landed}. "
            f"PRE ({data.date_range[0]}..{landed}): denominator={pre.denominator}, "
            f"followed={'n/a (prohibition)' if is_never else pre.followed}, "
            f"violated={pre.violated}, reminded={pre.reminded}. "
            f"POST ({landed}..{data.date_range[1]}): denominator={post.denominator}, "
            f"followed={'n/a (prohibition)' if is_never else post.followed}, "
            f"violated={post.violated}, reminded={post.reminded}."
        )
        unattributed = data.counts[rule].get("unattributed")
        if unattributed is not None:
            detail += (
                " Buckets are by harness attribution (M13: the harness commit each transcript "
                "resolved to), not by wall clock. UNATTRIBUTED — ambiguous or unknown harness "
                "version, excluded from every pre/post claim: "
                f"denominator={unattributed.denominator}, "
                f"followed={'n/a (prohibition)' if is_never else unattributed.followed}, "
                f"violated={unattributed.violated}, reminded={unattributed.reminded}."
            )
        if rule in validated:
            if post.denominator == 0:
                detail += (
                    " VIOLATION RATE: classifier validated (M14, thresholds approved), but the "
                    "post-gate denominator is zero in this corpus, so no rate exists to publish."
                )
            else:
                rate = 100.0 * post.violated / post.denominator
                detail += (
                    f" VIOLATION RATE (post-gate window): {post.violated}/{post.denominator} = "
                    f"{rate:.1f}% — published because M14 validated this classifier against "
                    "thresholds Sam approved on 2026-09-01; the pre-gate window is a count only."
                )
        findings.append(
            Finding(
                id=f"ledger-{rule}",
                check="effectiveness-ledger",
                severity=severity,
                summary=description,
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"transcripts {window}"),),
                detail=detail,
                limit=limit,
            )
        )

    _row(
        "git-push",
        "hard-safety",
        "NEVER `git push` (CLAUDE.md §0): real `git push` invocations (shlex-parsed, "
        "`--dry-run`/`-n` exempted) vs. hard-safety.py PreToolUse denials.",
        "Correlation only, and the denial side could not be measured at all: hard-safety.py "
        "denies via PreToolUse permissionDecision, which never appears as a hook_blocking_error "
        "attachment in this transcript format (0 of 78 real hook_blocking_error records carry "
        "hookEvent=PreToolUse — all 78 are Stop=75/PostToolUse=3). Zero real `git push` attempts "
        "occurred after the gate landed, so this is an untested absence of opportunity, not a "
        "validated zero (METHOD.md: silence is not absence). The one PRE-gate violation found "
        "(2026-08-19) predates the gate by 8 days and could not have been blocked by it.",
        is_never=True,
    )
    _row(
        "git-stash",
        "hard-safety",
        "NEVER `git stash` (CLAUDE.md §0): real non-readonly `git stash` invocations vs. "
        "hard-safety.py PreToolUse denials.",
        "Same PreToolUse-visibility limit as git-push. Additionally, this detector (like "
        "hardsafety.py itself) is not scoped by working directory: the one PRE-gate hit found "
        "(2 stash ops in one Bash call, 2026-08-20) targeted an unrelated personal repo "
        "(~/Documents/github/memq), not project-barracuda — a real signal about the operator's "
        "own shell habits, not evidence of a barracuda-session violation specifically.",
        is_never=True,
    )
    _row(
        "dlv-before-claiming",
        "dlv-verify-gate",
        "A `.go` write in a turn — via Edit/Write/MultiEdit, or a Bash redirect/sed-perl-i/"
        "tee/gofmt-w (`bash_writes_go`) — followed later in that same turn by a real "
        "`dlv <subcommand>` invocation (verb-position match — a commit message merely "
        "mentioning `dlv` does not count).",
        "This repo's OWN history records an EARLIER dlv-verify-gate as blind to Bash writes "
        "(0 of 132 caught, matcher scoped to Edit|Write) — but the CURRENT gate (built "
        "2026-08-21, after that lesson) already checks Bash writes via project-barracuda's own "
        "`bash_writes_go`, including an inline-Python-heredoc case. Verified against the real "
        "corpus while building this ledger: all 8 real reminders landed on turns an "
        "Edit/Write-only version of this detector missed entirely — every one wrote Go source "
        "via `python3 - <<'PY' ... open(p, \"w\").write(s)`, not a shell redirect — which is why "
        "that case is reimplemented here too, from `bash_writes_go`'s own read-only source. "
        "UNRESOLVED GAP found doing that verification: even with the inline-interpreter case "
        "added, this row's post-gate violated count (44) is still far larger than its reminded "
        "count (8), and the 8 real reminders do not obviously fall inside the 44 turns this "
        "detector calls violated when checked one-by-one. That could mean this reimplementation "
        "and the real gate disagree on the turn boundary, that many violations legitimately land "
        "in subagent transcripts the Stop hook never runs against, or that the gate under-fires "
        "on real violations — this reader cannot distinguish the three from replay alone, so "
        "'reminded' should be read as a validated lower bound and 'violated' as a good-faith "
        "upper bound, not as two counts of the same event.",
    )
    _row(
        "review-after-write",
        "review-gate",
        "A `.go` write in a turn (same detector as dlv-before-claiming, Edit/Write/MultiEdit or "
        "`bash_writes_go`), followed later in that same turn by an "
        "`Agent(subagent_type=\"opus-reviewer\")` dispatch.",
        "A turn can legitimately skip review because the edit was reverted, was a comment-only "
        "change, or review happened in a LATER turn on the same diff — none of which this "
        "per-turn detector can distinguish from a real skip. Counts co-occurrence within one "
        "turn only. Post-gate reminded (16) is closer to violated (26) here than in the "
        "dlv-before-claiming row (8 vs 44), but the same turn-boundary caveat applies: this "
        "reader cannot confirm the 16 reminded turns are a subset of the 26 called violated.",
    )
    _row(
        "one-ask-per-turn",
        "response-shape",
        "decision 0135's ACTUAL signature (reimplemented from response-shape.py): 2+ ask-shaped "
        "lines in a turn's closing assistant text is a 'bundled' violation; 0 asks with "
        "≥1500 chars and no human stop-request is a 'no-engagement' violation.",
        "This is a reimplementation, not the deployed hook's own code path, so its exact recall "
        "against the real gate is unverified beyond the reminded-vs-violated comparison in the "
        "detail field. The brief's literal suggestion — counting `AskUserQuestion` tool calls "
        "per turn — measures a DIFFERENT thing that response-shape.py never inspects; see the "
        "ask_call_histogram in the JSON for that count on its own, unattached to any known gate.",
    )

    concurrent = data.counts["max-two-concurrent-subagents"]["all"]
    findings.append(
        Finding(
            id="ledger-max-two-concurrent-subagents",
            check="effectiveness-ledger",
            # No gate exists for this rule, so no gate was broken; the count is the finding.
            severity=Severity.INFO,
            summary="MAX 2 CONCURRENT SUBAGENTS (CLAUDE.md §2): turns whose largest single "
            "assistant message dispatched more than two subagents (Agent/Task tool_use blocks "
            "grouped by `message.id`).",
            snapshot=snapshot_id,
            evidence=(Evidence(run_id=f"transcripts {window}"),),
            detail=(
                f"prose-only rule — no registered hook enforces it, so there is no landing "
                f"date and one window ({window}): denominator={concurrent.denominator} turns "
                f"with at least one dispatch, followed={concurrent.followed}, "
                f"violated={concurrent.violated}, reminded=0 (nothing can remind)."
            ),
            limit=(
                "Counts, not a rate: no percentage is reported for this classifier until M14 "
                "ground truth covers it. 'Concurrent' is read structurally as one assistant "
                "message id; a batch the model issued in two messages milliseconds apart is "
                "counted as sequential, and a subagent that itself dispatched subagents is "
                "counted in its own transcript, not its parent's. The rule's other clauses "
                "(never two agents in one Go package; review tiers serialize) are not measured."
            ),
        )
    )

    for gate, reason in UNMEASURABLE_GATES.items():
        count = data.gate_fired_total.get(gate, 0)
        findings.append(
            Finding(
                id=f"ledger-unmeasurable-{gate}",
                check="effectiveness-ledger",
                severity=Severity.INFO,
                summary=f"`{gate}`: reminded {count} time(s) in {window}; followed/violated "
                "are UNMEASURABLE from transcript replay, and that is itself the finding.",
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"transcripts {window}"),),
                detail=reason,
                limit="A rule nobody can detect compliance with cannot be enforced, tested, or "
                "probably followed either (docs/METHOD.md).",
            )
        )
    return findings


def render_table(data: LedgerData) -> str:
    lines = [
        f"effectiveness ledger — {data.turns_measured} turns, "
        f"{data.date_range[0]}..{data.date_range[1]}",
        "",
        f"{'rule':22s}{'gate':16s}{'window':6s}{'denom':>7s}{'followed':>10s}"
        f"{'violated':>10s}{'reminded':>10s}",
    ]
    for rule, gate in RULE_GATES.items():
        for bucket, c in data.counts[rule].items():
            followed = "n/a" if rule in ("git-push", "git-stash") else str(c.followed)
            lines.append(
                f"{rule[:22]:22s}{(gate or 'none (prose)')[:16]:16s}{bucket:6s}"
                f"{c.denominator:>7d}{followed:>10s}{c.violated:>10d}{c.reminded:>10d}"
            )
    lines.append("")
    lines.append("AskUserQuestion calls per turn (supplementary — no gate inspects this tool):")
    for n in sorted(data.ask_call_histogram):
        lines.append(f"  {n:>2d} call(s): {data.ask_call_histogram[n]:>5d} turns")
    lines.append("")
    lines.append("all Stop-gate reminders, corpus-wide (attributed complaints, not activity):")
    for gate, n in sorted(data.gate_fired_total.items()):
        lines.append(f"  {gate:22s}{n:>6d}")
    return "\n".join(lines)


__all__ = [
    "GATE_LANDED",
    "MAX_CONCURRENT_SUBAGENTS",
    "RULE_GATES",
    "LedgerData",
    "RuleCounts",
    "ask_shape",
    "bash_writes_go",
    "build_findings",
    "compute_ledger",
    "git_hard_safety_hits",
    "has_dlv_invocation",
    "render_table",
]
