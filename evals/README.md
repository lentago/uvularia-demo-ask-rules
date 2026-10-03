# The golden set — how you keep the Ask box honest

**What this is:** a list of questions your box should get right, each paired with
the outcome you expect and the records that back it. It is the test suite for an
assistant: when you change the box's instructions, its policy, or your records, a
pull request re-runs the whole list and shows you what moved.

**Why bother:** an assistant is easy to break without noticing — a reworded
instruction quietly starts declining questions it used to answer, or a retracted
record pulls the ground out from under an answer. The golden set turns "it still
works, I think" into a check that is green or red.

---

## How a check runs on a pull request

Two passes, and the one that gates every pull request needs no API key and costs
nothing:

- **Dry** (always on). Reads your **currently published** corpus and checks each
  question structurally, without calling a model:
  - every record id you said is required actually exists in the published corpus;
  - the expected outcome fits those ids — an `answered` question has to cite at
    least one real record; a `declined` or `escalated` one cites none, because by
    definition the records do not answer it;
  - every subject you list maps to a known record (it is a tag the corpus carries,
    or it appears in a record's id or title).

  Dry mode must pass completely. It is what catches a typo'd id, a question
  pointed at a record you retracted, or an expected outcome that contradicts
  itself.

- **Live** (only where a key is set). Loads the same published corpus into the
  Ask engine and asks every question for real, then scores the engine's outcome
  and citations against the golden set. It passes above the `eval.threshold` in
  [`policy.yaml`](../policy.yaml). This is the only pass that spends tokens, so it
  runs only when the `MITCHELLA_API_KEY` secret is present.

Run them yourself:

```
python3 evals/run.py                         # dry, reads policy.yaml for the corpus URL
python3 evals/run.py --bundle ./corpus-latest.json
python3 evals/run.py --mode live --report report.md
```

---

## Writing a golden entry

One JSON object per line in [`golden.jsonl`](golden.jsonl):

```json
{"id": "board-size", "question": "How many trustees are on the board?", "expect": "answered", "source_ids": ["2024-03-20-bylaws"], "subjects": ["bylaw"], "note": "The bylaws fix it at seven."}
```

| Field | What it is |
|---|---|
| `id` | a short, stable handle for this question |
| `question` | what a member of the public would type |
| `expect` | the outcome: `answered`, `incident`, `escalated`, or `declined` |
| `source_ids` | record ids the answer must rest on (required for `answered`; none for `declined`/`escalated`) |
| `subjects` | topics the question is about; each must map to a record you publish |
| `note` | why you expect this, for the next person who reads the file |

The four outcomes:

- **answered** — the records answer it, and the box cites them.
- **declined** — out of scope; the box says so and does not guess.
- **escalated** — in scope but not in the records; the box says it is not
  documented and points the reader to the organization.
- **incident** — a live override applies. This outcome only occurs when a
  matching open incident is declared in [`signals/incidents.toml`](../signals/incidents.toml)
  or an obligation is in breach, so it is meaningful in live mode only with that
  signal present.

**Make it yours.** The questions shipped here are written against the uvularia
demonstration vault so the check is green out of the box. Replace them with the
questions your members actually ask, pointed at your own records. Start with the
ten you answer most often by email.
