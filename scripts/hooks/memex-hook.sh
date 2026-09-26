#!/bin/sh
# memex-hook.sh — Claude Code hooks over the decision record. One script, one
# subcommand per event, so the binary-cache logic exists once (CLAUDE.md §11).
#
#   post-edit      PostToolUse (Edit|Write|MultiEdit): name the decision records
#                  that cite the edited file in evidence:. Converts "read the
#                  catalog before a load-bearing edit" from a rule that decays
#                  into an event that fires (CLAUDE.md §0a, §10). Silent on a
#                  miss — a hook that prints on every edit becomes noise.
#   session-start  SessionStart: two lines — corpus size and the open set. A
#                  fresh window inherits nothing but artifacts (CLAUDE.md §9).
#   cited-staged   git pre-commit: name the records citing any STAGED file. Not a
#                  Claude Code hook — plain text out, silent on a miss, so the
#                  caller can test for emptiness the way post-edit does. Exists
#                  because the memex lint gate keyed on "a docs/memex/ page is
#                  staged", and the commits that DRIFT an evidence anchor are
#                  Go-only ones: eight anchors across seven records drifted on
#                  2026-08-19 and surfaced two commits late, blamed on the commit
#                  that happened to stage a memex page.
#   (response shape) Stop: decision 0135's gate moved to
#                  scripts/hooks/response-shape.py, which BLOCKS on ask count
#                  rather than warning on length a turn later. Measured
#                  2026-08-07: the blocking AskUserQuestion gate changed
#                  behaviour 6/6 in one session; the advisory marker this
#                  replaced was overridden every time it fired.
#   stop           Stop: one line reminding that /decide exists, when the turn
#                  resolved an AskUserQuestion. Keyed on that tool rather than on
#                  approval words: 108 of 121 recorded rulings match no approval
#                  marker (measured 2026-08-05 over a 17-marker set, substring
#                  matching, so the rate is a floor) — an 89% false-negative rate
#                  for prose scanning, while AskUserQuestion exists to resolve a
#                  decision. Never blocks; suppressed when the ruling was already
#                  recorded, and on the second of two consecutive turns.
#
# Both read the corpus through `memexlint`, the same parser the validator uses.
# ONE subcommand gates: pre-question STOPS a question that may already be ruled
# and requires it to carry what each surfaced record says, so Sam adjudicates
# from the card (his ruling 2026-08-13). Every other
# subcommand is advisory and exits 0 silently on any failure. The 0135
# response-shape gate and the 0137 task-durability gate DO block, in their own
# scripts.
#
# The component bundle carries a built `memexlint` binary. Keeping the binary
# beside this hook makes the hook usable from an unwired repository: it does
# not assume that the target contains another product checkout or a Go module.

set -u

mode=${1:-}
[ -n "$mode" ] || exit 0

# Prefer the provider's explicit project root. When it is absent, resolve the
# Git checkout from the caller's working directory; the bundle itself is kept
# outside that checkout.
root=${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}

# Observability only — stamp.sh can never change this hook's exit code. Stamped
# BEFORE every early exit below, so an inert run is recorded rather than looking
# identical to a hook that was never wired; the verdict distinguishes them.
if [ -f "$root/scripts/hooks/stamp.sh" ]; then
	. "$root/scripts/hooks/stamp.sh" 2>/dev/null || :
fi
command -v stamp >/dev/null 2>&1 || stamp() { :; }
stamp "memex-$mode" ran

[ -d "$root/docs/memex/decisions" ] || {
	stamp "memex-$mode" skip-no-corpus
	exit 0
}
# The installer may set this when it materializes the hook into a provider's
# project tree. The default is the central bundle layout: hooks/ beside bin/.
components=${BEARHUG_COMPONENTS_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." 2>/dev/null && pwd)}
bin=${BEARHUG_MEMEXLINT:-$components/bin/memexlint}
[ -x "$bin" ] || exit 0

# emit wraps text as the hook's additionalContext for the given event.
emit() {
	printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":%s}}\n' \
		"$1" "$(printf '%s' "$2" | jq -Rs .)"
}

# Sam's ruling 2026-08-06 was "yes pre question should block", because advisory
# context is ignorable and being ignorable is the failure mode of the rule this
# replaces. The stop survives; only the adjudicator changed.
#
# samgate STOPS the question and routes the decision to Sam through the card.
#
# It emits permissionDecision "deny" because that is the only decision the
# harness honours on AskUserQuestion. MEASURED 2026-08-13 with a temporary probe:
# "ask" fires the hook and is then permitted silently, so the gate became inert
# the moment it was tried. deny is not a preference here, it is the only lever.
#
# What CHANGED (Sam, 2026-08-13) is what the stop DEMANDS. It no longer accepts
# the loop self-certifying that a ruling "does not settle it" — that is Sam's
# call, not the loop's (0122). It requires the question to name each surfaced
# record AND say what it rules, so Sam adjudicates from the card.
samgate() {
	printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
		"$(printf '%s' "$1" | jq -Rs .)"
}

case "$mode" in
pre-question)
	# PreToolUse (AskUserQuestion): name the accepted rulings that may already
	# answer the question, BEFORE Sam is asked. The mirror of post-edit — that one
	# fires when code is edited, keyed on the file; this fires before an interview,
	# keyed on the subject.
	#
	# Measured 2026-08-06: two interviews in one session asked Sam questions that
	# decisions 0100 and 0013 had already answered. Neither involved an edit, so no
	# existing hook could have fired. Attention is the scarcer resource, and this is
	# the only hook that protects it BEFORE it is spent.
	#
	# BLOCKS. Sam's ruling 2026-08-06, verbatim: "yes pre question should block".
	# Advisory context is ignorable, and being ignorable is precisely the failure
	# mode of the written rule this replaces (decision 0096).
	#
	# THE ESCAPE HATCH IS MECHANICAL, NOT A JUDGMENT CALL. A blocking check with no
	# way through would make a genuinely-open question unaskable the moment it
	# scored. To proceed, the question must NAME the records it has read, as
	# `memex-checked: NNNN[, NNNN]`, covering every record the search surfaced.
	# That cannot be satisfied by ignoring the hook, and it leaves an audit trail
	# in the question Sam reads: he sees which rulings were considered.
	# stdin is readable ONCE, so the payload is captured before any jq call.
	pq=$(cat)

	# Ids the question already accounts for, read BEFORE the clause is stripped.
	# Every `memex-checked:` clause in any field contributes.
	acked=$(printf '%s' "$pq" |
		grep -oE 'memex-checked:[[:space:]]*[0-9]{1,4}([[:space:]]*,[[:space:]]*[0-9]{1,4})*' |
		sed -E 's/memex-checked:[[:space:]]*//' | tr ',' ' ' | tr -s ' ' '\n' |
		sed -nE 's/^0*([0-9]+)$/\1/p')

	# STRIP THE WHOLE ACK CLAUSE — ids AND the justification prose after them —
	# BEFORE searching. Two regresses were measured on 2026-08-06, both of the same
	# class: feeding the acknowledgment back in as query text.
	#
	#   1. The IDS matched other records literally: acking 0060/0100/0132 hit 0058
	#      on the string "0060".
	#   2. The JUSTIFICATION introduced new vocabulary: explaining why 0012 did not
	#      settle a question meant writing "LocalizedTextResolve", "grep", "retire",
	#      which surfaced 0034 and 0052 — records the original question never
	#      touched. Each round of explaining grew the question, and a growing
	#      question grows the match set, so the gate had NO GUARANTEED FIXED POINT
	#      and blocked legitimate interviews indefinitely.
	#
	# The convention that makes this decidable: an ack clause runs from
	# `memex-checked:` to the END OF THE FIELD it appears in, so the justification
	# belongs in that same field. Stripped per field, before the join, because after
	# joining there is no field boundary left to anchor on.
	#
	# `split(token)[0]` and NOT a regex. The previous `gsub("memex-checked:.*$"; "")`
	# stripped a single-line clause and stripped a MULTI-LINE one not at all: jq's
	# `.` does not cross a newline, so the pattern simply failed to match and the
	# whole clause — ids and every explanation — fed back in as query text. That is
	# the regress this strip exists to prevent, reintroduced by the anchor. Measured
	# 2026-08-13 by session A: seven consecutive attempts blocked with DISJOINT
	# demanded sets, matching on words that appear only inside the ack clause
	# ("rulings", "bring", "alone", "neither") and on ids cited only in the
	# acknowledgment itself. jq's "s" flag does not fix it (verified inert here).
	# Scores question + header ONLY — not option label/description. Sam's
	# ruling 2026-08-13: option text is where a retry's wording churns most
	# (it must change — the exchange evolved), so scoring it gave "no
	# guaranteed fixed point" a second source independent of the already-fixed
	# ack-stripping bug: fresh option wording is fresh vocabulary for the
	# scorer on every reword, even when the actual question is unchanged.
	qsearch=$(printf '%s' "$pq" | jq -r '[(.tool_input.questions // [])[]
	            | .question, .header]
	           | map(split("memex-checked:")[0]) | join(" ")' 2>/dev/null)
	[ -n "$(printf '%s' "$qsearch" | tr -d '[:space:]')" ] || exit 0
	out=$("$bin" -root "$root" -topic "$qsearch" 2>/dev/null)
	[ -n "$out" ] || exit 0

	surfaced=$(printf '%s' "$out" | sed -nE 's/^  ([0-9]{4})  .*/\1/p')
	missing=""
	for id in $surfaced; do
		norm=$(printf '%s' "$id" | sed -E 's/^0*//')
		found=0
		for a in $acked; do [ "$a" = "$norm" ] && found=1 && break; done
		[ "$found" = "1" ] || missing="$missing $id"
	done
	missing=${missing# }
	[ -n "$missing" ] || exit 0 # every surfaced record accounted for: allow

	# Feedback on the ack parse (finding 2026-08-13-006, corrected form): a
	# malformed ack — prose-placed, or missing the colon — was silently
	# indistinguishable from no ack, so the operator concluded the gate was
	# broken. State what WAS parsed, so a format error is visible in the block.
	ackn=$(printf '%s\n' $acked | grep -c . || true)
	ackline="ACK PARSE: $ackn memex-checked id(s) recognized in the payload"
	[ "$ackn" = "0" ] && ackline="$ackline — if you believe you acked, the clause must be INSIDE the question/header field and use the literal token 'memex-checked:' (colon required); prose-level acks are invisible to this hook"

	samgate "$(printf '%s\n%s\n%s\n' "$out" "$ackline" \
		"OPERATOR ADJUDICATES — do not self-certify. An already-ruled question may already be settled. Read each record above. If one settles it, act on the ruling and do not ask. Otherwise put the decision in front of the operator: name them as 'memex-checked: $(printf '%s' "$missing" | tr ' ' ',' | sed 's/,/, /g')' and STATE WHAT EACH ONE RULES in one clause, so the operator judges whether it governs.")"
	;;
pre-decide)
	# PreToolUse (Skill): before /decide writes a record, surface the records it
	# may duplicate or extend. The skill's step 2 already requires this search;
	# a hook makes it FIRE rather than depend on the operator choosing to run it.
	#
	# Measured 2026-08-06: decision 0132 was written as a near-duplicate of 0013,
	# caught only because the search happened to be run by hand that time.
	payload=$(cat)
	skill=$(printf '%s' "$payload" | jq -r '.tool_input.skill // empty' 2>/dev/null)
	case "$skill" in
	decide | */decide) ;;
	*) exit 0 ;;
	esac
	args=$(printf '%s' "$payload" | jq -r '.tool_input.args // empty' 2>/dev/null)
	[ -n "$args" ] || exit 0
	out=$("$bin" -root "$root" -topic "$args" 2>/dev/null)
	[ -n "$out" ] || exit 0
	# Same records, different instruction: here they are supersession/extension
	# candidates, not a reason to stay silent.
	out=$(printf '%s' "$out" | sed \
		-e 's/^\[memex\] STOP — these accepted rulings may already answer this\..*$/[memex] Before writing a record — these may be the SAME ruling. Extend or supersede rather than duplicate (\/decide step 2):/' \
		-e 's|^  Read the record before asking\..*$|  If one says the same thing, extend it. If it says something different about the same subject, set supersedes: and flip the old record.|')
	emit PreToolUse "$out"
	;;
post-edit)
	_payload=$(cat)
	# Bash-written files count too: this hook is matched on Edit|Write|MultiEdit
	# and was dead for a whole session of Bash-only edits (decision 0204). ONE
	# shared resolver, also used by go-postedit (§4a).
	f=$(printf '%s' "$_payload" | python3 "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}/scripts/hooks/written-path.py" 2>/dev/null)
	[ -n "$f" ] || exit 0
	out=$("$bin" -root "$root" -reverse-index "$f" 2>/dev/null)
	[ -n "$out" ] || exit 0
	emit PostToolUse "$out"
	;;
cited-staged)
	# Same -reverse-index the post-edit channel uses, over the staged set instead
	# of one edited path. Prints the citing records for the first staged file that
	# has any, and stops there — the caller only needs to know THAT one exists.
	git diff --cached --name-only --diff-filter=ACMR 2>/dev/null | while IFS= read -r f; do
		[ -n "$f" ] || continue
		case "$f" in docs/memex/*) continue ;; esac
		out=$("$bin" -root "$root" -reverse-index "$f" 2>/dev/null)
		if [ -n "$out" ]; then
			printf '%s\n' "$out"
			break
		fi
	done
	;;
session-start)
	out=$("$bin" -root "$root" -catalog 2>/dev/null)
	[ -n "$out" ] || exit 0
	# Sequence-authority staleness (decision 0259, superseding the timestamp-only
	# check). A re-derive is expensive, so this fires on a THRESHOLD of unsequenced
	# work — open board rows absent from the master plan's §5 — not on every ledger
	# commit (which fired four times in one day). A grade or a phase close is the
	# other trigger, carried by 0013's cadence and an interview, not this hook.
	# Advisory — a drifting plan is a fact to see, not a turn to block; and the
	# re-derive itself is PROPOSED to Sam, never auto-run.
	REDERIVE_THRESHOLD=8
	jk="$root/scripts/hooks/joinkey-lint.py"
	if [ -f "$jk" ]; then
		# The lint command owns this count.  Its machine-readable mode keeps
		# diagnostics on stderr and prints exactly one decimal integer on stdout;
		# parsing the human report made this advisory silently disappear when the
		# report wording changed.
		unplaced=$(CLAUDE_PROJECT_DIR="$root" python3 "$jk" --unplaced-count 2>/dev/null \
			| tr -cd '0-9')
		if [ -n "$unplaced" ] && [ "$unplaced" -ge "$REDERIVE_THRESHOLD" ]; then
			out="$out
RE-DERIVE DUE: $unplaced open board rows are UNPLACED in the master plan (threshold $REDERIVE_THRESHOLD). Propose a re-derive to the operator before starting new work; until then new rows stay UNPLACED. A single grade or phase close is its own trigger, separate from this count."
		fi
	fi
	# Staging backlog (decision 0118): a ruling drafted with `id: TBD` is not
	# addressable and no gate reads it, so an unmerged staging file is invisible
	# until someone looks. Twelve files holding fifteen rulings accumulated this
	# way, the oldest for four days. Counted by `title:` because one file can
	# carry several drafts — the file count understated the backlog by three.
	nstage=$(ls "$root"/docs/memex/STAGING-*.md 2>/dev/null | wc -l | tr -d ' ')
	if [ "${nstage:-0}" -gt 0 ]; then
		nrule=$(cat "$root"/docs/memex/STAGING-*.md 2>/dev/null | grep -c '^title:')
		out="$out
STAGING BACKLOG: $nrule ruling(s) in $nstage file(s) still carry \`id: TBD\` — allocate ids and merge them into docs/memex/decisions/ before starting new work, then re-run the Memex linter and MemQ index from this checkout."
	fi
	# memq index freshness: a query against a stale index answers plausibly and
	# wrongly, which is the failure mode 0170 records. Compared against the
	# newest decision record rather than a clock — the corpus is what it indexes.
	newest=$(ls -t "$root"/docs/memex/decisions/*.md 2>/dev/null | head -1)
	db=$(ls -t "$root"/.memq/db/* 2>/dev/null | head -1)
	if [ -n "$newest" ] && [ -e "$db" ] && [ "$newest" -nt "$db" ]; then
		out="$out
STALE memq INDEX: docs/memex/decisions/ has changed since .memq/db was last written. Run \`memq index\` from this checkout before relying on a memq answer."
	fi
	emit SessionStart "$out"
	;;
stop)
	payload=$(cat)
	[ "$(printf '%s' "$payload" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ] && exit 0
	tr=$(printf '%s' "$payload" | jq -r '.transcript_path // empty' 2>/dev/null)
	[ -f "$tr" ] || exit 0
	sid=$(printf '%s' "$payload" | jq -r '.session_id // "unknown"' 2>/dev/null)

	# The turn boundary is promptSource: a real user prompt carries it, a
	# tool_result user entry does not (measured on 47 entries, 2026-08-05).
	# Emit one mark per prompt and per AskUserQuestion, streaming — a transcript
	# is too large to slurp.
	marks=$(jq -rc '
		if (.type == "user" and .promptSource != null) then
			"P\t\(.uuid)\t\(.timestamp)"
		elif .type == "assistant" then
			((.message.content // [])[] | select(.type == "tool_use" and .name == "AskUserQuestion") | "Q")
		else empty end' "$tr" 2>/dev/null)
	[ -n "$marks" ] || exit 0

	# cur = this turn, prev = the one before it, asked = an AskUserQuestion
	# resolved after cur began. The field separator is US (\037), not tab: on a
	# session's first turn prev is empty, and an IFS-whitespace delimiter
	# collapses the empty field, shifting asked onto it.
	us=$(printf '\037')
	fields=$(printf '%s\n' "$marks" | awk -F'\t' -v us="$us" '
		$1 == "P" { prev = cur; cur = $2; ts = $3; asked = 0; next }
		$1 == "Q" { asked = 1 }
		END { printf "%s%s%s%s%s%s%s\n", cur, us, prev, us, asked, us, ts }')
	oldifs=$IFS
	IFS=$us
	read -r cur prev asked ts <<-EOF
	$fields
	EOF
	IFS=$oldifs
	[ "${asked:-0}" = "1" ] || exit 0

	# Suppressed when the ruling was already recorded: a decisions/ page written
	# since this turn began.
	if [ -n "$ts" ]; then
		ref="${TMPDIR:-/tmp}/memex-stop/$sid.turnstart"
		mkdir -p "$(dirname "$ref")" 2>/dev/null
		if touch -d "$ts" "$ref" 2>/dev/null &&
			[ -n "$(find "$root/docs/memex/decisions" -name '*.md' -newer "$ref" 2>/dev/null)" ]; then
			exit 0
		fi
	fi

	# Suppressed on the second of two consecutive qualifying turns — a reminder
	# that repeats is a reminder that gets ignored.
	marker="${TMPDIR:-/tmp}/memex-stop/$sid.fired"
	[ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$prev" ] && exit 0
	printf '%s' "$cur" >"$marker" 2>/dev/null

	echo "[memex] this turn resolved an AskUserQuestion — record the ruling as an addressable page."
	;;
*)
	exit 0
	;;
esac
exit 0
