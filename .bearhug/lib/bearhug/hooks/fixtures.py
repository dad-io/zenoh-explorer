"""3.1 — event payloads and synthetic transcripts, including the paths gates cannot see.

The Bash-write cases are the reason this module exists. Barracuda's own measurement found
`dlv-verify-gate.py` blind to 132 of 132 `.go` writes because they went through Bash rather
than Edit, and static analysis (2.13) can only show that a *matcher* excludes Bash — it cannot
show what a gate does when a write arrives by a path the gate never learned to look at. That
needs the gate run against a transcript where the write happened in a heredoc.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION = "f1x7u4e5-0000-4000-8000-000000000001"


@dataclass(frozen=True, slots=True)
class EventFixture:
    """One hook invocation: what goes in on stdin, and what it is meant to represent."""

    name: str
    event: str
    payload: dict[str, Any]
    transcript: list[dict[str, Any]] = field(default_factory=list)
    describes: str = ""

    def stdin(self, *, transcript_path: Path, cwd: Path) -> str:
        body = dict(self.payload)
        body.update(
            session_id=SESSION,
            transcript_path=str(transcript_path),
            cwd=str(cwd),
            hook_event_name=self.event,
        )
        return json.dumps(body)


# --- transcript records -----------------------------------------------------------------------


def user_turn(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": "2026-08-28T10:00:00Z", "sessionId": SESSION}


def tool_result() -> dict[str, Any]:
    """Delivered as a user-role message, and NOT a turn boundary. Gates rely on the difference."""
    return {"type": "user", "sessionId": SESSION, "timestamp": "2026-08-28T10:00:02Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


def assistant_turn(text: str = "", tools: list[tuple[str, dict[str, Any]]] | None = None) -> dict:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for name, tool_input in tools or []:
        content.append({"type": "tool_use", "name": name, "input": tool_input})
    return {"type": "assistant", "sessionId": SESSION, "timestamp": "2026-08-28T10:00:01Z",
            "message": {"role": "assistant", "content": content}}


def edit_go_file(path: str = "opcua/internal/svc/worker.go") -> dict[str, Any]:
    """A .go write the gates CAN see."""
    return assistant_turn("editing", [("Edit", {"file_path": path, "old_string": "a",
                                                "new_string": "b"})])


def bash_heredoc_go_write(path: str = "opcua/internal/svc/worker.go") -> dict[str, Any]:
    """The same .go write, delivered by a path four gates' matchers do not select."""
    return assistant_turn("writing it out", [("Bash", {
        "command": f"cat > {path} <<'EOF'\npackage svc\n\nfunc Work() {{}}\nEOF"})])


def bash_sed_go_write(path: str = "opcua/internal/svc/worker.go") -> dict[str, Any]:
    return assistant_turn("patching", [("Bash", {"command": f"sed -i '' 's/a/b/' {path}"})])


def bash_dlv_session() -> dict[str, Any]:
    return assistant_turn("stepping it", [("Bash", {
        "command": "dlv test ./opcua/internal/svc -- -test.run TestWork"})])


def bash_mentions_dlv_only() -> dict[str, Any]:
    """Satisfies a word-boundary matcher without a debugger ever running (plan 3.9)."""
    return assistant_turn("committing", [("Bash", {
        "command": 'git commit -m "hooks: dlv-verify-gate notes"'})])


def write_transcript(records: list[dict[str, Any]], path: Path) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


# --- the fixture corpus -------------------------------------------------------------------------

#: A turn that edited Go through Edit, then never ran a debugger.
_EDIT_NO_DLV = [user_turn("fix the worker"), edit_go_file(), tool_result(),
                assistant_turn("Done — it works.")]

#: The SAME turn, with the write delivered by a Bash heredoc instead.
_BASH_NO_DLV = [user_turn("fix the worker"), bash_heredoc_go_write(), tool_result(),
                assistant_turn("Done — it works.")]

#: A turn-final reply long enough (well over 2,000 chars) and with zero engagement point (no "?",
#: no ask construction) to trip response-shape.py's (0135) no-ask-with-length branch, which
#: demands the reply be re-sent SHORTER — while the .go edit with no dlv session in the same turn
#: trips dlv-verify-gate.py, which demands a dlv session be RUN. Two Stop gates, one turn,
#: opposite remediations: this is 3.10's positive case.
_LONG_REPLY_TEXT = (
    "Finished the worker refactor across internal/svc/worker.go. The retry loop previously held "
    "the mutex through the entire backoff sleep, which serialized every worker in the pool "
    "regardless of which queue it was actually draining, and under load that turned into a "
    "convoy where fast workers waited behind one slow lease renewal.\n\n"
    "I split the state machine into three explicit phases: claim, execute, and release. The "
    "claim phase now grabs the lease under the lock, records the deadline, and releases the lock "
    "immediately so other workers can proceed. The execute phase runs entirely outside the "
    "critical section, including the backoff sleep on failure, and only re-acquires the lock in "
    "the release phase to mark the lease as done or to requeue it. This matches the "
    "isolation-of-io-from-consensus pattern already used in the scheduler package, so the shape "
    "should read as familiar to anyone who has touched that code.\n\n"
    "A few details worth recording here. The old code computed the backoff duration before "
    "checking whether the context was already cancelled, so a cancelled worker would still sleep "
    "out its full backoff before noticing the cancellation and exiting; the new code checks "
    "ctx.Err() immediately before sleeping and returns early when it is set, so shutdown is "
    "prompt instead of bounded by the longest configured backoff. I also removed the shared "
    "counter that tracked in-flight leases, since it was only read for a metric nobody graphs "
    "anymore and it was one more thing serialized under the same lock; if that metric turns out "
    "to matter it should be rebuilt as an atomic counter rather than folded back into the mutex.\n"
    "\n"
    "The error wrapping changed too. Previously a failed lease renewal was logged and swallowed "
    "at the point of failure, which meant the caller had no way to distinguish a transient "
    "renewal failure from a permanent one and always fell back to the same retry path. Now "
    "renewal failures are typed: a network timeout gets wrapped as a retryable error and rejoins "
    "the backoff loop, while a lease-not-found error is treated as terminal and the worker exits "
    "that claim immediately rather than retrying against a lease that no longer exists on the "
    "coordinator. This removes a class of log spam where a worker would retry a claim for "
    "minutes after the coordinator had already reassigned it elsewhere, which was one of the "
    "noisier entries in the on-call runbook.\n\n"
    "Test coverage for the new state machine lives in worker_test.go and exercises "
    "claim/execute/release independently with a fake clock, plus one end-to-end test that runs "
    "the full loop against an in-memory coordinator and asserts the lock is never held across a "
    "sleep by instrumenting the mutex with a wrapper that panics on reentry during a simulated "
    "delay. All of that passed locally along with the existing suite for the package, and go vet "
    "came back clean on the changed files.\n\n"
    "Net effect: the lock is now held for microseconds per claim instead of for the full backoff "
    "window, cancellation is immediate instead of bounded, and lease-not-found no longer "
    "masquerades as a retryable failure. This closes out the queued cleanup work for the worker "
    "package for this pass."
)

_LONG_REPLY_GO_EDIT = [user_turn("fix the worker"), edit_go_file(), tool_result(),
                       assistant_turn(_LONG_REPLY_TEXT)]


def corpus(project_dir: Path) -> list[EventFixture]:
    """Every fixture, named. `project_dir` is the disposable repo, never the subject."""
    go_file = "opcua/internal/svc/worker.go"
    return [
        EventFixture(
            "posttooluse-edit-go", "PostToolUse",
            {"tool_name": "Edit",
             "tool_input": {"file_path": str(project_dir / go_file)},
             "tool_response": {"filePath": str(project_dir / go_file)}},
            describes="a .go file edited through the Edit tool — the path gates were built for",
        ),
        EventFixture(
            "posttooluse-bash-heredoc-go", "PostToolUse",
            {"tool_name": "Bash",
             "tool_input": {"command": f"cat > {project_dir / go_file} <<'EOF'\npackage svc\nEOF"},
             "tool_response": {"stdout": "", "exitCode": 0}},
            describes="the SAME .go write through a Bash heredoc — no file_path in the payload",
        ),
        EventFixture(
            "posttooluse-edit-go-unformatted", "PostToolUse",
            {"tool_name": "Edit",
             "tool_input": {"file_path": str(project_dir / "opcua/internal/svc/messy.go")},
             "tool_response": {"filePath": str(project_dir / "opcua/internal/svc/messy.go")}},
            describes="a misformatted, vet-dirty .go file — the POSITIVE case for go-postedit, "
                      "without which that gate's silence elsewhere means nothing",
        ),
        EventFixture(
            "pretooluse-bash-safe", "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "go build ./..."}},
            describes="an ordinary build command, which nothing should block",
        ),
        EventFixture(
            "pretooluse-bash-push", "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}},
            describes="a push — §0 forbids it, so hard-safety.py must deny",
        ),
        EventFixture(
            "pretooluse-bash-push-via-heredoc", "PreToolUse",
            {"tool_name": "Bash",
             "tool_input": {"command": "bash -c 'git push origin HEAD'"}},
            describes="the same push wrapped in bash -c — decision 0282 states the gate is blind",
        ),
        EventFixture(
            "stop-go-edit-no-dlv", "Stop", {"stop_hook_active": False},
            transcript=_EDIT_NO_DLV,
            describes="a turn that edited Go via Edit and ran no debugger — dlv gate should block",
        ),
        EventFixture(
            "stop-go-bash-write-no-dlv", "Stop", {"stop_hook_active": False},
            transcript=_BASH_NO_DLV,
            describes="the same turn with the write via Bash heredoc — the blindness under test",
        ),
        EventFixture(
            "stop-go-edit-with-dlv", "Stop", {"stop_hook_active": False},
            transcript=[*_EDIT_NO_DLV[:-1], bash_dlv_session(), assistant_turn("Verified.")],
            describes="a real dlv session followed the edit — the gate should stay quiet",
        ),
        EventFixture(
            "stop-go-edit-dlv-word-only", "Stop", {"stop_hook_active": False},
            transcript=[*_EDIT_NO_DLV[:-1], bash_mentions_dlv_only(), assistant_turn("Done.")],
            describes="a commit message mentioning dlv — satisfies a word matcher, ran no debugger",
        ),
        EventFixture(
            "stop-long-reply-go-edit", "Stop", {"stop_hook_active": False},
            transcript=_LONG_REPLY_GO_EDIT,
            describes="the positive case for 3.10 stop-arbitration — a genuinely long, "
                      "no-engagement-point closing reply (response-shape.py demands SHORTER) "
                      "on a turn that also edited .go with no dlv session (dlv-verify-gate.py "
                      "demands a dlv session be run) — two Stop gates blocking one turn with "
                      "remediations pulling in opposite directions",
        ),
        EventFixture(
            "stop-docs-only", "Stop", {"stop_hook_active": False},
            transcript=[user_turn("update the readme"),
                        assistant_turn("edited", [("Edit", {"file_path": "README.md"})]),
                        tool_result(), assistant_turn("Updated the readme.")],
            describes="a docs-only turn — no code, so code gates should stay quiet",
        ),
        EventFixture(
            "userpromptsubmit", "UserPromptSubmit",
            {"prompt": "what does the worker do?"},
            describes="an ordinary prompt, for the injection path",
        ),
        EventFixture(
            "sessionstart", "SessionStart", {"source": "startup"},
            describes="a cold session start",
        ),
    ]
