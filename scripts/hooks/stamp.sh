#!/bin/sh
# Best-effort last-run telemetry; observing a hook never changes its verdict.

stamp() {
	_name=$1
	_verdict=${2:-ran}
	# CLAUDE_PROJECT_DIR is set for Claude Code hooks; git rev-parse covers the
	# git-hook callers. Either failing means no stamp, never an error.
	_root=${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}
	[ -n "$_root" ] || return 0
	_dir=$_root/.automation-stamps
	mkdir -p "$_dir" 2>/dev/null || return 0
	printf '%s %s\n' "$(date +%s)" "$_verdict" >"$_dir/$_name" 2>/dev/null || return 0
	return 0
}

# Standalone invocation: scripts/hooks/stamp.sh <name> [verdict]
case "${0##*/}" in
stamp.sh)
	[ $# -ge 1 ] && stamp "$@"
	exit 0
	;;
esac
