# Your Ask box's rules

This is the uvularia **rules** template — the `<org>-ask-rules` repository. It
holds everything that shapes *how your Ask box answers*, and nothing that shapes
*what is true*. The facts live in your records vault; this repository is the box's
instructions, its policy, its manual overrides, and the golden set of questions
that gate every change to them.

That separation is the whole point. A change here can reword the box or take it
offline; it can never edit a record. A change to a record can never edit a rule.
The two repositories have different reviewers and different blast radii on
purpose.

You do not need to be a programmer to run it. You edit a Markdown file or a short
policy file and open a pull request; the checks run on their own.

---

## 1. Use this template

**What you are about to do:** make your own copy of the rules repository in your
GitHub organization and point it at your records vault.

**Why bother:** this is the one repository that governs the assistant. Setting its
reviewers and its published corpus URL once is what lets every later change — a
reworded instruction, a new refusal, a postponed-meeting notice — be a reviewed,
reversible pull request.

**How long:** about ten minutes.

1. Click **Use this template → Create a new repository**. Name it
   `<your-org>-ask-rules`. It can be public.
2. In [`policy.yaml`](policy.yaml), set `published_base` to your records vault's
   published URL (for example `https://<your-org>.github.io/<your-org>-records`).
   The box and the eval gate both read `corpus-latest.json` from there.
3. Set your reviewers in [`.github/CODEOWNERS`](.github/CODEOWNERS) (replace the
   placeholder), then in **Settings → Branches** add a protection rule for `main`:
   require a pull request and require the **evals** status check to pass.
4. Read [`instructions.md`](instructions.md) and, if you like, name your
   organization in it. Read [`policy.yaml`](policy.yaml) and trim the
   `allowed_subjects` and `refusal_classes` to your organization.
5. Merge your first change (even just the edits above). The **release** workflow
   tags `rules-v1` and attaches `policy.yaml` and `instructions.md` as the
   release your Ask function pins.

**How you know it worked:** pull requests show a green **evals** check, and your
**Releases** page has a `rules-v1` with the two files attached.

---

## 2. What is in here

| File | What it does |
|---|---|
| [`instructions.md`](instructions.md) | the **frozen system block** — the first thing the model reads, ahead of your records. How the box decides, and the rules that do not bend. |
| [`policy.yaml`](policy.yaml) | allowed subjects, refusal classes, the not-legal-advice disclaimer, the daily cap, the model pin, the `enabled` kill switch, and your records vault's published URL. |
| [`signals/incidents.toml`](signals/incidents.toml) | the manual override — declare a postponed meeting or a closed trail and the box leads with it until you clear it. |
| [`evals/golden.jsonl`](evals/golden.jsonl) | the questions the box must get right, with their expected outcome and required citations. See [`evals/README.md`](evals/README.md). |

### The kill switch

`enabled: false` in [`policy.yaml`](policy.yaml), merged and released, takes the
box down cleanly: it returns a short maintenance line and answers nothing until
you set it back. It is the fastest lever you have and it needs no code change.

---

## 3. Change the box, safely

**What you are about to do:** reword an instruction, add a refusal, or add a
question to the golden set, and see the effect before it ships.

**Why bother:** an assistant breaks quietly. A pull request here re-runs the whole
golden set against your **currently published** corpus and posts, on the pull
request, what changed in the prompt and what the box now does with each golden
question — so a reviewer reads the effect, not just the wording.

**How long:** a few minutes, plus the check.

1. Edit the file and open a pull request.
2. The **evals** check runs the golden set in **dry** mode — a structural pass
   that needs no API key and must come back clean. If you set the
   `MITCHELLA_API_KEY` secret, it also runs a **live** pass that asks every
   question for real and scores the answers against the threshold in
   `policy.yaml`.
3. Read the comment it posts: the prompt diff (what changed in the instructions
   and policy) and the answer diff (what the box does with each golden question).

**How you know it worked:** the **evals** check is green, the comment shows the
outcomes you expected, and — once merged — a new `rules-vN` release carries the
change. To roll back, pin the previous release.

---

## How releases pin the box

On merge to `main`, [`release.yml`](.github/workflows/release.yml) tags
`rules-v<N>` and attaches [`policy.yaml`](policy.yaml) and
[`instructions.md`](instructions.md) as release assets. Your Ask function pins a
version and advances only when you cut a new one; rolling back is pinning the
previous `rules-vN`. A release is the unit of "this is how the box behaves now",
and it is reversible.

---

> Part of [uvularia](https://github.com/lentago/uvularia) by Lentago Labs.
> Firing us is a fork: this template runs in your org, on your account, for free.
