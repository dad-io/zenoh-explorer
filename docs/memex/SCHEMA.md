# Memex decision record schema

The `docs/memex/` directory is a small, addressable record of decisions and related knowledge
for one repository. It contains references to the repository's sources; it is not a copy of those
sources. Keep this contract with the repository so a fresh checkout can validate its own records.

## Layout

```text
docs/memex/
├── SCHEMA.md          # this contract
├── index.md           # required catalog
├── log.md             # append-only operation journal
├── decisions/         # decisions/NNNN-lowercase-hyphenated-slug.md
├── concepts/          # optional non-decision explanations
├── syntheses/         # optional arguments combining records
└── overviews/         # optional topic maps
```

`index.md` is required even when the corpus is empty. List each page exactly once with a relative
Markdown link. Use `## Decisions` for records whose status is `proposed` or `accepted`, and
`## Historical` for `superseded` or `rejected` records. Keep `log.md` append-only; each operation
is a dated heading such as `## [YYYY-MM-DD] decide | <description>`.

`SCHEMA.md`, `README.md`, `index.md`, `log.md`, and files named `STAGING-*.md` are control files.
They are not pages. A concurrent writer may draft a `STAGING-*.md` file with `id: TBD`; the
allocator assigns a permanent id before moving it into `decisions/`.

## Page types

The validator recognizes `decision`, `concept`, `synthesis`, and `overview`. Decision records use
`decisions/NNNN-<slug>.md`; the other types use their matching directory and a lowercase,
hyphenated slug. Add a page type to this table and to the validator before using it.

## Decision frontmatter

Every decision record has this shape. The angle-bracket values are instructions, not values for a
record.

```yaml
---
title: "<distinctive subject of the ruling>"
type: decision
id: <four-digit permanent id>
status: proposed            # proposed | accepted | superseded | rejected
date: <YYYY-MM-DD>
decided_by: <operator or confirmed operator+assistant>
supersedes: []
superseded_by: null
tags: []
ruling_verbatim: |
  <exact operator wording, or a restatement the operator confirmed>
reviewed_by: <optional named advisor and date>
execution: executed         # optional: unstarted | partial | executed
evidence: []                # path#NamedThing or a bare repository-relative path
spec: []                    # optional: Part N §... citations
sources:                    # where the ruling was already stated or evidenced
  - <repository-relative source>
related: []                 # repository-relative page paths; links are reciprocal
---
```

Required fields are `title`, `type`, `id`, `status`, `date`, `decided_by`, `ruling_verbatim`, and
`sources`. The id is four digits in both frontmatter and the filename, is assigned once, and is
never reused. A title should contain a term distinctive enough for topic search to find the
record from its own subject.

`ruling_verbatim` preserves the operator's exact words. When a decision was reached
collaboratively and no exact words exist, ask the operator to confirm the restatement and identify
the authorship accordingly. Do not invent wording to fill the field. `status: proposed` records an
open question and is never authority. `execution` is optional; when present it is one of
`unstarted`, `partial`, or `executed`, and only an accepted decision can be unstarted or partial.
Use `supersedes` and `superseded_by` together when a new record replaces an older one; keep the
older record and place it under `Historical` in the catalog.

Evidence anchors use a named symbol or heading (`path#Name`) when one exists, or a bare path when
the source has no named item. A line number alone is not a durable anchor. Only add `spec` entries
when the cited clause has been checked against the authoritative local copy. `related` links are
reciprocal. Every source that repeats the ruling should point back to the decision record.

## Body and maintenance

Use these sections for a decision, in order:

```markdown
## Context
What required a ruling, with sources.

## Decision
What the operator decided.

## What this forbids
The acts ruled out and the permitted alternative.

## Consequences
What follows, including accepted costs.

## Alternatives rejected
The options considered and why they were not chosen.
```

Read `index.md` and existing records before writing. Extend an existing record when it says the
same thing. If a new ruling changes an accepted one, stop for the operator's direction, then add a
new record and supersede the old record rather than rewriting its history. Never create a decision
from an inference or copy private project data into this directory.

The empty corpus is valid: keep `index.md`, `log.md`, and `decisions/`, and do not fabricate an
initial decision. Run the bundled validator after changes and inspect every `checked=` count; a
zero-count check did not examine any records.
