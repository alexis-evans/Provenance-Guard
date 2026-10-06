# Provenance Guard

For this project, I built a Flask API that checks text for signs of AI generation.
It combines three detection signals, explains uncertainty, and lets creators
appeal an assessment. It also includes reviewed provenance certificates and
a small analytics dashboard. A result describes writing patterns; it does not prove
who wrote the text.

## Setup and use

From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create `.env` using [.env.example](.env.example), then add your Groq API key:

```dotenv
GROQ_API_KEY=your_key_here
GROQ_MODEL=qwen/qwen3.8-27b
GROQ_REASONING_EFFORT=none
```

Start the server:

```bash
python -m flask --app app run --host 127.0.0.1 --port 5000
```

The home URL shows the available routes. In another terminal, submit the
included example and read the log:

```bash
curl -sS http://127.0.0.1:5000/submit -H 'Content-Type: application/json' --data-binary @examples/submit_request.json | python3 -m json.tool
curl -sS http://127.0.0.1:5000/log | python3 -m json.tool
```

`/submit` needs a JSON POST request; opening it in a browser sends GET instead.
SQLite saves records in `instance/provenance_guard.sqlite3`, which survives
restarts. `.env` and runtime databases are excluded from Git.

## Architecture

A submission first passes the rate limiter and input checks. Groq, Python
stylometry, and a disclosure check analyze the same text. Their scores are combined, a label
is selected, and SQLite saves the decision and audit entry together before the
API responds. The diagrams and original design are in [planning.md](planning.md).

An appeal loads the original decision, saves the creator's reasoning, changes
its status to `under_review`, and adds a linked audit entry. These writes happen
in one transaction, so a failure cannot leave only part of the appeal saved.

| Endpoint                      | Purpose                                                          |
| ----------------------------- | ---------------------------------------------------------------- |
| `GET /`                     | Show API information                                             |
| `POST /submit`              | Analyze`text` from a `creator_id`                            |
| `GET /content/<content_id>` | Retrieve the decision and current review status                  |
| `POST /appeal`              | Contest a decision using`content_id` and `creator_reasoning` |
| `GET /log` | Read decision and appeal audit events; supports `limit` and `offset` |
| `GET /dashboard` | View detection patterns, appeals, and certificates |
| `GET /analytics` | Retrieve dashboard totals as JSON |
| `GET /content/<content_id>/view` | View scores, transparency label, and certificate badge |
| `POST /verification/request` | Request a review of a draft and writing process |
| `GET /verification/<request_id>` | Read private evidence (reviewer token required) |
| `POST /verification/<request_id>/review` | Approve or reject verification (reviewer token required) |

## Ensemble detection

| Signal            | What it checks and why I chose it                                                                                                      | Main limitation                                                                |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Groq assessment   | Phrasing, repeated patterns, and consistency across sentences. It can consider meaning and context.                                    | Formal human writing can look generated, and casual AI writing can look human. |
| Python stylometry | Sentence-length variation, vocabulary diversity, and punctuation density. These are repeatable measurements that complement the model. | Short text, poetry, and different writing styles can break the assumptions.    |

The third signal is **explicit AI disclosure**. A local phrase check looks for
wording such as “as an AI language model” or “written by ChatGPT.” It returns
1 when a cue appears and a neutral 0.5 otherwise, with matched cue IDs in the
response. This checks a stated origin, while Groq checks context and stylometry
checks structure. Quotes and negations can produce misleading matches, and
removing a disclosure defeats this signal.

The structural score uses 70% sentence uniformity and 30% vocabulary repetition.
Punctuation density is saved for inspection but has no scoring weight. The
formulas and word-count rules are in [the plan](planning.md#stylometry-signal).
The signals can share biases, so agreement is not proof of authorship.

## Confidence and transparency labels

```text
ai_score = 0.55 * groq_score + 0.35 * stylometry_score + 0.10 * disclosure_score
confidence = max(ai_score, 1 - ai_score)
```

I gave Groq the most weight because it considers context. Stylometry adds a
structural check, and disclosure has a smaller weight because it is easy to
remove or quote. These weights are design choices, not measured accuracy rates.
Every new response includes `signals`, `weights`, and `policy_version: ensemble-v1`.
The content page displays each signal score beside the combined result.

Scores of **0.90 or higher** mean `likely_ai`; **0.20 or lower** mean
`likely_human`; the middle range is `uncertain`. The higher AI threshold reflects
the cost of wrongly accusing a human writer. Fewer than 50 words or 3 sentences,
or a difference above 0.40 between informative signals, forces uncertainty.
Groq and stylometry always count toward disagreement; disclosure only counts
when a cue is present. A neutral 0.5 is not evidence of human authorship. These
rules override the weighted result, so averaging cannot hide a conflict. If
any signal fails, the combined score and confidence are null.

Confidence shows how strongly the scores lean in one direction, not the chance
that the answer is correct. For example, AI scores of 0.60 and 0.40 both produce
confidence 0.60 and an uncertain result. The API includes the reasons for uncertainty.

| Variant               | Exact label text                                                                                                            |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| High-confidence AI    | "This text shows strong signs of AI generation. This automated assessment can be wrong. Creators can appeal."               |
| High-confidence human | "This text shows strong signs of human writing. This automated assessment does not verify authorship. Creators can appeal." |
| Uncertain             | "We cannot confidently determine whether this text is human-written or AI-generated. Creators can appeal."                  |

### Actual submission examples

These texts and scores come from the [saved live evaluation](examples/m4_verification.json).
They use the earlier `heuristic-v1` policy (60% Groq, 40% stylometry), not the
new ensemble. They show different outputs, not proven detection accuracy.

**Higher-confidence example**

> Remote work offers significant benefits for organizations seeking to improve productivity and strengthen collaboration across diverse professional teams. Remote work provides valuable opportunities for employees seeking to improve flexibility and maintain balance across their professional responsibilities. Remote work creates meaningful advantages for businesses seeking to improve efficiency and support growth across their operational activities. Remote work delivers important benefits for managers seeking to improve communication and encourage engagement across their distributed teams. Remote work represents a promising approach for organizations seeking to improve performance and achieve success across their evolving workplaces.

```json
{
  "attribution": "likely_ai",
  "ai_score": 0.9027472527472528,
  "confidence": 0.9027472527472528,
  "uncertainty_reasons": []
}
```

**Lower-confidence example**

> I've been thinking a lot about remote work lately. There are genuine tradeoffs — flexibility and no commute on one side, isolation and blurred work-life boundaries on the other. Studies show productivity varies widely by individual and role type.

```json
{
  "attribution": "uncertain",
  "ai_score": 0.6173707337022094,
  "confidence": 0.6173707337022094,
  "uncertainty_reasons": [
    "insufficient_text",
    "signal_disagreement",
    "middle_range"
  ]
}
```

The second example has 39 recognized words. Its result is uncertain because of
length, disagreement, and the middle score range. A longer formal-human excerpt
also returned uncertain, with confidence 0.7994, because the signals disagreed.

## Appeals

Copy a returned content ID into [examples/appeal_request.json](examples/appeal_request.json)
and add the creator's explanation, then send:

```bash
curl -sS http://127.0.0.1:5000/appeal -H 'Content-Type: application/json' --data-binary @examples/appeal_request.json | python3 -m json.tool
```

A successful appeal returns HTTP 201. `GET /content/<content_id>` then shows
`under_review`, the reasoning, and this notice:

> The creator has contested this assessment. It is under review.

The original score and label stay unchanged. Missing content returns 404,
invalid input returns 400, and another appeal for the same submission returns
409. The app collects appeals but does not automatically reclassify text or
resolve a review.

## Provenance certificate

A creator can earn a **Verified human** badge for a specific submission by
providing an earlier draft and explaining their writing process. A reviewer
reads the evidence and holds a live conversation with the creator to check
identity and authorship. Approval requires an explicit reviewer attestation;
a favorable detection score alone never issues a certificate.

The certificate stores a UUID, creator ID, reviewer name, issue time, and the
SHA-256 digest of the exact submitted text. It is a local database-backed
credential, not a portable signed certificate. It does not verify the creator's
other work. The content page and dashboard show a green **Verified human** badge
separately from the automated transparency label, which stays unchanged even
when the two disagree. `GET /content/<content_id>` also includes the certificate.

To enable review, generate a secret with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'`, then set
`REVIEWER_TOKEN` and `REVIEWER_NAME` in `.env` and restart Flask. Export the same
`REVIEWER_TOKEN` in the terminal used for reviewer commands. Tokens shorter than
32 characters leave review disabled.

1. Submit your text through `/submit` and save its `content_id`.
2. Create `instance/verification_request.json` with the fields below. `text`
   must match the submission exactly, including whitespace. Supply your actual
   draft (20–20000 characters) and explanation (40–5000 characters).

```json
{
  "content_id": "UUID from /submit",
  "text": "The exact text originally submitted",
  "draft": "An earlier draft of your writing",
  "explanation": "Explain how you developed and revised this particular piece."
}
```

```bash
curl -sS http://127.0.0.1:5000/verification/request -H 'Content-Type: application/json' --data-binary @instance/verification_request.json
```

3. Give the returned `request_id` to the reviewer. Replace `REQUEST_ID` below
   with that ID. The reviewer reads the evidence, completes the live review,
   and only then approves:

```bash
curl -sS http://127.0.0.1:5000/verification/REQUEST_ID -H "Authorization: Bearer $REVIEWER_TOKEN"
curl -sS http://127.0.0.1:5000/verification/REQUEST_ID/review -H "Authorization: Bearer $REVIEWER_TOKEN" -H 'Content-Type: application/json' -d '{"approve":true,"attestation":true}'
```

To reject instead, send `{"approve":false,"attestation":false}`. A rejected
request cannot be approved later, but the creator can submit a new request.
Approval and certificate creation commit together. Only one certificate can
exist per submission. Old submissions without a text hash must be resubmitted.
Verification requests retain their review status separately from the immutable
detection and appeal log.

This is a reviewer attestation, not technical proof of authorship. The app
cannot enforce that the conversation took place. There are no creator accounts,
so the reviewer must verify identity; knowing the original text is not proof
of ownership. Certificates currently have no expiry or revocation workflow.

## Analytics dashboard

Open **http://127.0.0.1:5000/dashboard** after starting Flask. It shows:

- **Detection patterns:** counts and percentages for AI, human, and uncertain verdicts.
- **Appeal rate:** submissions with an appeal divided by all submissions.
- **Certificate rate:** certified submissions divided by all submissions.

Each submission counts once. Appeals do not add to the denominator, and empty
storage shows zero values. The page also lists policy versions and the latest
20 submissions. Click a creator to see individual scores and the separate
certificate badge. Refresh to update; `/analytics` exposes the same totals as
JSON. These are activity metrics, not detection accuracy measurements.

For a populated demonstration without using Groq or changing your database:

```bash
python -m scripts.demo_stretch
```

Open **http://127.0.0.1:5001/dashboard**. This uses a temporary database, three
controlled verdicts, one appeal, and one simulated certificate. The banner and
reviewer name identify it as a demo. It does not represent a real human review.
Use `python -m scripts.demo_stretch --check` to run the same workflow and print
its scores, certificate, and metrics without starting a server.

## Rate limiting

Submissions are limited to **10 per minute and 100 per day per client IP**.
This allows several revisions while limiting bursts and sustained provider use.
The configuration is in [provenance_guard/__init__.py](provenance_guard/__init__.py).
Flask-Limiter uses fixed windows, `request.remote_addr`, and `memory://` storage.
Invalid submissions count toward the limit; rejected requests do not call Groq.
Appeals and log reads stay available when the submission quota is exhausted.

The HTTP test returned:

```text
200 200 200 200 200 200 200 200 200 200 429 429
```

The two 429 responses included `Retry-After`. The [saved evidence](examples/m5_rate_limit_evidence.json)
uses real HTTP, Flask-Limiter, and SQLite with a controlled Groq signal, so the
burst test does not spend provider quota. The daily cap is tested with a
controlled clock. Local counters reset on restart and are not shared between
workers; Groq's own quotas are separate.

## Audit log

Each decision records its timestamp, content ID, attribution, confidence,
individual signal scores, label, status, and policy version. An appeal adds its
reasoning and the original decision's event ID without replacing that decision.

These are selected fields from [four live audit entries](examples/m5_verification.json).
Confidence is rounded here; the JSON report contains full scores and signals.

| Event ID | Type     | UTC timestamp               | Attribution  | Confidence | Status       |
| -------- | -------- | --------------------------- | ------------ | ---------- | ------------ |
| 40       | decision | 2026-10-05T02:49:14.455271Z | likely_ai    | 0.9027     | classified   |
| 41       | decision | 2026-10-05T02:49:14.832597Z | likely_human | 0.9733     | classified   |
| 42       | decision | 2026-10-05T02:49:15.215317Z | uncertain    | 0.7994     | classified   |
| 43       | appeal   | 2026-10-05T02:49:15.219491Z | uncertain    | 0.7994     | under_review |

Appeal 43 links to decision 42. Its reasoning identifies the submitted passage
as public-domain writing by Charles Darwin and asks for a review of how formal
writing affected the structural signal. The linked report includes the full
reasoning and content before and after the appeal.

## Testing and results

```bash
python -m unittest discover -v
```

All **54 tests pass**. Stretch tests also cover disclosure conflicts, reviewer
authorization, certificate persistence and rollback, concurrent approval, HTML
escaping, and dashboard counts. They cover text measurements, score boundaries, provider
errors, exact labels, request validation, concurrent appeals, rollback, and rate
limits. The [live workflow report](examples/m5_verification.json) also shows
three actual Groq submissions under the earlier two-signal policy reaching
all three labels, followed by an appeal and duplicate rejection.

For the earlier two-signal policy, four development cases were each run three times, then
four additional longer examples were checked. Scores stayed the same across
the repeated calls. Among the eight longer inputs, 3/8 were uncertain, none of
the four known-human excerpts was labeled AI, and two of the four known-AI
examples were incorrectly labeled human. The [inputs](examples/m4_inputs.json)
and [results](examples/m4_verification.json) include their sources. This small
set is not enough to estimate general accuracy.

## Known limitations

- **Conversational AI can pass as human.** Both AI rewrites fooled the system:
  Groq favored their casual voice, while stylometry favored irregular sentences.
- **Formal prose and poetry can be misread.** Uniform sentences and repeated
  words can raise the structural score even for human writing.
- **Text handling is English-focused.** Abbreviations can inflate sentence
  counts, and the word pattern handles accented and non-English words poorly.
- **Confidence is not calibrated.** The examples are few, classic passages may
  be familiar to the model, and the edited samples are AI rewrites rather than
  genuine human edits.
- **This is a local prototype.** There is no login or ownership check for
  appeals. Anyone with an ID can appeal, and log access exposes stored reasoning.
  Submitted text is sent to Groq; the decision stores its hash, not a raw-text
  field, but model explanations may repeat parts of it. Verification drafts
  and explanations are stored locally and require the reviewer token to read
  through the API. Prompt injection and provider failures remain risks.

For a real deployment, I would prioritize a larger known-origin test set,
authenticated creator and reviewer access, a review-resolution process, and
shared rate-limit storage. The scores should not be used as automatic proof
against a writer.

## Spec reflection

The plan helped by defining thresholds and failure behavior before coding.
Those rules became tests, including the 0.20 and 0.90 boundaries and the rule
that a missing signal must not become a zero score.

A few things changed during implementation:

- The suggested Scout model was unavailable to the account, so the app uses
  `qwen/qwen3.8-27b` with reasoning disabled to fit the small output budget.
- Early versions used temporary labels without an appeal invitation until the
  appeal endpoint existed. Final labels now match the plan; older audit entries
  keep their original wording.
- Longer public-domain human excerpts and AI-edited examples replaced the
  planned original-human and human-edited samples. Genuine human edits remain
  a gap in the evaluation.
- Decimal comparisons were added to keep floating-point rounding from changing
  a threshold result. SQLite write locking was added alongside the unique
  appeal constraint to handle simultaneous requests safely.
- `GET /` was added after opening the server's home URL returned 404.
- Live testing hit Groq's quota, so the verification runner was paced and given
  a resume option. Saved reports were kept separate from later reruns.

The stretch extension changes the weights to 55/35/10 and versions new decisions
as `ensemble-v1`. Historical decisions and reports retain their original scores.
The existing milestone 5 runner now writes reruns to `instance/` and uses a
repeated-text fixture for its controlled AI case. The new ensemble has automated
workflow checks, but has not had a fresh live accuracy evaluation.

## AI usage

I used AI assistance for planning, implementation, tests, and documentation.
The revisions below were made during the assisted coding process.

| Request                                               | AI output                                                            | Revision or check                                                                                     |
| ----------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Turn the assignment into a plan before coding         | Architecture diagrams, signal contracts, and API design              | Made uncertainty thresholds and appeal edge cases specific instead of forcing a binary answer.        |
| Build the Flask endpoint, Groq signal, and SQLite log | App factory, adapter, storage functions, and tests                   | Tightened validation to reject duplicate JSON keys, boolean scores, and incomplete responses.         |
| Add stylometry and combined scoring                   | Measurement functions, scoring code, and evaluation scripts          | Checked boundaries with decimal comparisons and kept the observed false negatives in the results.     |
| Add appeals and rate limiting                         | Final labels, appeal transaction, lookup endpoint, and limiter setup | Tested duplicate appeals and rollback; separated controlled rate-limit tests from live Groq evidence. |

The stretch features were also built with AI assistance and checked with
automated policy, review-access, storage, and dashboard tests.
