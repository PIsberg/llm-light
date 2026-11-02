# llm_light — Benchmark Suite

Three standalone benchmarks ship with `llm_light.py`. Each tests a different
aspect of what the tool optimises and can be shared independently to demonstrate
its value.

## Published Headline Numbers

The current headline benchmark figures published in [`README.md`](../README.md)
come from `benchmark_claude_code.py`, which compares Claude Code against
`llm_light` on the same 4 Python repositories and 10-query workload.

Measured on 2026-03-23:

| Repository | Repo size | Claude Code | llm_light | Savings |
|---|---:|---:|---:|---:|
| `psf/requests` v2.31.0 | 108K tokens | $0.91 | $0.23 | **75%** |
| `pallets/flask` 3.0.0 | 144K tokens | $1.62 | $0.17 | **89%** |
| `encode/httpx` 0.25.2 | 191K tokens | $1.75 | $0.45 | **74%** |
| `bottlepy/bottle` 0.13.2 | 92K tokens | $1.52 | $0.30 | **80%** |
| **Total (≈40 queries)** | | **$5.81** | **$1.16** | **80% / 5× cheaper** |

`benchmark_cost.py` is still useful, but it answers a different question:
LLM Light vs a naive "send the whole repo" baseline. The README headline
stats should therefore be treated as the source of truth for tool-vs-tool cost
comparison, and this document now reflects that.

---

## Quick Reference

| Script | What it tests | API key needed | Est. cost |
|---|---|---|---|
| `benchmark.py` | Analytical token savings formula | No | Free |
| `benchmark_retrieval.py` | RAG retrieval quality (Hit@K, MRR) | No | Free* |
| `benchmark_models.py` | Embedding models, hybrid fusion, and per-effort budgets on this repo | No | Free (models download on first run) |
| `benchmark_router.py` | Effort tier and model the router picks for each question | No | Free (static model download on first run) |
| `benchmark_session_cache.py` | Session cost of four routing policies under a prompt-cache model | No | Free (static model download on first run) |
| `benchmark_cache_warming.py` | What the startup warm and the idle heartbeat spend, run through the real `heartbeat()` and `warm_cache()` | No | Free |
| `benchmark_answer_quality.py` | Answer quality of two variants on the same question, judged by Opus with swapped orders; Fable/Opus smoke | No (refuses with one); needs the Claude subscription | Subscription quota, about 150 CLI calls for `all` |
| `benchmark_cli_providers.py` | Tokens codex (or agy, `--provider gemini`) reports for one question through the shipped provider's `_run`, as shipped or `--baseline` | `codex login`, or a signed-in agy | Subscription or key usage, about 4k to 46k input tokens per run |
| `benchmark_live_api.py` | What only a real key can settle: the SDK on real responses (#71), effort messages and cache reads (#88), Fable and Opus requests (#92), whether `fetch_chunk` pays for itself (#85) | Yes, and `--i-accept-the-bill` | Stops at `--max-usd`, default $1.00 |
| `benchmark_cost.py` | Real-world cost savings vs naive baseline | Yes | ~$0.05–$0.50 |
| `benchmark_claude_code.py` | Claude Code vs `llm_light` vs naive baseline | Yes | Varies with live API/tool runs |

\* Offline fixture mode is fully local. Live SWE-bench mode downloads HuggingFace data + sentence-transformers on first run (~1.5 GB).

---

## 1 · `benchmark.py` — Analytical Token-Savings Benchmark

### Purpose

Answers: *"How much cheaper is llm_light than sending the whole codebase on
every query?"* using pure arithmetic — no API calls, no network, no GPU.

It replicates every formula from `llm_light.py` (token proxy, TOP_K,
retrieval k, cost calculation) on synthetic Java repositories generated
identically to `--test-mode`.

### Methodology

Three cost scenarios are compared for every (preset × query × effort) combination:

| Scenario | What it represents | Token price used |
|---|---|---|
| **Naive** | Sending the full repo on every single query | $3.00/M (full input) |
| **Cold** | First query of a session — cache write + retrieval | $3.75/M write |
| **Warm** | Follow-up queries — cache read + retrieval | $0.30/M read |

Formulas replicated from `llm_light.py`:

```
token_count  = len(text) // 4
TOP_K        = max(2, min(15, round(6000 / avg_chunk_tokens)))
retrieved_k  = max(2, round(TOP_K * budget / TARGET_RETRIEVED_TOKENS))
retrieved_k  = min(retrieved_k, total_chunks)
```

Pricing constants:  `PRICE_INPUT=3.00`, `PRICE_WRITE=3.75`, `PRICE_READ=0.30`,
`PRICE_OUTPUT=15.00` (per million tokens). These are a fixed reference rate (Sonnet 4.6)
defined in the benchmark scripts themselves, so results stay comparable with the committed
baselines. llm_light prices real calls per model (`MODEL_PRICES` in `config.py`).

A 10-turn session model `[low×3, medium×3, high×3, max×1]` is used to compute
realistic session costs (turn 1 = cold, turns 2–10 = warm).

### Presets

| Preset | Files | Methods/file | Approx. naive tokens |
|---|---|---|---|
| small | 5 | 10 | ~2.3K |
| medium | 50 | 15 | ~36K |
| large | 200 | 20 | ~193K |
| extra-large | 1000 | 20 | ~966K |

### Running

```bash
# All presets (default)
python tests/benchmarks/benchmark.py

# One preset
python tests/benchmarks/benchmark.py --preset large

# Machine-readable JSON
python tests/benchmarks/benchmark.py --json

# Fast mode (uses precomputed token counts — skips the iteration loop)
python tests/benchmarks/benchmark.py --fast
```

### Interpreting Results

The four output tables are:

1. **Preset overview** — files, chunks, avg chunk tokens, TOP_K, skeleton tokens,
   naive tokens for that preset.
2. **Per-query table** — one row per query × effort combination showing naive / cold /
   warm tokens and costs with percentage savings.
3. **Per-effort aggregate** — mean Cold save% and Warm save% per effort tier,
   averaged across all presets.
4. **10-turn session projection** — what a realistic coding session costs in each
   scenario and the Naive/Warm ratio.

Key insight from the extra-large preset:

```
Naive 10-turn session:  $27.61
Warm  10-turn session:  $0.08
Ratio:                  346x
```

### Assumptions / Limitations

- Synthetic data only — all files are identical Java templates. Real codebases
  have variable chunk sizes and more complex skeletons.
- Skeleton compression for synthetic data is exact (all files brace-group to one
  line). Real skeletons vary by directory depth.
- Does not model cache expiry (the heartbeat keeps the 1-hour skeleton entry
  alive; `benchmark_cache_warming.py` prices what that costs).

---

## 2 · `benchmark_retrieval.py` — RAG Retrieval Quality Benchmark

### Purpose

Answers: *"Does llm_light's RAG pipeline retrieve the right code?"* in two modes:

- Offline fixture mode for deterministic CI regression checks
- Live [SWE-bench Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite) mode for broader retrieval studies

Given a natural-language issue description, the benchmark checks whether the
correct source files appear in the top-K retrieved chunks — the same chunks
llm_light would send to Claude.

### Methodology

Offline fixture mode:
1. Load `tests/fixtures/retrieval_cases.json`.
2. Index the committed tiny repo in `tests/fixtures/retrieval_repo/`.
3. Chunk source files with the same tree-sitter pipeline used by `llm_light.py`.
4. Embed with the deterministic local `offline-token-overlap-v1` embedder.
5. Query with the issue text and rank chunks by cosine similarity.
6. Measure Hit@K, Recall@K, Precision@K, and MRR at K = [5, 10, 15].

Live SWE-bench mode:
1. Download SWE-bench Lite from HuggingFace (`princeton-nlp/SWE-bench_Lite`).
2. For each instance, clone the target repo at the base commit using a treeless
   shallow clone (`git clone --filter=blob:none --no-checkout`).
3. Parse the `patch` field to extract gold files (the files modified by the fix).
4. Chunk all source files with the same tree-sitter pipeline used by
   `llm_light.py` (method-level, with preamble injection).
5. Embed with the model(s) the `--retrieval` mode names (default `auto`: the
   model chosen by repo size, as before).
6. Query with the issue description text and rank chunks: by cosine similarity
   for `auto` and `dense`; for `static` and `hybrid`, through
   `llm_light.retrieval.hybrid_scores` (see below).
7. Measure Hit@K, Recall@K, Precision@K, and MRR at K = [5, 10, 15].

Retrieval modes (`--retrieval`, #69):

| Mode | Models | Ranking |
|---|---|---|
| `auto` (default, CI) | one, by repo size (MiniLM, mpnet, nomic) | cosine, unfiltered |
| `dense` | `--dense-model` (default `DENSE_EMBED_MODEL`, mpnet) | cosine, unfiltered |
| `static` | `FAST_EMBED_MODEL` | z-scores, `HYBRID_MIN_SCORE` floor, `HYBRID_SCORE_GAP` |
| `hybrid` | `FAST_EMBED_MODEL` + `--dense-model` | fused z-scores, floors and gap, as llm_light's hybrid index |

`static` and `hybrid` apply the shipped floors (a query neither model relates
to retrieves nothing, recorded as `retrieved_nothing`) and drop chunks more than
`HYBRID_SCORE_GAP` below the top fused score. They do not apply
`_hybrid_select_pairs`' token budget and one-chunk-per-file pass, which need the
chunk texts the embedding cache does not keep, so their curves are top-K like
the other modes. Each model's vectors are cached separately, so `hybrid` after
`dense` and `static` on the same repos embeds nothing new.

Results on the dev split (2026-11-02, CPU, 23 instances in 6 repos; the hybrid
run embedded every commit with both models in about 3 hours, then `dense` and
`static` reused the cache):

| Mode | Hit@5 | Hit@10 | Hit@15 | Recall@10 | MRR | Retrieved nothing |
|---|---|---|---|---|---|---|
| `dense` (all-mpnet-base-v2) | 6/23 | 9/23 | 9/23 | 39% | 0.228 | 0 |
| `static` | 8/23 | 8/23 | 9/23 | 35% | 0.151 | 0 |
| `hybrid` (shipped) | 7/23 | 8/23 | 9/23 | 35% | 0.202 | 0 |

Per repo, Hit@10 dense / static / hybrid: sqlfluff 3/2/2 of 5, marshmallow 1/1/2
of 2, pvlib 2/1/1 of 5, astroid 0/2/0 of 5, pyvista 1/1/1 of 1, pydicom 2/1/2 of
5. The three modes are within one instance of each other at every K; head to
head at Hit@10, hybrid alone found 2 and dense alone 3. That is noise at n = 23,
so on other codebases the hybrid index neither gains nor loses recall against
mpnet alone; its point stays the fast start (static vectors are ready before the
dense pass). The floors and gap left no instance with nothing retrieved, so
nothing here argues for re-tuning `HYBRID_MIN_SCORE` or `HYBRID_SCORE_GAP`, and
they stay (#69). The `--split test` run (300 instances) was not done; on this
machine it would take days of CPU.

### Metrics

| Metric | Definition |
|---|---|
| **Hit@K** | Fraction of instances where at least one gold file appears in top-K chunks |
| **Recall@K** | Fraction of all gold files retrieved in top-K (averaged per instance) |
| **Precision@K** | Fraction of top-K chunks that are gold files (averaged per instance) |
| **MRR** | Mean Reciprocal Rank — 1/rank of the first retrieved gold chunk |

Higher is always better. Hit@K and MRR are the primary signals.

### Coverage

The committed offline fixture covers 3 tiny Python retrieval cases: auth,
billing, and reporting. It is intentionally deterministic and currently has
non-zero retrieval signal for CI regression testing.

The live SWE-bench dataset spans repositories such as `django/django`,
`astropy/astropy`, `sympy/sympy`, `scikit-learn/scikit-learn`,
`matplotlib/matplotlib`, and `pytest-dev/pytest`.

### Running

```bash
# Offline committed fixture (used in CI)
python tests/benchmarks/benchmark_retrieval.py --fixture tests/fixtures/retrieval_cases.json

# Install dependencies for the larger live benchmark
pip install datasets sentence-transformers numpy

# Full dev split (23 instances — fast, no API key needed)
python tests/benchmarks/benchmark_retrieval.py --split dev

# Subset of the test split (300 instances total)
python tests/benchmarks/benchmark_retrieval.py --split test --n 50

# One specific repo only
python tests/benchmarks/benchmark_retrieval.py --split test --repo django/django

# Custom K values
python tests/benchmarks/benchmark_retrieval.py --k 5 10 20

# Machine-readable JSON
python tests/benchmarks/benchmark_retrieval.py --json

# Use pre-cloned repos (avoids re-cloning)
python tests/benchmarks/benchmark_retrieval.py --repos-dir /path/to/repos

# The hybrid index against its parts (#69): mpnet alone, static alone, fused
python tests/benchmarks/benchmark_retrieval.py --split dev --retrieval dense
python tests/benchmarks/benchmark_retrieval.py --split dev --retrieval static
python tests/benchmarks/benchmark_retrieval.py --split dev --retrieval hybrid
```

### Disk Space

| Item | Size |
|---|---|
| HuggingFace dataset | ~50 MB |
| Embedding model (MiniLM, < 50 files) | 22 MB |
| Embedding model (mpnet, 50-199 files) | 420 MB |
| Embedding model (nomic, 200+ files) | ~550 MB |
| Repo clones (treeless, all 6 repos) | ~300 MB |
| Embedding cache | ~50–200 MB |

### Interpreting Results

The output table shows per-repo and overall metrics at each K (the numbers below show the format only; the measured dev-split results are under Methodology):

```
Results: dev split  23 instances  K=[5,10,15]
+---------------------------+----------+----------+----------+----------+
| Repo                      | Hit@5    | Hit@10   | MRR      | Recall@10|
+---------------------------+----------+----------+----------+----------+
| django/django             | 0.714    | 0.857    | 0.542    | 0.612    |
| ...                       | ...      | ...      | ...      | ...      |
| OVERALL                   | 0.652    | 0.783    | 0.498    | 0.561    |
+---------------------------+----------+----------+----------+----------+
```

- **Hit@10 > 0.70** is a strong result for method-level chunking.
- **MRR > 0.50** means the correct file typically appears in the first two results.
- Low Recall@K with high Hit@K means multi-file bugs where only one file is
  retrieved — a known limitation of top-K retrieval.

### Assumptions / Limitations

- Gold files come from the patch diff — new files added by the fix are excluded
  (they don't exist at the base commit). Deleted files are also excluded.
- The issue text is used directly as the query; `llm_light.py` would let
  Claude rephrase or disambiguate first, which could improve recall.
- `MIN_SCORE` (0.35) cosine threshold is **not applied** in this benchmark so
  that recall at all K values is measurable. In production, low-scoring chunks
  are dropped, which improves precision at the cost of recall.
- Some SWE-bench instances touch only configuration files or test files that are
  outside the indexed extensions — these count as misses.
- It reimplements chunking. The default `auto` and `dense` modes rank by
  cosine similarity alone and do not import `llm_light`; `static` and `hybrid`
  use `llm_light.retrieval.hybrid_scores` and the configured floors and gap, but
  not the token budget (see Methodology). The test-file exclusion, the
  working-tree boost never run here, so `LLM_LIGHT_WORKTREE_BOOST` does not
  change its output. Measure those with `benchmark_models.py` (section 2b).

---

## 2b · `benchmark_models.py` — Embedding Models and Hybrid Fusion

### Purpose

Chooses the embedding models and the hybrid thresholds (#61). It answers one
question per configuration: for questions about this repository, does the file
that answers the question reach the context the model is given?

### Methodology

* `tests/fixtures/repo_questions.json` holds 41 questions: 23 that name code
  ("Where is exponential backoff for transient API errors implemented?"), 12
  paraphrases with no identifiers ("Where do we wait longer and longer between
  attempts after the server says it is overloaded?") and 6 off-topic ones
  ("what's the weather like in Paris today"), each with its answering file.
* The repository is chunked with llm_light's own chunker (tests included,
  since they compete in real use) and embedded with each model. Vectors are
  cached in `.llm_light_cache/bench/`, keyed on the chunk texts.
* For every question the production selection code runs at the medium
  budget: `_adaptive_select_pairs` for single dense models,
  `hybrid_scores` + `_hybrid_select_pairs` for the hybrid index.

### Running

```bash
python tests/benchmarks/benchmark_models.py          # all configurations
python tests/benchmarks/benchmark_models.py --sweep  # also sweep the gap and floors
python tests/benchmarks/benchmark_models.py --budgets  # recall and tokens sent per effort budget
python tests/benchmarks/benchmark_models.py --worktree   # working-tree boost, right file or unrelated file modified
python tests/benchmarks/benchmark_router.py            # effort and model per question
python tests/benchmarks/benchmark_router.py --margin 0.30  # replay with another escalation margin
```

### Results (2026-10-24, CPU)

| Configuration | Answering file sent (identifier / paraphrase) | Off-topic got nothing |
|---|---|---|
| all-mpnet-base-v2 alone, `MIN_SCORE` 0.35 (before #61) | 8/23, 7/12 | 6/6 |
| all-MiniLM-L6-v2 alone | 7/23, 4/12 | 6/6 |
| static-retrieval-mrl-en-v1 alone (while the dense pass runs) | 16/23, 3/12 | 6/6 |
| static + all-MiniLM-L6-v2 | 13/23, 7/12 | 6/6 |
| static + all-mpnet-base-v2 (default) | 14/23, 9/12 | 6/6 |

1,453 chunks from 68 files. Embedding about 1,400 chunks took 2 s with the
static model, 109 s with MiniLM and 383 s with mpnet. The sweep (`--sweep`) kept
the defaults: floors of 0.22 (static) and 0.21 (mpnet) are the lowest that keep
all six off-topic questions empty, and a gap of 1.5 sends a median of 3 files
(3.0 finds two more answers but sends 6). 35 questions on one repository:
differences of one or two questions are noise.

### Per-effort budget (2026-10-24, `--budgets`)

The configured hybrid (static + mpnet), with each effort's `_RETRIEVAL_BUDGET`.
Tokens are the estimated size of the selected chunk text, not the rendered
context, which adds file summaries on top.

| Effort (budget) | Identifier | Paraphrase | Off-topic got nothing | Tokens sent, median | Tokens sent, mean |
|---|---|---|---|---|---|
| low (1,500) | 13/23 | 8/12 | 6/6 | 1,141 | 971 |
| medium (3,000) | 14/23 | 9/12 | 6/6 | 1,901 | 1,689 |
| high (6,000) | 16/23 | 9/12 | 6/6 | 1,997 | 2,732 |
| max (9,000) | 16/23 | 9/12 | 6/6 | 1,997 | 3,443 |

Lowering a budget costs recall: low gives up one identifier and one paraphrase
answer against medium. Above high, the budget adds tokens without adding
answers on these questions, but none of the 41 questions routes to max, so
this result says nothing about the max tier's own questions.

Re-run 2026-10-30 on the current index (43 questions, 1,633 chunks, same
command):

| Effort (budget) | Identifier | Paraphrase | Off-topic got nothing | Tokens sent, median | Tokens sent, mean |
|---|---|---|---|---|---|
| low (1,500) | 13/25 | 10/12 | 6/6 | 1,121 | 996 |
| medium (3,000) | 20/25 | 11/12 | 6/6 | 1,933 | 1,811 |
| high (6,000) | 20/25 | 11/12 | 6/6 | 2,301 | 2,842 |
| max (9,000) | 22/25 | 11/12 | 6/6 | 2,301 | 3,667 |

Low still loses answers against medium. High no longer adds any over medium
and sends 57% more mean tokens; max now adds two identifier answers over high.
Both shifts rest on one or two questions. These token columns count the selected chunks' text; the context the high render sends differs from medium's by about 2%, and answer quality was equal, so the budgets stay (section 2f, #77).

### Router replay (2026-10-24, `benchmark_router.py`)

| Queries | Low | Medium | High | Max | Scores in the escalation band |
|---|---|---|---|---|---|
| 41 (repo questions) | 32 (78%) | 2 | 7 | 0 | 0 at margin 0.15, 0.30 and 0.50 |
| 43 (re-run 2026-10-30) | 33 (77%) | 2 | 8 | 0 | 0 at margin 0.15, 0.30 and 0.50 |

The Opus cost gate only changes a query's tier when its score is between 12.0
and 12.0 × (1 + margin). No query falls there, so the escalation margin has no
effect on this set. The replay measures routing only; it does not score answers.

### Test-file exclusion (2026-10-24, `--test-policy`, shipped)

The index includes the test suite, and the test and benchmark files reuse a
question's words (model, effort, budget). For a question about the package,
chunks under a test directory were crowding out the answer. Shipped rule: for a
question that does not mention tests, chunks under `tests/` (or any directory
named `test`, `__tests__`, `spec` or `specs`) get no fused score, so the
selector never picks them. A question that mentions tests keeps them all.

The fixture has 43 questions now (two were added on this date, both about
tests, and both answered in `tests/unit/`). The keep and exclude rows come from
the same run on the same 1,484 chunks:

| Effort (budget) | Policy | Identifier | Paraphrase | Off-topic got nothing | Tokens median / mean |
|---|---|---|---|---|---|
| low (1,500) | keep | 13/25 | 8/12 | 6/6 | 1,132 / 968 |
| low (1,500) | exclude | 15/25 | 10/12 | 6/6 | 984 / 907 |
| medium (3,000) | keep | 15/25 | 8/12 | 6/6 | 1,901 / 1,693 |
| medium (3,000) | exclude | 20/25 | 11/12 | 6/6 | 1,570 / 1,642 |
| high (6,000) | keep | 17/25 | 8/12 | 6/6 | 1,997 / 2,762 |
| high (6,000) | exclude | 21/25 | 11/12 | 6/6 | 1,570 / 2,429 |
| max (9,000) | keep | 17/25 | 9/12 | 6/6 | 1,997 / 3,469 |
| max (9,000) | exclude | 22/25 | 11/12 | 6/6 | 1,570 / 3,054 |

Recall rose at every budget, off-topic stayed at 6/6, and tokens fell or held.
Questions that mention tests are unchanged by the carve-out.

Two alternatives were measured on the 41-question set and not kept. A penalty
of 1.0 fused z-score units left median tokens at medium above keep (2,042 against
1,997). A penalty of 2.0 matched exclude on recall but needed a tuned constant,
so the rule with no constant was kept. The benchmark flags are `--test-policy`
(`keep`, `exclude` or `penalty`) and `--test-penalty`.

Limits: the rule was measured on this repository only. The directory names
come from common conventions, not from a survey of other projects.

### Call-graph expansion (2026-10-25, `--callgraph`, since removed)

Expansion adds up to 3 direct callers or callees of the best hit while the
budget has room (`_related_pairs`). Same question set, 1,579 chunks, static +
all-mpnet-base-v2, test-file exclusion on:

| Effort (budget) | Graph | Identifier | Paraphrase | Off-topic got nothing | Tokens median / mean |
|---|---|---|---|---|---|
| low (1,500) | off | 13/25 | 10/12 | 6/6 | 1,074 / 978 |
| low (1,500) | on | 13/25 | 10/12 | 6/6 | 1,074 / 978 |
| medium (3,000) | off | 19/25 | 11/12 | 6/6 | 1,933 / 1,802 |
| medium (3,000) | on | 19/25 | 11/12 | 6/6 | 2,833 / 2,169 |
| high (6,000) | off | 20/25 | 11/12 | 6/6 | 2,301 / 2,829 |
| high (6,000) | on | 20/25 | 11/12 | 6/6 | 3,838 / 3,503 |
| max (9,000) | off | 22/25 | 11/12 | 6/6 | 2,301 / 3,586 |
| max (9,000) | on | 22/25 | 11/12 | 6/6 | 3,949 / 4,603 |

No extra right-file hits at any budget, and 20-28% more mean tokens from
medium up (low skips expansion by design). So `CALL_GRAPH` defaulted to off. Limit: the metric is whether the
answering file is sent, not answer quality. A caller's body may help an answer
even when the file was already found, and nothing offline measures that. The
model can still pull callers and callees on demand with `fetch_chunk`. The
live answer-quality run in section 2f settled it (2 to 2), and the expansion
and its `--callgraph` option were removed (#86).

### Working-tree boost (2026-10-25, `--worktree`, shipped at 1.0)

`_boost_modified_chunks` adds `WORKTREE_BOOST` fused z-score units to chunks in
files git reports as modified. The benchmark replaces the git file list per
question: "right file" marks the answering file as modified (the case the
boost is for), "other file" marks one unrelated source file, rotating through
them (what the boost costs when you ask about something else). Medium budget,
same run as above:

| Boost | Right file modified: identifier / paraphrase | Tokens mean | Other file modified: identifier / paraphrase | Tokens mean |
|---|---|---|---|---|
| 0 | 19/25 / 11/12 | 1,802 | 19/25 / 11/12 | 1,802 |
| 0.5 | 22/25 / 11/12 | 1,767 | 19/25 / 11/12 | 1,816 |
| 0.75 | 23/25 / 11/12 | 1,696 | 19/25 / 11/12 | 1,820 |
| 1.0 | 25/25 / 11/12 | 1,658 | 19/25 / 11/12 | 1,829 |
| 1.5 | 25/25 / 11/12 | 1,443 | 19/25 / **9/12** | 1,869 |
| 2.0 | 25/25 / 12/12 | 1,282 | **18/25 / 9/12** | 1,934 |

Off-topic questions got nothing (6/6) in every row. 1.0 is the largest boost
with no loss when an unrelated file is modified, and it gains 6 identifier
answers when the answering file is. 1.5 loses two paraphrase answers. Limits:
one repository, one modified file per question, and the scenarios bound the
effect rather than sample real sessions, where you are sometimes asking about
the file you are editing and sometimes not.

### Embedding text experiment (2026-10-24, not shipped)

A miss on `route_query` showed the cause. Each chunk's embedded text starts with
the whole module preamble (imports, docstring), and all-mpnet-base-v2 reads only
its first ~384 tokens. For `llm.py::route_query` that window is almost all
imports, so the dense model never sees the function. Chunks from one file then
embed alike: four `benchmark_models.py` chunks scored exactly 0.299.

The experiment embedded `// <path>` plus the symbol alone, leaving the text sent
to Claude unchanged. Against the configured hybrid, using the same question set:

| Configuration | Identifier | Paraphrase | Off-topic got nothing | Median tokens, medium |
|---|---|---|---|---|
| Preamble in embedded text (shipped) | 14/23 | 9/12 | 6/6 | 1,901 |
| Symbol alone | 17/23 | 10/12 | **2/6** | 2,571 |

The dense model alone gained: all-mpnet-base-v2 went from 8/23 to 13/23 on the
identifier questions, with off-topic at 6/6. The fused result did not hold.
The `--sweep` of gap and floors could not restore 6/6 off-topic without losing
the gain, so the change was reverted. Re-tuning on these 41 questions would
have been fitting the answer key. The patch is kept out of the repo; the
mechanism is described here so the next attempt starts from the cause.

---

## 2c · `benchmark_session_cache.py` — Routing Policies and the Prompt Cache

### Purpose

Prices one session of the repo questions under four routing policies, to decide how effort and model changes should reach the API without throwing away the prompt cache (lever 1 in `TOKEN-SAVINGS.md`).

### Methodology

The questions in `tests/fixtures/repo_questions.json` are routed with the real `route_query()` and static embedder, then played as one session, in fixture order and in 20 shuffled orders. Each turn is priced with `MODEL_PRICES` and a cache model taken from the Claude API docs: the skeleton, tools and system prompt (measured from this repo at 4 characters per token) are cached per model for 1 hour; the history is cached per model for 5 minutes and stays readable across switches to other models; history compression rewrites it. Output tokens per effort (low 600, medium 1,500, high 3,000, max 6,000) and 400 answer tokens kept in history are assumptions, scaled with `--output-scale`.

| Policy | What it models |
|---|---|
| `old` | Per-turn model and top-level effort; an effort change on the same model invalidates the history cache |
| `effort` | Per-turn model, top-level effort fixed, routed effort in an effort message (`llm._apply_effort`) |
| `pinned` | `effort`, plus the session's model only moves up a tier |
| `first` | `effort`, plus the first question's model for the whole session (the default, `ROUTE_ONCE`). Its cost depends on the opening question, so each of the 43 questions opens one session and the result is split by the opener's model |

Whether an effort change also invalidates the cached skeleton is model-specific and not documented per model; `--effort-in-prefix` models that case.

### Running

```bash
python tests/benchmarks/benchmark_session_cache.py
python tests/benchmarks/benchmark_session_cache.py --gap 3 --output-scale 0.3 --effort-in-prefix
```

### Results (2026-10-25)

43 questions: 33 Haiku low, 8 Sonnet high, 2 Sonnet medium. Prefix 11,577 tokens. Mean of 20 shuffled sessions, change against `old`:

| Gap between turns | Output | Effort in prefix | `effort` | `pinned` |
|---|---|---|---|---|
| 2 min | x1.0 | no | -0.4% | +62.8% |
| 2 min | x1.0 | yes | -6.9% | +52.2% |
| 2 min | x0.3 | no | -0.6% | +63.9% |
| 2 min | x0.3 | yes | -10.1% | +48.3% |
| 1 min | x1.0 | no | -0.5% | +67.4% |
| 3 min | x1.0 | yes | -8.9% | +49.0% |

The fixed top-level effort is never more expensive, and saves most when an effort change would also have invalidated the skeleton. Pinning the model loses in every case: Haiku-routed questions run on Sonnet at 20 times the output price, to save cache writes that cost Haiku almost nothing. Limits: this is a cost model, not a live run, and it does not score answers; pinning would raise answer quality on the questions it moves to Sonnet.

`first` against `effort` with the same opener (prefix 11,786 tokens, measured after the docs above grew):

| Gap between turns | Output | Effort in prefix | Opens on Sonnet (10 sessions) | Opens on Haiku (33 sessions) |
|---|---|---|---|---|
| 2 min | x1.0 | no | +71.1% | -91.0% |
| 2 min | x1.0 | yes | +71.1% | -91.0% |
| 3 min | x0.3 | no | +75.0% | -90.9% |

Averaged over openers in the repo mix, `first` costs about 54% less than `effort`, but all of that comes from sessions that open on Haiku and then answer the 10 Sonnet-routed questions on Haiku as well. Whichever question happens to come first decides the session's model. It is the default by the developer's choice, for one predictable model per session; `LLM_LIGHT_ROUTE_ONCE=0` restores per-turn routing.

---

## 2d · `benchmark_cache_warming.py` — What Keeping the Cache Warm Costs

### Purpose

Prices the startup warm and the idle heartbeat, which `benchmark_session_cache.py` leaves out. A warm request is a cache read of the whole prefix, or a 1-hour write when the entry is missing, so a warm that no turn reads is pure cost.

### Methodology

Runs the real `warm_cache()` and `heartbeat()` against a fake API client and a fake clock, so the policy measured is whatever the code on the current commit does. The fake client keeps a 1-hour prompt cache per model for the prefix (skeleton, tools and system prompt, measured from this repo at 4 characters per token) and prices each request with `MODEL_PRICES`: a read when that model's entry is alive, a 1-hour write when it is not. Each scenario starts the session (the startup warm `full_refresh()` sends), asks one question at minute 1, stays idle for `--idle` hours with the heartbeat running, then asks a second question on the same model. A turn is priced as one prefix read or write on its model. History, retrieved chunks and output are the same under every warming policy and are left out.

### Running

```bash
python tests/benchmarks/benchmark_cache_warming.py
python tests/benchmarks/benchmark_cache_warming.py --idle 8
python tests/benchmarks/benchmark_cache_warming.py --idle 0 --changes 3
```

`--changes N` has the file watcher rebuild the skeleton N times between the two questions (a file added, deleted or renamed), each a new prefix in the fake cache.

### Results (2026-11-01)

Prefix 16,807 tokens. Prefix cost of the whole scenario (warms plus the two questions), before and after the change that warms once per TTL, only the model the turns read, and nothing before that model is known:

| Session | Idle | Warms before | Warms after | Before | After | Change |
|---|---|---|---|---|---|---|
| routed, opens on Haiku | 0 h | 1 | 0 | $0.0708 | $0.0035 | -95% |
| routed, opens on Sonnet | 0 h | 1 | 0 | $0.0706 | $0.0689 | -2% |
| routed, opens on Opus | 0 h | 1 | 0 | $0.2050 | $0.1378 | -33% |
| pinned Opus (`--model opus`) | 0 h | 1 | 1 | $0.2050 | $0.1412 | -31% |
| routed, opens on Haiku | 1 h | 112 | 1 | $0.0894 | $0.0037 | -96% |
| routed, opens on Sonnet | 1 h | 112 | 1 | $0.2571 | $0.0706 | -73% |
| routed, opens on Opus | 1 h | 112 | 1 | $0.5782 | $0.1412 | -76% |
| pinned Opus (`--model opus`) | 1 h | 112 | 2 | $0.5227 | $0.1445 | -72% |
| routed, opens on Sonnet | 8 h | 952 | 8 | $1.6689 | $0.0824 | -95% |
| routed, opens on Opus | 8 h | 952 | 8 | $3.4017 | $0.1647 | -95% |
| pinned Opus (`--model opus`) | 8 h | 952 | 9 | $1.9345 | $0.1681 | -91% |

Three causes, all in the old code. The heartbeat measured idle time from the last turn only, so once idle passed 240 s it warmed on every 30 s tick: 112 warms in an idle hour where one read inside the 1-hour TTL is enough (a read resets the timer). The startup warm wrote Sonnet's entry before any question was routed, a $0.067 write that no turn reads when the session opens on Haiku (33 of the 43 repo questions) or Opus. And `warm()` ignored the `--model` pin, so a pinned session kept Sonnet warm while its turns read Opus. No scenario costs more after the change. The one thing lost is latency: a session whose model is not pinned now pays the prefix write on its first question instead of in the background at startup. This is a cost model, not a live run.

Skeleton changes between the two questions, no idle time, before and after the watcher stopped warming (`refresh_skeleton_only()` and `refresh_md_file()` used to call `warm_cache()`), prefix 16,841 tokens:

| Session | Changes | Before | After | Change |
|---|---|---|---|---|
| routed, opens on Sonnet | 1 | $0.1364 | $0.1347 | -1% |
| routed, opens on Opus | 1 | $0.2728 | $0.2695 | -1% |
| routed, opens on Sonnet | 3 | $0.2711 | $0.1347 | -50% |
| routed, opens on Opus | 3 | $0.5423 | $0.2695 | -50% |
| pinned Opus | 3 | $0.5456 | $0.2728 | -50% |

A warm on a skeleton change is never cheaper than leaving the write to the next turn: warm then turn is one 1-hour write and a read, the turn alone is the write. Each further change before the turn was another full write, and a change that leaves the skeleton text as it was (an editor saving through a rename) was a read. The trade is the same as for the startup warm: the turn after a change waits for the prefill.

---

## 2e · `benchmark_cli_providers.py` — What Codex and agy Send Per Question

### Purpose

Codex has no prompt cache that llm_light controls, and the codex CLI adds its own prompt in front of llm_light's: agent instructions, a skills list, permissions text, collaboration-mode and multi-agent text, an environment block and the tool schemas of every enabled feature. This measures what one llm_light question costs in codex's own token counts.

### Methodology

Builds the stdin text `CodexProvider.complete()` sends (SYSTEM_PROMPT, two retrieved files, the question, flattened by `transcript()`), runs it through the shipped `CodexProvider._run`, and reads the `usage` of codex's `turn.completed` event. `run_cli` is wrapped only to read that line, so argv, working directory and the instructions file are what a session uses. `--baseline` swaps in the untrimmed argv (`codex._ARGV_TAIL`). `agent_messages` counts agent messages per run; more than one means codex took extra model rounds, each of which re-sends the whole prompt.

### Running

```bash
python tests/benchmarks/benchmark_cli_providers.py --runs 5 --baseline
python tests/benchmarks/benchmark_cli_providers.py --runs 5
python tests/benchmarks/benchmark_cli_providers.py --runs 5 --effort low   # codex's model_reasoning_effort
```

Live: it needs `codex login` and spends the quota codex is signed in with.

### Results (2026-11-01, codex-cli 0.166.0, Windows)

The same question ("find every way run_cli can leave an orphaned process or hang", runner.py and codex.py as context, 10,864 characters), 5 runs each:

| Argv | Input tokens, mean | Range | Output tokens, mean | Runs with extra rounds |
|---|---|---|---|---|
| baseline (untrimmed) | 21,766 | 15,341 to 46,295 | 926 | 1 of 5 |
| trimmed (shipped) | 9,190 | 9,190 to 9,190 | 621 | 0 of 5 |

-58% input and -33% output tokens. A single-round call drops from about 15,700 to 9,190 input tokens, which is the trim itself (about 6,500 tokens of codex's own prompt per round). The rest of the gain is the extra rounds: before the benchmark existed, the same question through the same argv took extra rounds on 4 of 8 runs (mean 26,850 input tokens), so 5 of 13 untrimmed runs took them against 0 of 13 trimmed runs. Codex does not report what the extra round was; the trimmed instructions say there are no tools, which is the same fix agy needed (`gemini._PREAMBLE`). A one-line prompt ("Reply with the single word: ok") drops from 13,197 to 6,654 input tokens.

What each switch removes, from `codex debug prompt-input` (characters, with the user's config): agent instructions 17,814 (replaced by `codex.INSTRUCTIONS`, 238), skills list 6,177, permissions text 9,061, collaboration mode 1,056, environment block 494; the feature switches removed 1,237 characters of tool schemas. Answer quality is not scored: one trimmed answer to the question above was read by hand (six concrete failure paths, each with a fix), and an edit request through the trimmed path still produced SEARCH/REPLACE blocks that `parse_edit_blocks()` applied. Codex reports `cached_input_tokens` on its own; llm_light's turn stats show the same counts for every codex turn (#148).


### Codex at the routed effort (#150, 2026-11-02, codex-cli 0.166.0, not shipped)

`--effort` runs codex with `-c model_reasoning_effort=<effort>`; without it codex reasons at its model default, `medium` for every model `codex debug models` lists. Each question at codex's default and at the effort llm_light routes it to, 5 runs each:

| Question (routed effort) | Effort | Input, mean | Output, mean | Reasoning, mean |
|---|---|---|---|---|
| "What does empty_temp_dir do?" (low) | default (medium) | 9,705 | 52 | 0 |
| same | low | 9,705 | 56 | 0 |
| run_cli failure paths, step by step (high) | default (medium) | 9,727 | 595 | 0 |
| same | high | 9,727 | 1,870 | 1,232 |

Routing the effort to codex is not shipped. On the low-routed question, where it was meant to save, codex already spends no reasoning tokens at its default, so there is nothing to save; on the high-routed question it triples the output. Whether the high-effort answers are better is not scored. The `--effort` option and `codex._argv_tail(..., effort)` stay so this can be re-measured on a later codex.

### agy (Gemini), 2026-11-02, agy 1.4.12, Windows

`--provider gemini` runs the same prompt through the shipped `GeminiProvider._run` and reads the `usage` on agy's `result` event (`input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens`). `--baseline` runs agy's default agent (no `llm-light` agent file, no `--agent`), which is what llm_light sent before #149. `tool_steps` counts finished tool calls, and `error` is 1 when the shipped path refused the run.

```bash
python tests/benchmarks/benchmark_cli_providers.py --provider gemini --runs 5 [--baseline]
python tests/benchmarks/benchmark_cli_providers.py --provider gemini --runs 5 --question "What does empty_temp_dir do?" [--baseline]
```

| Question | Agent | Input tokens, mean | Output tokens, mean | Thinking, mean | Tool steps |
|---|---|---|---|---|---|
| "What does empty_temp_dir do?" (routes low) | default (baseline) | 11,078 | 698 | 518 | 0 of 5 runs |
| same | `llm-light`, no tools (shipped) | 4,072 | 615 | 441 | 0 of 5 runs |
| run_cli failure paths, step by step (routes high) | default (baseline) | 11,094 | 12,537 | 9,699 | 0 of 5 runs |
| same | `llm-light`, no tools (shipped) | 4,600 | 16,239 | 12,542 | 0 of 5 runs |

The shipped agent sends about 6,500 fewer input tokens per question: agy's own agent prompt and tool schemas. One easy-question run read 2,542 tokens from agy's cache, which is why its mean is below 4,580. On the hard question the output rose 30%, all of it thinking; output varies a lot from run to run (8,932 to 16,929 on the baseline), so five runs put that rise near the edge of noise. #158 re-ran it with 10 runs per arm on two hard questions: output 16,705 against 14,031 (+19%, t = 1.7) on this one, and 10,882 against 9,846 (+11%, t = 0.8) on a max-routed refactor question ("Compare how the codex provider and run_cli handle a CLI that exits non-zero, and design a refactor that unifies their error handling."), with run-to-run standard deviations of 1,400 to 4,100. Neither difference is distinguishable from noise, while the input saving (11,092 to 4,600 and 11,098 to 4,602) is exact in every run and the total tokens per question fall by 3,819 and 5,460. No change to the agent. The change ships for #122: on a prompt that asks for a web search, the default agent ran `search_web` 3 times at 34,648 input tokens and the `llm-light` agent ran no tool in 5 of 5 runs at 1,249. Three edit requests through the shipped path each returned a SEARCH/REPLACE block that `parse_edit_blocks()` read.


---

## 2f · `benchmark_answer_quality.py` — Answer Quality, Live on the Subscription

### Purpose

The offline benchmarks score whether the right file reaches the context, not whether the answer is right. This one answers the same repo question with two variants and has a judge model pick the better answer, for the open answer-quality questions: Haiku against Sonnet under ROUTE_ONCE (#90), the medium against the high retrieval budget (#77), and call-graph expansion off against on (#86). A `tools` experiment answers Sonnet-routed questions with tools off and with the agent's read-only tools on, to see what the agent's file reads cost (#79), a `toolset` experiment compares the `--tools` list with every built-in tool (lever 7), and the `docs` experiments compare the skeleton Claude gets with every doc excerpt (lever 8, #142). A `toptier` mode sends two plan-mode turns to Fable 5.1 and Opus 5.5 (#92).

### Methodology

- Retrieval is llm_light's own: hybrid static + dense scores, test chunks excluded, `_hybrid_select_pairs`, rendered by `retrieval._finish` (which, at the time of the run, added callers and callees when `CALL_GRAPH` was on; the expansion was removed after it, #86). The working-tree boost is left out.
- The request is built as `chat()` builds a first turn, flattened by `transcript()`, and sent through `providers.claude._make_cli_subprocess_call` with **tools off**, so the answer rests on the context alone. The CLI path carries no effort, so every answer runs at the CLI's default effort.
- The judge (Opus 5.5, tools off) gets the question, a reference (the answering file's source, or the ground-truth callers for #86) and both answers, scores each 0 to 2 and names a winner. Each pair is judged twice with the answers swapped; a side wins only when both orders agree.
- It refuses to run when an API key is configured, so it never bills one.

### Running

```bash
python tests/benchmarks/benchmark_answer_quality.py --experiment all
python tests/benchmarks/benchmark_answer_quality.py --report
```

Live: it needs `claude auth login` (or `~/.llm_light_automation_token`) and spends subscription quota: about 310 CLI calls for `all`.

### Results (2026-11-02, claude 2.1.316, Windows)

| Experiment | Pairs | x wins | y wins | Ties | Score sum, x / y (of 2 per pair) | Mean context tokens, x / y | Mean output tokens, x / y |
|---|---|---|---|---|---|---|---|
| models: Haiku (x) / Sonnet (y), Sonnet-routed questions | 10 | 0 | 2 | 8 | 11 / 12 | 1,153 / 1,153 | 931 / 679 |
| budget: medium 3,000 (x) / high 6,000 (y), high-routed questions | 8 | 1 | 2 | 5 | 9 / 9 | 1,065 / 1,086 | 600 / 572 |
| callgraph: off (x) / on (y), `tests/fixtures/usage_questions.json` | 8 | 2 | 2 | 4 | 4 / 6 | 822 / 1,581 | 855 / 960 |

- **Models (#90).** Haiku never beat Sonnet and lost 2 of 10 (once scored 1 against 2, once preferred in both orders at equal scores). Under ROUTE_ONCE a session that opens on Haiku answers Sonnet-routed questions on Haiku; on this set that costs quality on about 2 in 10 of them, for the 91% saving `benchmark_session_cache.py` models.
- **Budget (#77).** The rendered context barely changes: 1,065 against 1,086 mean tokens, identical on 5 of 8 questions. At high effort only the top two chunks are shown in full and the rest as a bounded file summary, so a larger selection adds little to what is sent. The 57% gap in section 2b's budget table counts the selected chunks' text, not the rendered context. Lowering the high budget would save about 2% of the context with no measured change in quality, so the budgets stay.
- **Call graph (#86).** Expansion nearly doubles the context (822 to 1,581) and does not win: 2 against 2 in pairwise judgement. The rubric scores lean its way (6 against 4 of 16; it lifted two answers from 0 to 1), but neither answer side could see the real callers, so most scores are 0 or 1. By the rule #86 set, it is removed.
- **Top tier (#92).** Fable 5.1 and Opus 5.5 each answered two plan-mode turns through the real agent path (3,460 to 3,969 characters, about 30 s each), and the second call read the cache (Fable 120,948 cached tokens, Opus 121,727). The CLI builds its own request, so this says nothing about llm_light's API-mode effort message or breakpoints.
- **Tools (#79), 2026-11-02, a second run.** Same 10 Sonnet-routed questions, same retrieved context; x without tools, y with the CLI's read-only tools in plan mode, the repo as the working directory:

  | | Wins | Mean total input (input + cache read + cache write) | Mean output | Sonnet-priced cost per question |
  |---|---|---|---|---|
  | tools off | 2 | 29,218 | 578 | $0.0223 |
  | tools on | 6 (2 ties) | 91,322 | 982 | $0.0495 |

  The agent read files on 4 of 10 questions (3 to 4 model rounds; 135,504 to 193,180 total input tokens, nearly all cache reads); on the other 6 it answered in one round, and having tools available still added about 15,000 input tokens (the tool definitions). Cache writes on the reading questions exceed the non-reading ones by about 2,000 to 6,000 tokens: that is the file content read, about $0.005 to $0.015 per reading question at Sonnet prices. A cheaper-model read shunt (lever 5) could save at most part of that, on the questions where reading won on quality, so it is not built. The CLI path's tools are Claude Code's, not llm_light's API-mode `read_file`; API mode is unmeasured (it needs a key, see #85).
- **Toolset (lever 7), 2026-11-02, claude 2.1.316.** Same 10 Sonnet-routed questions through the real agent path, read-only in plan mode with the repo as the working directory; x sends llm_light's `--tools` list, y the same command with that flag removed, so the CLI offers every built-in tool. The list won 4, every tool won 1, 5 tied. Mean total input in that pass was 77,357 against 67,954, because x happened to read files on 7 questions and y on 5, and x won all three questions where only x read. A scratch rerun separated the two effects, 6 of those questions 3 times per arm with the order alternated and tool calls counted (36 runs, no judge; `--experiment toolset` runs the judged pass only):

  | | Runs that used a tool | Mean tool calls | Mean total input | Mean cache write | Mean output | Sonnet-priced cost per question |
  |---|---|---|---|---|---|---|
  | `--tools` list | 13 of 18 | 1.61 | 79,769 | 7,056 | 1,196 | $0.0369 |
  | every tool | 11 of 18 | 1.39 | 99,052 | 9,543 | 1,148 | $0.0443 |

  Four of the 6 questions read at the same rate in both arms. At equal tool calls the list is 25% to 28% lower (0 calls 33,293 against 44,535, 1 call 67,340 against 89,356, 2 calls 98,443 against 137,006), which is the schema saving repeated per round. The model's reads are noisy between runs, so one pass is too few to read a total from. Plan mode only: `default` and `bypassPermissions` got a live smoke test (a real edit, an allowlisted command, a bypass command), not this comparison.
- **Docs (lever 8, #142), 2026-11-02, claude 2.1.316.** x is the skeleton with only `CLAUDE.md`, `AGENTS.md` and the root `README` (3,564 tokens on this repo), y is every excerpt (15,952). Same retrieved context, both with the `--tools` list. `docs` and `docs-no-tools` use `tests/fixtures/doc_questions.json`, 10 questions only the docs can answer, with that doc as the judge's reference. These are the worst case for x. The runs came from a scratch prototype of these experiments, which also ran code questions with tools off.

  | Questions, tools | x wins | y wins | Ties | Mean total input, x / y | Sonnet-priced cost, x / y | Mean seconds, x / y |
  |---|---|---|---|---|---|---|
  | doc, on (`docs`) | 6 | 0 | 4 | 34,945 / 75,824 | $0.0378 / $0.0401 | 14.4 / 14.2 |
  | code, on (`docs-code`) | 1 | 1 | 7 (1 invalid) | 34,772 / 76,634 | $0.0310 / $0.0353 | 14.2 / 16.9 |
  | doc, off (`docs-no-tools`) | 0 | 7 | 3 | 7,841 / 27,756 | $0.0129 / $0.0203 | 9.3 / 9.2 |
  | code, off (prototype only, 9 of 10: the last question hung and was stopped) | 2 | 3 | 4 | 9,531 / 29,450 | $0.0154 / $0.0167 | 8.9 / 8.5 |

  With a tool to open files the excerpts cost tokens and bought nothing: most are compacted to headings, and without them the model opens the file rather than answer from an outline. With no such tool they are the only route to the docs, and leaving them out loses doc answers. So Claude gets x, and Gemini and Codex keep y. Three limits. x ran first on every `docs` question, which pays cold writes against it; the other runs alternate, and the benchmark now does too. All of this went through the subscription's CLI path, so API-key mode, whose `read_file` is a different tool, is only modelled: `benchmark_session_cache.py` gives $0.6262 to $0.5084 per session (-19%) in fixture order, from a prefix of 17,080 to 4,692 tokens. And it is one repository, whose docs are unusually many; a repo with few docs saves less.
- Small samples: a difference of one pair is noise. Where a pair had no tools, a variant that would have read a file to recover is scored without that chance.

---

## 2g · `benchmark_live_api.py`: What Only an API Key Can Settle

### Purpose

A subscription turn goes through the `claude` CLI, which builds its own request, so no subscription run reaches the request llm_light builds in API-key mode. This script holds the checks that are left. It has not been run against the real API: it was tested through the real SDK against a local stub (`tests/integration/test_benchmark_live_api.py`), because no key was available.

### Running

From the repo root, with a real key. It refuses to start without `--i-accept-the-bill`, in OAuth mode, or on a subscription token (`sk-ant-oat...`), and stops once it has spent `--max-usd` (default $1.00) at `MODEL_PRICES`.

```bash
export ANTHROPIC_API_KEY=sk-ant-api...
B="python tests/benchmarks/benchmark_live_api.py --i-accept-the-bill"
$B --experiment sdk                       # #71: one create, one stream, on Haiku
$B --experiment effort                    # #88: the effort-only message
$B --experiment effort --effort-in-prefix # #88: the shape before it, to compare
$B --experiment toptier                   # #92: two turns each on Fable 5.1 and Opus 5.5
$B --experiment fetch-chunk --limit 10    # #85: the same questions with and without the tool
```

It prints one table per experiment and writes the rows to `.llm_light_cache/bench/live_api.json`. It exits 1 if any turn failed or the cap stopped it, and each experiment stops at its first failed turn.

### Reading the result

- **`sdk`**: a row with tokens for both calls means the API accepted the request body on the installed SDK and the response parsed.
- **`effort`**: on turn 2, and again on turn 4, `Cache read` should stay near the previous Sonnet turn's whole prompt. Run `--effort-in-prefix` as well: if `Cache read` falls only to the skeleton there, a top-level change spares the skeleton on Sonnet 5.5, the 0.4% case in #88; if it falls to zero, the 7-10% case. Each run adds a unique line to the skeleton, so it starts cold and reads nothing the other run wrote.
- **`toptier`**: the follow-up turn should read what the tool turn wrote. `USD` is `usage` at `MODEL_PRICES`, to compare with the Console. A zero-data-retention org should see `Claude API error 400` in the `Error` column instead of a table of tokens.
- **`fetch-chunk`**: `fetch-chunk-summary` in the JSON has mean tokens and tool calls per warm question for each arm, leaving out each arm's first question, which writes the cache. Keep the tool only if tokens or tool calls fall.

### Limits

- The router is pinned to each experiment's model and effort; the rest of the turn is the real `chat()` and transport.
- `fetch-chunk` uses the static embedder (`dense="skip"`), so retrieval there is the fast path, not hybrid.

---

## 3 · `benchmark_cost.py` — Real-World Cost Benchmark

### Purpose

Answers: *"How much money does llm_light actually save on real code?"* by
running the tool against four popular Python open-source projects and comparing
the actual API spend to a naive baseline (sending the whole repo every query).

This is the benchmark most useful for sharing with other developers: it shows
real dollar amounts on real codebases.

### Methodology

1. Clone 4 Python repos at fixed stable tags (shallow, `--depth 1`).
2. Count total source tokens in each repo (same extensions and SKIP_DIRS as
   `llm_light.py`) to compute the naive baseline.
3. For each of 10 standard queries, run `llm_light.py` in one-shot mode:
   ```bash
   python llm_light.py "query"
   ```
   with `stdin=/dev/null` so it runs non-interactively.
4. Strip ANSI codes from stderr and extract the `[Stats]` and `[Cost]` lines.
5. Compute true savings vs the naive baseline:
   ```
   nocache_cost = actual_cost + saved_vs_nocache   (from [Cost] line)
   output_cost  = nocache_cost - (total_input / 1M) * 3.00
   naive_cost   = (naive_tokens / 1M) * 3.00 + output_cost
   true_savings = (naive_cost - actual_cost) / naive_cost
   ```

### Repos Benchmarked

| Repo | Tag | Description |
|---|---|---|
| `psf/requests` | v2.31.0 | HTTP library |
| `pallets/flask` | 3.0.0 | WSGI web framework |
| `encode/httpx` | 0.25.2 | Next-gen HTTP client |
| `bottlepy/bottle` | 0.13.2 | Fast micro web-framework |

These repos were chosen because they are:
- Pure Python (no generated code or large binary assets)
- Well-maintained with stable tags
- Representative of real-world project sizes (small to medium)

### Standard Queries

10 generic queries spanning all 4 effort tiers are run against each repo:

| # | Effort | Query |
|---|---|---|
| 1 | low | List all public classes or modules in this codebase |
| 2 | low | Where is the main entry point defined? |
| 3 | low | What Python version is required by this project? |
| 4 | medium | Explain how HTTP sessions or connections are managed |
| 5 | medium | Summarize the error handling and exception hierarchy |
| 6 | medium | Describe how routing or request dispatching works |
| 7 | high | Show me an example of adding a custom middleware or hook |
| 8 | high | Implement a helper function that retries a request on 5xx errors with exponential backoff |
| 9 | high | Identify potential thread-safety issues and suggest fixes |
| 10 | max | Evaluate the overall architecture trade-offs and scalability of this codebase |

### Running

```bash
# Install API key (real costs incurred)
export ANTHROPIC_API_KEY=sk-ant-...

# Full run (all 4 repos × 10 queries = 40 API calls)
python tests/benchmarks/benchmark_cost.py

# One repo only
python tests/benchmarks/benchmark_cost.py --repo pallets/flask

# Dry-run (clone repos, count tokens, no API calls)
python tests/benchmarks/benchmark_cost.py --dry-run

# Use already-cloned repos
python tests/benchmarks/benchmark_cost.py --repos-dir /path/to/repos

# JSON output
python tests/benchmarks/benchmark_cost.py --json
```

### Estimated Costs

A full run (40 API calls) costs approximately:
- **$0.05–$0.15** if cache hits land well (queries 2–10 reuse the warm cache).
- **$0.20–$0.50** worst case (cold cache on every query, max-effort responses).

The first query per repo is always a cache-write. Subsequent queries benefit from
warm cache reads at $0.30/M vs $3.75/M write.

### Interpreting Results

**Per-repo table:**

```
Results for psf/requests:
+--+-----------------------------------------------+------+--------+---------+------+---------+---------+----------+
|# | Query (truncated)                             |Effort|Model   |Input tok|Cache%|Actual $ |Naive $  |True save%|
+--+-----------------------------------------------+------+--------+---------+------+---------+---------+----------+
|1 |List all public classes or modules...          |low   |haiku   |   12,450|  0.0%|  $0.0005|  $0.0421| 98.8%    |
|2 |Where is the main entry point defined?         |low   |haiku   |   11,200| 84.3%|  $0.0001|  $0.0393| 99.7%    |
...
```

**Key columns:**
- **Cache%**: Fraction of input tokens served from cache on that query.
- **Actual $**: What was actually charged to your API key.
- **Naive $**: What it would cost to send the entire repo on this query.
- **True save%**: `(Naive - Actual) / Naive`. The headline number.

**Aggregate table:**

```
AGGREGATE RESULTS ACROSS ALL REPOS
+------------------------+------------------------------+-------+---------+---------+----------+
|Repo                    |Desc                          |Queries|Actual $ |Naive $  |True save%|
+------------------------+------------------------------+-------+---------+---------+----------+
|psf/requests            |HTTP library for Python       |10     |$0.0025  |$0.4210  |99.4%     |
|pallets/flask           |Lightweight WSGI web framework|10     |$0.0031  |$0.5820  |99.5%     |
...
|TOTAL                   |                              |40     |$0.0123  |$2.1380  |99.4%     |
+------------------------+------------------------------+-------+---------+---------+----------+
Naive/Actual ratio: 174x
```

### Assumptions / Limitations

- **Cache warm-up**: Query 1 per repo is always cold. In real usage, developers
  run many more than 10 queries per session, so the effective cache hit rate is
  higher.
- **Naive baseline**: Assumes the entire repo is re-sent on every query at full
  $3.00/M input price. This is the absolute worst case. Other tools may use
  smaller context windows but still be more expensive than llm_light's RAG.
- **Model routing**: llm_light's router picks model and effort automatically.
  The benchmark does not force a specific model — it measures the tool as-is.
- **tree-sitter**: If tree-sitter is installed, chunks are method-level. Without
  it, whole-file chunks are used. Whole-file mode increases retrieved tokens and
  slightly reduces true savings. Install tree-sitter for best results.
- **Output tokens**: Output tokens are included in `actual_cost` but not in
  `naive_cost` estimation for the input-only naive baseline. The formula adds
  them back correctly via `output_cost`.

---

## 4 · `benchmark_claude_code.py` — Claude Code vs LLM Light

### Purpose

Answers: *"How does `llm_light` compare to Claude Code on the same real
queries?"* by combining live Claude Code runs with the saved JSON output from
`benchmark_cost.py`.

This is the benchmark that feeds the README headline comparison table.

### What It Compares

For each repo/query pair it compares:

- **Claude Code**: `claude --print` in a fresh isolated session
- **llm_light**: JSON results captured from `benchmark_cost.py --json`
- **Naive baseline**: sending the entire repo every time

### Repo Set

The current published comparison uses:

| Repo | Tag | Description |
|---|---|---|
| `psf/requests` | v2.31.0 | HTTP library |
| `pallets/flask` | 3.0.0 | WSGI web framework |
| `encode/httpx` | 0.25.2 | Next-gen HTTP client |
| `bottlepy/bottle` | 0.13.2 | Fast micro web-framework |

### Running

```bash
# First capture llm_light's real-cost benchmark output
python tests/benchmarks/benchmark_cost.py --json > cl_results.json

# Then run the Claude Code comparison
python tests/benchmarks/benchmark_claude_code.py --llm-light-results cl_results.json

# One repo only
python tests/benchmarks/benchmark_claude_code.py --repo pallets/flask --llm-light-results cl_results.json
```

### Published Result Snapshot

The current README numbers are:

| Repository | Claude Code | llm_light | Savings |
|---|---:|---:|---:|
| `psf/requests` | $0.91 | $0.23 | **75%** |
| `pallets/flask` | $1.62 | $0.17 | **89%** |
| `encode/httpx` | $1.75 | $0.45 | **74%** |
| `bottlepy/bottle` | $1.52 | $0.30 | **80%** |
| **Total** | **$5.81** | **$1.16** | **80% / 5× cheaper** |

## Running All Four Benchmarks

```bash
# 1. Analytical (free, instant)
python tests/benchmarks/benchmark.py

# 2. Retrieval quality
python tests/benchmarks/benchmark_retrieval.py --fixture tests/fixtures/retrieval_cases.json
# or the larger live benchmark:
pip install datasets sentence-transformers numpy
python tests/benchmarks/benchmark_retrieval.py --split dev

# 3. Real-world cost (requires API key, incurs ~$0.10 in costs)
export ANTHROPIC_API_KEY=sk-ant-...
python tests/benchmarks/benchmark_cost.py --dry-run  # preview without API calls
python tests/benchmarks/benchmark_cost.py            # real run

# 4. Claude Code comparison (requires Claude Code plus benchmark_cost JSON)
python tests/benchmarks/benchmark_cost.py --json > cl_results.json
python tests/benchmarks/benchmark_claude_code.py --llm-light-results cl_results.json
```

## Sharing Results

To share benchmark results with other llm_light users:

```bash
# Capture all three
python tests/benchmarks/benchmark.py --json       > results_analytical.json
python tests/benchmarks/benchmark_retrieval.py \
    --fixture tests/fixtures/retrieval_cases.json \
    --json                       > results_retrieval.json
python tests/benchmarks/benchmark_cost.py \
    --json 2>/dev/null           > results_cost.json
```

The JSON outputs are self-contained and include all metadata needed to reproduce
or compare results across runs.

## CI Regression Testing

`llm_light` includes an automated GitHub Actions pipeline that ensures token costs do not spike and retrieval accuracy does not degrade over time!
It implements a hard block on any pull request that reduces `Hit@10` / `Recall@10` by >2% or increases analytical token costs by >1%.

If a developer legitimately changes the chunking behavior for the better, they simply run the benchmarks locally, overwrite the `baseline_*.json` files, and commit them.

**To manually update the baselines:**
```bash
# 1. Update analytical token baseline
python tests/benchmark.py --json > tests/baseline_token_stats.json

# 2. Update retrieval performance baseline (matches the CI offline fixture)
python tests/benchmark_retrieval.py --fixture tests/fixtures/retrieval_cases.json --output tests/baseline_retrieval_stats.json

# 3. Commit the new baseline files
git add tests/baseline_token_stats.json tests/baseline_retrieval_stats.json
git commit -m "chore: Update benchmark baselines"
```
