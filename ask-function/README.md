# The Ask function

A small AWS Lambda that runs [mitchella](https://github.com/lentago/mitchella)'s
engine as the brain behind your site's Ask box. It answers only from your
published records, leads with what the board shows when an obligation is in
breach, cites the records it used, and refuses the rest. It is the Platform-tier
piece of uvularia (Phase 1); the vault, the board, and the rules repo stand on
their own without it.

## What you're about to do

Deploy one function into **your own AWS account** with `terraform apply`. It
reads three things at the versions you pin:

- your records, as the published `corpus-<digest>.json` on your vault's Pages;
- your rules — `policy.yaml` and `instructions.md` — from a `rules-vN` release;
- your live state — `standing.json` and the announcement `feed.xml` — so the box
  cannot claim compliance the board denies.

It puts a public Function URL in front of mitchella and nothing else: no
database of questions, no login, no stored identity. The site's Ask widget POSTs
a question to the URL and renders the reply and the citations.

## Why bother

Because the box is only safe if it is boxed in. This function is where the
hardening lives, and each guard has a reason:

- **The key is never in an environment variable.** It sits in an SSM
  SecureString and is read once at cold start. (solidago's ask-lambda kept it in
  plaintext; this does not.)
- **The daily cap is durable.** One atomic counter per UTC day in DynamoDB, so
  the ceiling holds across every warm container — not `containers × cap`.
- **A kill switch you can throw in seconds.** Set `enabled: false` in
  `policy.yaml`, cut a release, and within the refresh window the box returns a
  maintenance line and answers nothing. No code change, no redeploy.
- **Citations are the ones the engine verified**, not the passages that were
  sent. An id the model invents is dropped before you ever see it.
- **A bot check in front** (Cloudflare Turnstile), off by default so local and CI
  runs need no secret, on when you face the open web.
- **CORS locked to one origin** — your site, and nothing else in a browser.

## How long

About **15 minutes** the first time, most of it one-time account setup:

1. Put your Anthropic API key in SSM as a SecureString:
   ```
   aws ssm put-parameter --name /uvularia/anthropic-key --type SecureString \
     --value sk-ant-...
   ```
2. Copy `templates/ask-function/` from the uvularia source into your rules repo
   as `ask-function/` (the deploy workflow expects it there), or deploy the
   module directly.
3. Set the module's variables (a `terraform.tfvars`), at minimum:
   ```hcl
   published_base_url         = "https://your-org.github.io/your-org-records"
   rules_repo                 = "your-org/your-org-ask-rules"
   allowed_origin             = "https://your-org.github.io"
   anthropic_api_key_ssm_path = "/uvularia/anthropic-key"
   ```
4. `terraform init && terraform apply`. The build step `pip install`s mitchella
   and anthropic into the package (needs `python3`, `pip`, and `zip` on the
   machine running the apply).
5. Put the `function_url` output into your site repo's Ask-widget config.

For CI instead of a laptop, use the deploy workflow shipped in the rules template
(`.github/workflows/deploy-ask.yml`): it plans on a pull request and applies on
merge over GitHub OIDC, so no AWS keys live in a secret.

## How you know it worked

- `curl` the URL with a question and read the JSON:
  ```
  curl -s "$FUNCTION_URL" -H 'content-type: application/json' \
    -d '{"question":"are the trails open?"}' | jq
  ```
  You should get `{"kind":"answered","reply":"…","source_ids":["…"],"degraded_signals":[]}`
  with a real record id in `source_ids`.
- Ask about something your board shows in breach: the reply leads with the
  standing, and `kind` is `incident`, not `answered`.
- Flip `policy.yaml` to `enabled: false`, cut a release, wait the refresh window,
  and the same question comes back `{"kind":"maintenance",…}`.
- The turn log in CloudWatch shows the outcome, the latency, and the question
  truncated to 500 characters — and no origin, IP, or identity.

## Check on it: `GET /health`

The same URL answers `GET /health` with what the box is serving now. It handles no
question, uses none of the day's cap, and calls no model:

```
curl -s "$FUNCTION_URL/health" | jq
{"status":"ok","enabled":true,"digest":"3f9a…","digest_pinned":false,
 "rules_tag":"rules-v4","day":"2026-10-04","cap_used":37,"cap":500}
```

`digest` is the corpus the box answers from (`null` while it is paused).
`cap_used` is today's count (`null` if the counter could not be read). The reply
never carries the key, an SSM path, or anything about who asked, and it gets the
same CORS headers as a question. Put this URL in your records vault's
`ASK_HEALTH_URL` variable. The vault's **watch** workflow then opens an issue if
the box falls behind a publish, or if the day's cap passes 80 %.

## Watch it in Grafana (optional)

**What you are about to do:** have the function send two kinds of short event to
a free Grafana Cloud account you own. Grafana Cloud is a hosted dashboard service;
its log store is called **Loki**.

- `served`: each time the box re-polls, the corpus digest and rules tag it is now
  answering from;
- `asked`: once per question, the outcome (`kind`), how long it took, how much
  of today's cap is used and left, which signals degraded, and the question cut
  to 500 characters. **Never** the asker's origin, IP address, or anything else
  that says who asked.

**Why bother:** next to your vault's and rules repo's events, you can see that the
box is serving the digest you just published and how it's answering. **Skip this
if** CloudWatch's turn log is enough. With `loki_push_url` empty the function sends
nothing and reads no extra secret.

**How long:** about ten minutes if you already have the Grafana stack and the
write-only token from the records vault's README (**Watch the pipeline**).

1. Put the token pair in SSM as a SecureString, the same way as the API key:
   ```
   aws ssm put-parameter --name /uvularia/loki-write-token --type SecureString \
     --value '123456:glc_...'
   ```
2. Add two variables to `terraform.tfvars` and apply:
   ```hcl
   loki_push_url             = "https://logs-prod-NNN.grafana.net"
   loki_write_token_ssm_path = "/uvularia/loki-write-token"
   ```
   Optionally set `loki_cluster` to your organization's short name. The default
   is your rules repo's owner, lowercased. With the deploy workflow, set the
   repository variables `LOKI_PUSH_URL` and `LOKI_WRITE_TOKEN_SSM_PATH` (and
   optionally `LOKI_CLUSTER`) instead.

Sending is **best-effort**. Each push has a two-second limit. If Grafana is down
or the token can't be read, CloudWatch gets one `telemetry_warning` line and the
answer goes out unchanged.

**How you know it worked:** ask one question, then in Grafana open **Explore** →
Loki and run `{source="uvularia", pipeline="ask"} | json`. You'll see a `served`
event from the first refresh and an `asked` event for your question.

## What it costs

- **Lambda, DynamoDB, CloudWatch: free tier.** The function is invoked per
  question; DynamoDB is one tiny write per answered question on a provisioned
  1/1 table (inside the always-free 25-unit tier), with a TTL that deletes old
  day-counters for free.
- **Model tokens are the only real cost, and the cap is the ceiling.** A corpus
  of a few hundred records sits under mitchella's cache breakpoint, so each
  question is one cached-prefix read (roughly a tenth of input price) plus a
  short Sonnet completion — pennies. The `daily_cap` bounds the month no matter
  what traffic arrives. You hold your own Anthropic key and see your own bill.

## How to turn it off

- **Pause it:** set `enabled: false` in `policy.yaml` and cut a release. Fastest
  lever; reversible by flipping it back.
- **Lower the ceiling:** drop `daily_cap` in `policy.yaml` (it overrides the
  module's default) to throttle without taking the box down.
- **Remove it entirely:** `terraform destroy`. The vault, the board, and the
  rules repo are untouched — the site simply loses its Ask box.

## The response shape

```json
{
  "kind": "answered | incident | escalated | declined | maintenance",
  "reply": "the text to show the reader",
  "source_ids": ["ids the engine verified against the corpus"],
  "degraded_signals": ["any signal source that could not be read"]
}
```

`source_ids` carries only citations the engine's gate confirmed are real
records. `degraded_signals` is non-empty when a signal source (standing,
announcements, overrides) could not be read — the box still answers, but it tells
you it was working with less than the full picture.

## Layout

| Path | What it is |
|---|---|
| `src/handler.py` | the Function URL entrypoint and the request guards, in order |
| `src/engine_build.py` | fetches the pinned rules + corpus and assembles mitchella's engine |
| `src/cap.py` | the durable DynamoDB daily cap (atomic conditional increment) |
| `src/secrets.py` | reads the Anthropic key from SSM at cold start |
| `src/turnstile.py` | the bot check (off by default) |
| `src/policy.py` | reads the kill switch, model, cap, and disclaimer from `policy.yaml` |
| `src/config.py` | the deploy-time settings, from the environment |
| `src/telemetry.py` | the optional `served` / `asked` events to your Grafana Cloud Loki; a no-op when unset |
| `src/loki_push.py` | drosera's Loki push client, vendored unchanged (the header names the commit) |
| `terraform/` | the module: Lambda, Function URL, DynamoDB, log group, least-privilege role |
| `tests/` | unit tests (no key, no network) + a live integration test over the demo vault |
| `requirements.txt` | mitchella (pinned by commit) and anthropic; boto3 is in the runtime |

## Boundaries

- One org, one function, one key — no multi-tenancy. Lentago operates nothing.
- Nothing Lentago-specific is baked in: the role ARN, the state key, the SSM
  path, and the origin are all variables. The demonstration client's values come
  from the lentago/solidago sandbox at deploy time.
