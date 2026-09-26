"""WORK.json v2 view generators and the content-completeness gate.

Purpose
    Bear Hug's WORK.json v2 (frozen contract: the scratchpad's `workv2-contract.md`, mirrored by
    the golden fixtures the importer and this module both build against) holds Barracuda's
    plan/board/ledger state as a flat, ordered list of "units" per source doc: every source line
    becomes exactly one unit (`kind: "row"` for a classified board queue row, `kind: "block"` for
    everything else — headers, sub-tables, legends, blanks, ledger and plan prose). This module
    turns that structure back into text, and proves the round trip lost nothing.

Responsibilities
    - `render_views(workv2)`: regenerate one text blob per source doc, keyed by the doc's
      relative path (from `workv2["source_docs"]`). The regenerated *format* is explicitly NOT
      required to be byte-identical to the original — only every unit's `src_line` content has to
      be present in the regenerated output. This module's implementation emits each doc's units
      in original order, one `src_line` per output line, so the round trip is trivially lossless;
      it does not attempt cell-level reconstruction (`cells`/`fields` are the importer's derived
      index, not authoritative — `src_line` is authority per the contract).
    - `assert_content_complete(workv2, sources)`: the losslessness gate. For each doc named in
      `workv2["source_docs"]`, checks that every line of the *original* source text (as supplied
      in `sources`, keyed by the same relative path) is present both in the stored units'
      `src_line`s and in `render_views`'s regenerated output for that doc. Raises `ValueError`
      naming the first missing line (by relative path and 1-based line number) it finds, walking
      the original text top to bottom, so the first failure reported is always the first line
      that would actually be lost.

Interfaces / dependencies
    - Stdlib only (CLAUDE.md: phases 1-4 are stdlib-only by design).
    - Input contract (`workv2: dict`): `{"schema": "bearhug-work/2", "source_docs": {<doc_key>:
      <relpath>, ...}, <doc_key>: {"units": [<unit>, ...]}, ...}` for `doc_key` in
      `{"board", "ledger", "master_plan"}`. Each unit has at least `"kind"` and `"src_line"`; row
      units additionally carry `"board_row"`, `"cells"`, `"fields"` — this module never reads
      those, only `"src_line"`.
    - `sources: dict[str, str]` for `assert_content_complete`: relative path (matching
      `workv2["source_docs"]` values) -> full original file text, newline-joined. Callers building
      this from real files should read with `splitlines()`-equivalent semantics; this module
      compares by splitting on `"\n"` to mirror how `src_line`s were captured (one unit per
      `text.split("\n")` element, matching the frozen contract's own reconstruction check).
    - Consumed by the WORK.json importer/CLI (a separate module owned elsewhere in this
      migration): it imports `render_views` by this exact name and signature —
      `render_views(workv2: dict) -> dict[str, str]` — to write regenerated views, and may call
      `assert_content_complete` before trusting a freshly-built v2 document.

Invariants
    - `render_views` output is deterministic: same input always produces the same dict (no
      wall-clock, randomness, or dict-ordering dependence beyond the input's own `source_docs`
      insertion order and each doc's own unit order).
    - `render_views` never drops or reorders units within a doc.
    - `assert_content_complete` treats presence as exact-line-string membership (a set built from
      `"\n".split`), matching the contract's own "line-set-equals" invariant: multiplicity is not
      checked, only that no original line's content is absent.
    - Neither function mutates `workv2`.

Testing
    `tests/test_work_views_v2.py` uses hand-authored v2 fixtures (this module has no dependency on
    the importer, so it does not need real Barracuda docs to unit test): a rich board row whose
    Notes cell has an inline-code pipe, an ADVANCES-style sub-table block line, a ledger prose
    line, and a blank line. It asserts `render_views` output carries every fixture `src_line`, that
    `assert_content_complete` passes on a matching-good fixture, and that it raises when a line is
    dropped from either the stored units or the regenerated output.
"""

from __future__ import annotations


def render_views(workv2: dict) -> dict[str, str]:
    """Regenerate one text blob per source doc named in ``workv2["source_docs"]``.

    Returns a map of relative-path -> regenerated text. Each doc's units are emitted in order, one
    unit's ``src_line`` per output line, joined with ``"\\n"``. Byte-identity with the original
    source is explicitly not required (and not attempted); the only requirement enforced by
    ``assert_content_complete`` is that every original line's content survives somewhere in the
    corresponding output.
    """
    views: dict[str, str] = {}
    for doc_key, relpath in workv2["source_docs"].items():
        units = workv2[doc_key]["units"]
        views[relpath] = "\n".join(unit["src_line"] for unit in units)
    return views


def assert_content_complete(workv2: dict, sources: dict[str, str]) -> None:
    """Raise ``ValueError`` if any original source line was lost.

    ``sources`` maps each doc's relative path (matching ``workv2["source_docs"]`` values) to that
    doc's full original text. For each doc, every line of the original text must appear, verbatim,
    both as some unit's ``src_line`` (the stored-units check) and in ``render_views``'s regenerated
    output for that doc (the regeneration check). The first missing line encountered — walking the
    original text top to bottom, across docs in ``source_docs`` order — is named precisely in the
    raised message, by relative path and 1-based line number.
    """
    source_docs = workv2["source_docs"]
    rendered = render_views(workv2)

    for doc_key, relpath in source_docs.items():
        if relpath not in sources:
            raise ValueError(
                f"assert_content_complete: no original text supplied for {relpath!r} "
                f"(doc {doc_key!r}); sources must be keyed by workv2['source_docs'] paths"
            )
        units = workv2[doc_key]["units"]
        stored_lines = {unit["src_line"] for unit in units}
        rendered_text = rendered.get(relpath, "")
        rendered_lines = set(rendered_text.split("\n"))

        original_lines = sources[relpath].split("\n")
        for line_no, line in enumerate(original_lines, start=1):
            if line not in stored_lines:
                raise ValueError(
                    f"content-completeness violation: {relpath}:{line_no} missing from stored "
                    f"{doc_key} units (src_line not found): {line!r}"
                )
            if line not in rendered_lines:
                raise ValueError(
                    f"content-completeness violation: {relpath}:{line_no} missing from "
                    f"render_views output for {doc_key} ({relpath}): {line!r}"
                )
