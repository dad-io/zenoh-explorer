"""M14 — classifier ground truth: locators, blind and keyed label sets, scoring, and the gate.

Every replay rate rests on a detector nobody has validated (roadmap 4.11): "real dlv session vs
word match vs none" is itself a regex, and percentages inherit its blindness. This module lets a
human validate one without Bear Hug ever copying transcript text. A sample is a stratified draw of
record LOCATORS — manifest relpath, file hash, line, tool-use block index, record hash — with the
classifier's prediction kept in a separate key file. Labels come back as a third file, may carry
two labelers, and a disagreement counts as unlabelled until someone adjudicates it in writing.

The gate: a rule may report a headline rate only when every classifier it rests on has a scored
label set that clears the thresholds. The thresholds are inputs; the defaults below are proposals
and need Sam's approval before a rate is published on their strength.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT, assert_writable
from bearhug.replay.corpus import CorpusSelection
from bearhug.replay.dlv import CLASSES as DLV_CLASSES
from bearhug.replay.dlv import DLV_CLASSIFIER_VERSION, classify_dlv_command
from bearhug.replay.ledger import bash_writes_go
from bearhug.replay.transcript import iter_records

LABELS_DIR = REPO_ROOT / "labels"

#: Proposed, not ruled. A published rate on a classifier below these needs Sam's approval.
DEFAULT_MIN_PRECISION = 0.9
DEFAULT_MIN_RECALL = 0.9
DEFAULT_MAX_UNLABELLED_SHARE = 0.1


@dataclass(frozen=True, slots=True)
class ClassifierSpec:
    name: str
    version: str
    unit: str
    classes: tuple[str, ...]
    predict: Callable[[str], str]
    instructions: str


def _go_write_class(command: str) -> str:
    return "go-write" if bash_writes_go(command) else "not-go-write"


CLASSIFIERS: dict[str, ClassifierSpec] = {
    "dlv": ClassifierSpec(
        name="dlv",
        version=DLV_CLASSIFIER_VERSION,
        unit="bash-command",
        classes=DLV_CLASSES,
        predict=classify_dlv_command,
        instructions=(
            "Open the Bash tool call at the locator and read ONLY its command. Label "
            "`real-session` if an approved debugger subcommand (`dlv test|debug|exec|attach|"
            "trace|core|connect|dap`) is what the shell would run as a command — not quoted, not "
            "inside a heredoc body, not an argument to echo/git — wherever in the command it "
            "sits, including a later line, after a heredoc, through a path (`…/bin/dlv attach`), "
            "a variable bound in the same command (`\"$DLV\" attach`), or a wrapper "
            "(`timeout 90 dlv test`). Label `word-only` if the word `dlv` appears as a token "
            "anywhere (a commit message, an echo, `dlv --help`) but no real session would start; "
            "Sam ruled 2026-09-01 that the gate's own name (`dlv-verify-gate`, `dlv.py`, a "
            "variable bound to the gate module) COUNTS as the word, because it is what the "
            "captured gate's word match accepts. Otherwise `none`."
        ),
    ),
    "go-write": ClassifierSpec(
        name="go-write",
        version="1.0.0",
        unit="bash-command",
        classes=("go-write", "not-go-write"),
        predict=_go_write_class,
        instructions=(
            "Open the Bash tool call at the locator and read ONLY its command. Label `go-write` "
            "if running it would create or modify a `.go` file: a redirect into one, `sed`/`perl "
            "-i`, `tee`, `gofmt`/`goimports -w`, or an inline interpreter that opens a `.go` path "
            "for writing. Reading, listing, building, testing, or merely mentioning a `.go` path "
            "is `not-go-write`."
        ),
    ),
}

#: Which classifiers each ledger rule rests on. A rule whose classifier has no sampler here (the
#: dispatch batch, the git verb parser, the ask-shape reimplementation) can never be validated by
#: this module and is listed so the gap is visible rather than implied.
RULE_CLASSIFIERS: dict[str, tuple[str, ...]] = {
    "git-push": ("git-verb",),
    "git-stash": ("git-verb",),
    "dlv-before-claiming": ("dlv", "go-write"),
    "review-after-write": ("go-write",),
    "one-ask-per-turn": ("ask-shape",),
    "max-two-concurrent-subagents": ("dispatch-batch",),
}


@dataclass(frozen=True, slots=True)
class Locator:
    id: str
    relpath: str
    file_sha256: str
    line: int
    block_index: int
    record_sha256: str
    predicted: str


@dataclass(slots=True)
class LabelSet:
    classifier: str
    classifier_version: str
    corpus_kind: str
    corpus_digest: str
    seed: int
    per_class: int
    population: dict[str, int] = field(default_factory=dict)
    items: list[Locator] = field(default_factory=list)

    @property
    def folder_name(self) -> str:
        return f"{self.classifier}-{self.corpus_kind}-{self.corpus_digest[:12]}"

    def as_dict(self, *, blind: bool = False) -> dict[str, Any]:
        items = []
        for item in self.items:
            row = asdict(item)
            if blind:
                row.pop("predicted")
            items.append(row)
        return {
            "schema_version": "1",
            "classifier": self.classifier,
            "classifier_version": self.classifier_version,
            "corpus_kind": self.corpus_kind,
            "corpus_digest": self.corpus_digest,
            "seed": self.seed,
            "per_class": self.per_class,
            "population": dict(sorted(self.population.items())),
            "classes": list(CLASSIFIERS[self.classifier].classes),
            "items": items,
        }


def _record_hash(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sample_locators(
    corpus: CorpusSelection, classifier: str, *, per_class: int, seed: int
) -> LabelSet:
    """A stratified, seeded draw of at most ``per_class`` locators per predicted class."""
    spec = CLASSIFIERS[classifier]
    label_set = LabelSet(
        classifier=classifier,
        classifier_version=spec.version,
        corpus_kind=corpus.kind,
        corpus_digest=corpus.digest,
        seed=seed,
        per_class=per_class,
        population={cls: 0 for cls in spec.classes},
    )
    candidates: dict[str, list[Locator]] = {cls: [] for cls in spec.classes}
    for path in corpus.paths:
        member = corpus.member_for(path)
        for line_number, record in iter_records(path):
            if record.get("type") != "assistant":
                continue
            message = record.get("message") or {}
            blocks = message.get("content") if isinstance(message, dict) else None
            if not isinstance(blocks, list):
                continue
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != "Bash":
                    continue
                command = (block.get("input") or {}).get("command")
                if not isinstance(command, str):
                    continue
                predicted = spec.predict(command)
                label_set.population[predicted] += 1
                identity = hashlib.sha256(
                    f"{corpus.digest}\0{member.relpath}\0{line_number}\0{block_index}".encode()
                ).hexdigest()[:16]
                candidates[predicted].append(
                    Locator(
                        id=identity,
                        relpath=member.relpath,
                        file_sha256=member.sha256,
                        line=line_number,
                        block_index=block_index,
                        record_sha256=_record_hash(record),
                        predicted=predicted,
                    )
                )
    rng = random.Random(seed)
    for cls in spec.classes:
        pool = sorted(candidates[cls], key=lambda item: item.id)
        chosen = pool if len(pool) <= per_class else rng.sample(pool, per_class)
        label_set.items.extend(sorted(chosen, key=lambda item: (item.relpath, item.line)))
    return label_set


def _instructions(label_set: LabelSet) -> str:
    spec = CLASSIFIERS[label_set.classifier]
    return "\n".join(
        [
            f"# Labelling `{label_set.classifier}` {label_set.classifier_version} — "
            f"{label_set.corpus_kind}:{label_set.corpus_digest[:12]}",
            "",
            "`blind.json` lists record locators and no text. Each item names a transcript by its",
            "corpus-manifest relpath and file hash, a line number, and the index of the tool-use",
            "block on that line. Open the transcript in the pinned corpus, go to the line, and",
            "read that block only. Do not read `key.json` until every label is written.",
            "",
            "## What to label",
            "",
            spec.instructions,
            "",
            f"Vocabulary: {', '.join(f'`{c}`' for c in spec.classes)}. Nothing else is accepted.",
            "",
            "## How to record labels",
            "",
            "Write `labels.json` beside this file:",
            "",
            "```json",
            '{"labels": [{"id": "<locator id>", "label": "<class>", "labeler": "<name>"}],',
            ' "adjudications": [{"id": "<locator id>", "label": "<class>", "by": "<name>",',
            '                    "note": "<why>"}]}',
            "```",
            "",
            "Use two labelers where possible. An item whose labelers disagree counts as UNLABELLED",
            "until an adjudication names the label and why. An item nobody labelled also counts",
            "as unlabelled; the report carries the unlabelled share and withholds the rate when",
            "it is high.",
            "",
            "## What the sample is",
            "",
            f"Seed {label_set.seed}; at most {label_set.per_class} per predicted class; population "
            f"by predicted class: {json.dumps(dict(sorted(label_set.population.items())))}.",
            "The population counts are the classifier's own predictions over the whole corpus,",
            "so the sample is stratified by prediction, not by truth. A class the classifier",
            "never predicts cannot be sampled here, and that absence is itself a finding.",
            "",
        ]
    )


def write_label_set(label_set: LabelSet, *, labels_dir: Path | str | None = None) -> list[Path]:
    root = Path(labels_dir) if labels_dir is not None else LABELS_DIR
    folder = root / label_set.folder_name
    folder.mkdir(parents=True, exist_ok=True)
    blind = assert_writable(folder / "blind.json")
    key = assert_writable(folder / "key.json")
    instructions = assert_writable(folder / "INSTRUCTIONS.md")
    blind.write_text(
        json.dumps(label_set.as_dict(blind=True), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    key.write_text(
        json.dumps(label_set.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    instructions.write_text(_instructions(label_set), encoding="utf-8")
    return [blind, key, instructions]


def load_label_set(path: Path | str) -> LabelSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return LabelSet(
        classifier=data["classifier"],
        classifier_version=data["classifier_version"],
        corpus_kind=data["corpus_kind"],
        corpus_digest=data["corpus_digest"],
        seed=data["seed"],
        per_class=data["per_class"],
        population=dict(data["population"]),
        items=[Locator(**item) for item in data["items"]],
    )


@dataclass(slots=True)
class Score:
    classifier: str
    classifier_version: str
    corpus_kind: str
    corpus_digest: str
    sample_size: int
    labelled: int
    disputed: tuple[str, ...]
    unlabelled: tuple[str, ...]
    class_balance: dict[str, int]
    per_class: dict[str, dict[str, float | int | None]]

    @property
    def unlabelled_share(self) -> float:
        if not self.sample_size:
            return 1.0
        return (len(self.disputed) + len(self.unlabelled)) / self.sample_size

    def validation_reason(
        self,
        *,
        min_precision: float = DEFAULT_MIN_PRECISION,
        min_recall: float = DEFAULT_MIN_RECALL,
        max_unlabelled_share: float = DEFAULT_MAX_UNLABELLED_SHARE,
    ) -> str:
        if self.labelled == 0:
            return "no labels have been recorded for this sample"
        if self.unlabelled_share > max_unlabelled_share:
            return (
                f"unlabelled share {self.unlabelled_share:.2f} exceeds {max_unlabelled_share:.2f}"
            )
        weak = []
        for cls, metrics in self.per_class.items():
            precision, recall = metrics["precision"], metrics["recall"]
            if precision is None or recall is None:
                weak.append(f"{cls}: precision or recall undefined in the sample")
            elif precision < min_precision or recall < min_recall:
                weak.append(f"{cls}: precision {precision:.2f} recall {recall:.2f}")
        if weak:
            return "below threshold — " + "; ".join(weak)
        return "validated"

    def validated(self, **thresholds: float) -> bool:
        return self.validation_reason(**thresholds) == "validated"

    def as_dict(self, **thresholds: Any) -> dict[str, Any]:
        approved_by = thresholds.pop("approved_by", None)
        status = (
            f"approved by {approved_by}"
            if approved_by
            else "proposed defaults; a published rate needs Sam's approval of these"
        )
        return {
            "schema_version": "1",
            "classifier": self.classifier,
            "classifier_version": self.classifier_version,
            "corpus_kind": self.corpus_kind,
            "corpus_digest": self.corpus_digest,
            "sample_size": self.sample_size,
            "labelled": self.labelled,
            "disputed": list(self.disputed),
            "unlabelled": list(self.unlabelled),
            "unlabelled_share": self.unlabelled_share,
            "class_balance": self.class_balance,
            "per_class": self.per_class,
            "thresholds": {
                "min_precision": thresholds.get("min_precision", DEFAULT_MIN_PRECISION),
                "min_recall": thresholds.get("min_recall", DEFAULT_MIN_RECALL),
                "max_unlabelled_share": thresholds.get(
                    "max_unlabelled_share", DEFAULT_MAX_UNLABELLED_SHARE
                ),
                "approved_by": approved_by,
                "status": status,
            },
            "validated": self.validated(**thresholds),
            "validation_reason": self.validation_reason(**thresholds),
        }


def score_labels(key: LabelSet, labels: dict[str, Any]) -> Score:
    spec = CLASSIFIERS[key.classifier]
    by_id = {item.id: item for item in key.items}
    votes: dict[str, set[str]] = {}
    for entry in labels.get("labels", []):
        if entry["id"] not in by_id:
            raise ValueError(f"label for unknown locator {entry['id']!r}")
        if entry["label"] not in spec.classes:
            raise ValueError(
                f"label {entry['label']!r} is outside the {key.classifier} vocabulary "
                f"{spec.classes}"
            )
        votes.setdefault(entry["id"], set()).add(entry["label"])
    adjudicated: dict[str, str] = {}
    for entry in labels.get("adjudications", []):
        if entry["id"] not in by_id:
            raise ValueError(f"adjudication for unknown locator {entry['id']!r}")
        if entry["label"] not in spec.classes:
            raise ValueError(
                f"adjudication {entry['label']!r} is outside the {key.classifier} vocabulary"
            )
        adjudicated[entry["id"]] = entry["label"]

    truth: dict[str, str] = {}
    disputed: list[str] = []
    unlabelled: list[str] = []
    for identity in by_id:
        if identity in adjudicated:
            truth[identity] = adjudicated[identity]
        elif identity not in votes:
            unlabelled.append(identity)
        elif len(votes[identity]) == 1:
            truth[identity] = next(iter(votes[identity]))
        else:
            disputed.append(identity)

    per_class: dict[str, dict[str, float | int | None]] = {}
    for cls in spec.classes:
        tp = sum(1 for i, t in truth.items() if t == cls and by_id[i].predicted == cls)
        fp = sum(1 for i, t in truth.items() if t != cls and by_id[i].predicted == cls)
        fn = sum(1 for i, t in truth.items() if t == cls and by_id[i].predicted != cls)
        per_class[cls] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        }
    return Score(
        classifier=key.classifier,
        classifier_version=key.classifier_version,
        corpus_kind=key.corpus_kind,
        corpus_digest=key.corpus_digest,
        sample_size=len(by_id),
        labelled=len(truth),
        disputed=tuple(sorted(disputed)),
        unlabelled=tuple(sorted(unlabelled)),
        class_balance=dict(key.population),
        per_class=per_class,
    )


def render_locators(label_set: LabelSet, corpus: CorpusSelection) -> list[dict[str, Any]]:
    """The labelling aid: each locator's Bash command, read back from the corpus for a human.

    Verified twice before anything is shown — the file's hash against the locator's
    `file_sha256`, and the record's canonical hash against `record_sha256` — so a labeler never
    grades a command the classifier did not see. A moved file yields `file-moved` and no command.
    Output is for a labeler's eyes; it is never written into a label set.
    """
    by_relpath = {corpus.member_for(path).relpath: (path, corpus.member_for(path))
                  for path in corpus.paths}
    rows: list[dict[str, Any]] = []
    for item in label_set.items:
        row: dict[str, Any] = {"id": item.id, "relpath": item.relpath, "line": item.line,
                               "block_index": item.block_index, "predicted": item.predicted,
                               "command": None, "status": "missing-file"}
        located = by_relpath.get(item.relpath)
        if located is None:
            rows.append(row)
            continue
        path, _member = located
        # Hash the file AS IT IS NOW, not the manifest's record of it: a live file can move after
        # the corpus was selected, and the labeler must not be shown a line from a different file.
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            rows.append(row)
            continue
        if actual != item.file_sha256:
            row["status"] = "file-moved"
            rows.append(row)
            continue
        record = None
        for line_number, candidate in iter_records(path):
            if line_number == item.line:
                record = candidate
                break
        if record is None or _record_hash(record) != item.record_sha256:
            row["status"] = "record-moved"
            rows.append(row)
            continue
        blocks = ((record.get("message") or {}).get("content") or [])
        try:
            block = blocks[item.block_index]
            command = (block.get("input") or {}).get("command")
        except (IndexError, AttributeError, TypeError):
            command = None
        if not isinstance(command, str):
            row["status"] = "record-moved"
        else:
            row["command"] = command
            row["status"] = "ok"
        rows.append(row)
    return rows


def _publishable(report: dict[str, Any]) -> bool:
    """A score is ``validated`` against the thresholds it was given; a RULE is validated only when
    those thresholds carry an approval. Sam, 2026-09-01: until the thresholds are approved, no rate
    is published anywhere — so the default proposals cannot publish one by themselves."""
    thresholds = report.get("thresholds") or {}
    return report.get("validated") is True and bool(thresholds.get("approved_by"))


def validated_rules(reports: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Ledger rules every one of whose classifiers has a report that is validated against
    APPROVED thresholds (see `_publishable`)."""
    return frozenset(
        rule
        for rule, classifiers in RULE_CLASSIFIERS.items()
        if classifiers and all(_publishable(reports.get(name, {})) for name in classifiers)
    )


def load_validation_reports(
    reports_dir: Path | str, *, corpus_kind: str | None = None
) -> dict[str, dict[str, Any]]:
    """Every ``ground-truth-<classifier>-*.json`` in ``reports_dir``, keyed by classifier.

    With `corpus_kind`, only that corpus's reports count — a rate measured over the frozen corpus
    is validated by the frozen labels, not by whatever the live directory scored. Without it, a
    classifier with several reports is publishable only if EVERY one of them is: the conservative
    reading, so a live report can never be shadowed by an older frozen one or vice versa.
    """
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(reports_dir).glob("ground-truth-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data.get("classifier")
        if not name:
            continue
        if corpus_kind is not None and data.get("corpus_kind") != corpus_kind:
            continue
        if name in found and corpus_kind is None:
            weaker = min((found[name], data), key=_publishable)
            found[name] = weaker
            continue
        found[name] = data
    return found


__all__ = [
    "CLASSIFIERS",
    "DEFAULT_MAX_UNLABELLED_SHARE",
    "DEFAULT_MIN_PRECISION",
    "DEFAULT_MIN_RECALL",
    "LABELS_DIR",
    "RULE_CLASSIFIERS",
    "ClassifierSpec",
    "LabelSet",
    "Locator",
    "Score",
    "load_label_set",
    "load_validation_reports",
    "render_locators",
    "sample_locators",
    "score_labels",
    "validated_rules",
    "write_label_set",
]
