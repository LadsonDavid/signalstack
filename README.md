# signalstack

A self-hosted engine that tells you which companies to contact this week, and why.

Most prospecting tools sell you a database. This is a pipeline: it scrapes public signals (hiring boards, tech stacks, status pages, forum posts, GitHub activity), scores accounts on fit and intent, and gives you a ranked list with a reason attached to every entry. You run it on your own infrastructure for the cost of a few API calls, not a per-seat license.

```bash
pip install -r requirements.txt
python -m app.cli init
python -m app.cli seed seeds.txt
python -m app.cli run ats          # discover boards + hiring/tech signals
python -m app.cli run techstack    # fingerprint tech stacks
python -m app.cli serve            # http://localhost:8000
```

## What it does

Give it a `config.yaml` describing your ideal customer (which integrations they'd have, what pains they post about, which titles buy your product) and a seed list of company domains. It runs a set of collectors against public sources, diffs each result against what it saw last time, and writes every change to an append-only log. Nothing gets thrown away.

From that log it computes two numbers per account:

- **Fit** — how closely they match your ICP. Static, doesn't decay.
- **Intent** — how much they've done recently that looks like buying behavior. Decays with a half-life you control.

Score is fit gating intent, so a perfect-fit account with zero activity still shows up (worth a cold email) and a bad-fit account with a lot of noise doesn't crowd out your feed.

## Why scores are never stored

Every score is recomputed at read time from the log, not written to a column and left to rot. Change a weight in `config.yaml` and every account re-scores instantly — no re-scrape, no migration, no cron job creeping stale point values forward. The log is the only source of truth; everything downstream is a query over it.

```
collectors ──▶ snapshot ──▶ diff ──▶ signal log (append-only) ──▶ score (read-time) ──▶ board
```

## Collectors

A collector is two methods:

```python
def fetch(self) -> Iterable[Record]          # the only thing a new source writes
def signals(self, rec, prev) -> Iterable[Sig]
```

Everything else — timeouts, circuit breakers, dedup, run logging — lives once in [`app/collectors/base.py`](app/collectors/base.py). A new source inherits all of it for free.

Sources shipped out of the box:

| Source | What it gives you | Auth |
|---|---|---|
| ATS boards (Greenhouse, Lever, Ashby...) | hiring signal, team size, what they're hiring for | none |
| Tech stack fingerprinting | which tools an account runs | none |
| Status page incident feeds | who's under operational pressure right now | none |
| Hacker News, Lobsters, Dev.to, Mastodon | who's talking about the problem you solve | none |
| GitHub stargazers | who's evaluating tools in your category | token |
| Vendor customer pages | confirmed usage of tools you integrate with | none |

Discovery collectors (HN hiring threads, YC directory) surface companies nobody typed into `seeds.txt` — a 40-line seed list can turn into 800+ accounts on the first run. `python -m app.cli prune` cleans out the ATS shorteners and recruiting tools that discovery tends to drag in.

## Running it

SQLite on a volume, one process, one user. No Postgres, no Kafka, no queue — the architecture assumes a single tenant, because that's what self-hosting means. Deploys to Railway for roughly $10–17/month depending on which paid enrichment steps you turn on (email verification, LLM classification are both optional and gated so spend stays predictable).

```bash
cp .env.example .env      # fill in what you have; everything is optional
cp config.example.yaml config.yaml
# edit config.yaml: your ICP, your integrations checklist, your persona terms
python -m app.cli init
python -m app.cli seed seeds.txt
python -m app.cli run ats
python -m app.cli serve
```

With nothing in `.env` set, you still get the free collectors and a working score. Add a `GITHUB_TOKEN` to raise your API limit, an `LLM_API_KEY` if you want forum posts classified for intent, an email verification key if you want deliverability checked before you send.

## Web UI

A small feed/board/detail interface ships with the CLI's `serve` command — three ways to look at the same log: a chronological feed of new signals, a sorted board of accounts, and a detail view per account showing every signal that contributed to its score.

## License

Apache 2.0 — see [LICENSE](LICENSE).
