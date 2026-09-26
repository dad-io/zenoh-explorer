"""Provider-specific extraction of one strict machine-readable final response."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ProviderFinalOutputError(ValueError):
    """A provider run has no unambiguous strict JSON final response."""


_MAX_OUTPUT_BYTES = 256 * 1024


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderFinalOutputError(f"provider JSON repeats key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProviderFinalOutputError(f"provider JSON contains non-standard constant {value!r}")


def _json(value: bytes | str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_closed_object,
        parse_constant=_reject_constant,
    )


def _records(raw: bytes) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n"):
        raise ProviderFinalOutputError("provider stream is not newline-terminated JSONL")
    values: list[dict[str, Any]] = []
    for ordinal, line in enumerate(raw.splitlines(), start=1):
        try:
            value = _json(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderFinalOutputError(
                f"provider stream line {ordinal} is invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ProviderFinalOutputError(f"provider stream line {ordinal} is not an object")
        values.append(value)
    return values


def _text(run: Any) -> str:
    receipt = getattr(run, "receipt", None)
    if not isinstance(receipt, dict):
        raise ProviderFinalOutputError("provider run has no receipt")
    provider = receipt.get("provider")
    if provider == "anthropic-claude":
        path = getattr(run, "raw_events_path", None)
        if not isinstance(path, Path):
            raise ProviderFinalOutputError("Claude run has no raw event path")
        terminal = [value for value in _records(path.read_bytes()) if value.get("type") == "result"]
        if len(terminal) != 1 or not isinstance(terminal[0].get("result"), str):
            raise ProviderFinalOutputError("Claude stream has no unique textual result")
        return terminal[0]["result"]
    if provider == "openai-codex":
        path = getattr(run, "server_events_path", None)
        if not isinstance(path, Path):
            raise ProviderFinalOutputError("Codex run has no server event path")
        messages: list[str] = []
        for value in _records(path.read_bytes()):
            if value.get("method") != "item/completed":
                continue
            params = value.get("params")
            item = params.get("item") if isinstance(params, dict) else None
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text)
        if not messages:
            raise ProviderFinalOutputError("Codex stream has no completed textual agent message")
        return messages[-1]
    raise ProviderFinalOutputError(f"unsupported provider final-output surface: {provider!r}")


def _stream_records(path: Path):
    with path.open("rb") as source:
        for line in source:
            yield _records(line)[0]


def provider_terminal_failure(run: Any) -> str | None:
    """Describe an unsuccessful transport turn before attempting to parse model output.

    This is diagnostic text, never a success or retry authorization. The runtime separately
    validates and preserves the receipt's raw custody before recording the failed episode.
    """
    receipt = getattr(run, "receipt", None)
    if not isinstance(receipt, dict) or receipt.get("terminal_state") not in {
        "failed", "incomplete"
    }:
        return None
    provider = receipt.get("provider")
    detail = None
    if provider == "openai-codex":
        path = getattr(run, "server_events_path", None)
        if isinstance(path, Path):
            for value in _stream_records(path):
                params = value.get("params")
                if value.get("method") != "turn/completed" or not isinstance(params, dict):
                    continue
                turn = params.get("turn")
                if not isinstance(turn, dict):
                    continue
                if (params.get("threadId"), turn.get("id")) != (
                    receipt.get("thread_id"), receipt.get("turn_id")
                ):
                    continue
                error = turn.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    detail = error["message"]
    # Codex sometimes wraps the useful API error message in an encoded JSON envelope.
    if detail:
        try:
            envelope = _json(detail)
        except (ValueError, UnicodeError):
            envelope = None
        if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
            message = envelope["error"].get("message")
            if isinstance(message, str):
                detail = message
        detail = " ".join(detail.split())[:2000]
    label = "Codex" if provider == "openai-codex" else "Provider"
    return f"{label} turn {receipt['terminal_state']}" + (f": {detail}" if detail else ".")


def _object_spans(text: str) -> list[str]:
    """Every balanced ``{...}`` span at the top level of the text, in order.

    Scanned with string and escape awareness so a brace inside a JSON string never opens
    or closes a span. Nested objects are part of their parent's span, not candidates.
    """

    spans: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(text[start : index + 1])
                start = None
    return spans


def strict_final_json(run: Any) -> dict[str, Any]:
    """Return the one JSON object in the provider's terminal text.

    Whatever surrounds the object is ignored: a sentence before it, a Markdown fence with
    or without a language tag, a sign-off after it. None of that is a second answer, and
    refusing it discarded episodes whose result was complete and correct.

    The invariant that remains is the one that matters: exactly one object. Two distinct
    top-level objects are still refused, because choosing between them would be a guess.
    Whichever object is selected goes through the same parser as before, so duplicate keys
    and non-standard constants are still rejected.
    """

    failure = provider_terminal_failure(run)
    if failure is not None:
        raise ProviderFinalOutputError(failure)
    text = _text(run)
    try:
        raw = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProviderFinalOutputError("provider final response is not valid UTF-8") from exc
    if not raw or len(raw) > _MAX_OUTPUT_BYTES:
        raise ProviderFinalOutputError("provider final response is empty or exceeds its bound")

    stripped = text.strip()
    candidates: list[str] = []
    if stripped.startswith("{"):
        # A bare object is the common case, but "{...} Done." also starts with a brace,
        # so take the whole text only when the whole text is the object.
        try:
            if isinstance(json.loads(stripped), dict):
                candidates = [stripped]
        except ValueError:
            candidates = []
    if not candidates:
        seen: list[str] = []
        for span in _object_spans(text):
            try:
                if not isinstance(json.loads(span), dict):
                    continue
            except ValueError:
                continue
            normalized = " ".join(span.split())
            if normalized not in seen:
                seen.append(normalized)
                candidates.append(span)
        if len(candidates) > 1:
            raise ProviderFinalOutputError(
                "provider final response contains more than one JSON object"
            )
    if not candidates:
        raise ProviderFinalOutputError("provider final response contains no JSON object")
    try:
        value = _json(candidates[0])
    except json.JSONDecodeError as exc:
        raise ProviderFinalOutputError(
            "provider final response must be one bare JSON object"
        ) from exc
    if not isinstance(value, dict):
        raise ProviderFinalOutputError("provider final response must be a JSON object")
    return value


__all__ = ["ProviderFinalOutputError", "provider_terminal_failure", "strict_final_json"]
