# Praxis

> *A reading of your AI practice. Praxis scans your Claude, Codex, and Copilot chat history, scores your usage against research-backed criteria, and coaches you on what to do differently.*

A local CLI tool that reads your AI assistant chat history from disk, scores your usage against research-backed criteria, tracks your learning trajectory over time, and gives per-model coaching grounded in each model's documented strengths and weaknesses.

Built for practitioners, not theorists.

---

## What it does

Praxis has three layers:

### 1. Technique score — *how well do you prompt?*

Six research-backed dimensions, weighted, summed to /10:

| Dimension | Weight | Evidence base |
|---|---|---|
| Planning before prompting | 20% | Sarkar 2025 — experienced agent users plan first; +6% accept rate per SD of experience |
| Context richness | 20% | OpenRouter State of AI 2025 — prompt length grew 4× to 6K tokens |
| Iteration & evaluation | 18% | Sarkar 2025 — abstraction, clarity, evaluation as core skills |
| Tool & multi-step use | 14% | OpenRouter State of AI 2025 — agentic patterns rising |
| Model–task fit | 14% | OpenRouter 'Glass Slipper' retention; cross-model fluency |
| Verification habits | 14% | Anthropic safety/grounding; hallucination literature |

### 2. Learning trajectory — *are you growing or atrophying?*

Beyond scoring technique, Praxis tracks behavioral patterns across all your sessions over time:

- **Engagement signals**: "why" questions, comprehension checks, follow-up depth
- **Atrophy signals**: pure delegation, outsourced debugging, telegraphic prompts
- **Independence signals**: showing your own attempt before asking

It then picks one trajectory label: **Learning**, **Engaged**, **Passive**, or **Atrophying**.

This layer is grounded in Shen & Tamkin (2026, *How AI Impacts Skill Formation*, arXiv 2601.20245), an Anthropic-affiliated RCT of 52 developers learning a new Python library. The AI-assisted group scored 17 percentage points lower on a comprehension quiz, with the biggest gap in debugging — the exact skill needed to supervise AI. Anthropic's 2026 qualitative study of 81,000 users found 16.3% explicitly worried about cognitive atrophy from AI use; 24% among teachers.

Praxis finds the same patterns in your own chat history, with both heuristic detection and LLM-judged analysis.

### 3. Per-model advisor — *are you using the right model for the task?*

Praxis ships with model cards for Claude (Opus 4.7, Sonnet 4.6, Haiku 4.5), GPT (5, 5-mini, 4o), Gemini 2.5 Pro, and GitHub Copilot. For each model you actually use, it produces:

- **Fit assessment**: well-matched, over-using, under-using, or mixed
- **Specific advice**: prompting quirks for that model, tasks better suited to other tiers
- **Card strengths/avoids**: from the model's documented best-for and avoid-for lists

You can add your own model cards by dropping JSON files in `~/.praxis/model_cards/`. The system supports custom cards because models ship faster than this code can keep up.

---

## What it reads

Praxis scans chat session files where your AI tools store them:

- **Claude Code** — `~/.claude/projects/**/*.jsonl`
- **Codex CLI** — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- **GitHub Copilot** — VS Code workspace storage (`chatSessions/*.json` and `state.vscdb` SQLite)

Your chat content never leaves your machine, except as a sampled transcript sent to the LLM judge (Claude or OpenAI) you authorize.

## Install

```bash
pipx install praxis
```

Or from source:

```bash
tar xzf praxis.tar.gz
cd praxis
pipx install .
```

Set your API keys (at least one):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

Praxis runs heuristics-only without keys, but the deep insight comes from the LLM-as-judge layer.

## Usage

```bash
# Run the full pipeline: scan + score + behavior + model advice + consolidate + render
praxis scan

# Open the HTML report
praxis scan --open

# Fast/free mode — no API calls
praxis scan --no-judge

# Inspect the scoring rubric
praxis rubric

# List loaded model cards
praxis models

# See one card in detail
praxis models --show claude-opus-4-7

# Check status
praxis status
```

## Run it daily

```bash
praxis install-daemon
```

Prints platform-specific config:
- **macOS**: LaunchAgent plist
- **Linux**: systemd user timer
- **Windows**: Task Scheduler command

Runs once a day, picks up new sessions, regenerates the trajectory and model advice.

## Model cards: where they come from and how to update them

Praxis ships with eight built-in cards under `praxis/data/builtin_cards/*.json` — one JSON file per model. Each card carries everything needed to advise on its use: strengths, weaknesses, prompting quirks, what it's good and bad for, and per-token pricing.

### Where the data comes from

Each card cites the vendor pages it was assembled from in its `sources` field, plus a `pricing_source` URL for the per-token cost. Verify before relying on the dashboard for budget decisions — pricing changes:

| Family | Card docs | Pricing |
|---|---|---|
| Claude (Opus / Sonnet / Haiku) | <https://docs.claude.com/en/docs/about-claude/models/overview> | <https://www.anthropic.com/pricing#api> |
| GPT-5 / GPT-5 mini / GPT-4o | <https://platform.openai.com/docs/models> | <https://openai.com/api/pricing/> |
| Gemini 2.5 Pro | <https://ai.google.dev/gemini-api/docs/models> | <https://ai.google.dev/pricing> |
| GitHub Copilot | <https://docs.github.com/en/copilot> | <https://github.com/features/copilot/plans> |

Pricing values in the built-in cards were verified on the dates listed in each card's `pricing_last_verified` field. Run `praxis models --show <id>` to see the full card including the verification date and source URL.

### Updating built-in cards

You don't need to rebuild the package. The loader reads the JSON files directly. To bump pricing or strengths:

```bash
# Find the card file
ls $(.venv/bin/python -c "import praxis, pathlib; print(pathlib.Path(praxis.__file__).parent / 'data' / 'builtin_cards')")

# Edit the JSON, then verify
praxis models --show claude-opus-4-7
```

The cards are cached in memory after the first read in a given process, so changes are picked up next run.

### Adding your own card

Drop a JSON file in `~/.praxis/model_cards/` using the same schema. User cards override built-ins with the same `id`. Minimal schema:

```json
{
  "id": "my-custom-model",
  "family": "custom",
  "display_name": "My Custom Model",
  "vendor": "Vendor Name",
  "tier": "frontier",
  "context_window_tokens": 200000,
  "strengths": ["..."],
  "weaknesses": ["..."],
  "prompting_quirks": ["..."],
  "best_for": ["..."],
  "avoid_for": ["..."],
  "notes": "...",
  "sources": ["https://vendor.example.com/docs/my-model"],
  "aliases": ["alt-name", "shortname"],
  "input_per_million_usd": 1.50,
  "output_per_million_usd": 6.00,
  "pricing_last_verified": "2026-05-01",
  "pricing_source": "https://vendor.example.com/pricing",
  "pricing_notes": "Optional caveats — context-window tiers, image surcharges, etc."
}
```

The pricing fields are optional. Set them to `null` for subscription-based products.

### Cost estimates in the report

Praxis derives a rough per-window cost estimate from your actual prompt-character volume per model:

- ~4 chars per token (English text)
- Output tokens ≈ 1.5× input tokens (typical for coding workloads)
- No prompt-caching or batch-API discounts applied

These assumptions make the figure conservative for short prompts and roughly right for typical chat. The HTML report's per-model cards show the cost basis explicitly, and total spend appears in the "Estimated spend" callout above the model grid. Subscription-only products (Copilot) are excluded from the total and called out separately.

Treat the dashboard cost as a signal ("Opus is dominating my spend"), not a budget instrument. For invoiced costs, use the vendor's billing console.

## Honest framing

This is not a daemon that runs continuously in memory. It's a scheduled job that runs once a day, picks up new chat sessions from disk, scores them, and refreshes your profile.

The "continuous learning" comes from three places:
1. **Idempotent re-runs** — every session keeps its score; re-running is cheap
2. **Daily consolidation** — once per calendar day, the snapshot updates and coaching regenerates
3. **Trajectory tracking** — your engagement and delegation slopes are recomputed against the full history each run

If you want true real-time scoring, the same orchestrator can be wrapped in a `watchdog` filesystem observer on the chat storage directories.

## Privacy

- All scanning happens locally. Nothing is uploaded except sampled transcripts you send to your authorized LLM judge.
- Profile data lives in `~/.praxis/profile.db` (SQLite). Delete it any time.

## What's in the box

```
praxis/
├── scanners/         # one per provider — claude, codex, copilot
├── scoring/
│   ├── rubric.py     # the seven dimensions, weights, citations
│   ├── heuristics.py # fast feature extraction
│   ├── judge.py      # LLM-as-judge (Claude + OpenAI)
│   ├── aggregate.py  # blend heuristic + judge, roll up across sessions
│   └── coach.py      # personalized coaching from your weakest dimensions
├── behavior/
│   ├── signals.py    # per-session engagement and atrophy patterns
│   └── trajectory.py # trajectory assessment (learning vs atrophying)
├── models_advisor/
│   ├── cards.py      # model card schema and built-in cards
│   └── advisor.py    # per-model usage analysis and advice
├── storage/          # SQLite persistence + daily consolidation
├── reports/          # terminal + HTML renderers
├── orchestrator.py   # the brain that ties it all together
└── cli/              # CLI entry point
```

## References

The scorecard's claims are grounded in:

- Shen & Tamkin (2026), *How AI Impacts Skill Formation*, arXiv 2601.20245
- Sarkar (2025), *AI Agents, Productivity, and Higher-Order Thinking*
- OpenRouter State of AI 2025 (100T tokens analyzed)
- Anthropic's 2026 qualitative study of 81,000 users
- Microsoft Copilot Usage Report 2025
- Anthropic, OpenAI, Google official prompting documentation

## License

Proprietary. Contact for licensing.
