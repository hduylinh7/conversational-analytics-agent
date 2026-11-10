# Evaluation & Benchmark Suite

Layered, golden-data-driven benchmarks for the conversational analytics agent.
Scoring is **deterministic** (exact / tolerance-based comparison against
materialized golden result sets and guardrail verdicts) — no LLM-as-a-judge,
no subjective scoring. The LLM is only evaluated through what its generated
SQL actually executes to.

---

## Layers

Each layer mirrors one stage of the LangGraph pipeline and reuses the real
production components:

| Layer | Component under test | LLM | DB | CI-safe |
|---|---|---|---|---|
| `retrieval` | `SemanticRetriever` selection/refusal fidelity | No | No | Yes |
| `guardrail` | `SQLGuardrailValidator` verdicts (DML/DDL/allowlist/dangerous functions) | No | No | Yes |
| `generation` | Text-to-SQL: validity + **Execution Accuracy (EX)** vs golden results | Yes | Yes | No (provider + DB) |
| `e2e` | Full graph: success / refusal / failure stage + EX when golden materialized | Yes | Yes | No |

### Scored metrics

- **Retrieval**: selection recall/precision per question, refusal accuracy for
  non-analytics questions.
- **Guardrail**: verdict accuracy, plus a hard split between
  `unsafe_allows` (expected deny, observed allow — security regressions) and
  `false_denies`.
- **Generation**:
  - `execution_accuracy` (EX): fraction of `result_match` cases whose
    generated SQL reproduces the golden result multiset (order-insensitive,
    column-name-insensitive, numeric-tolerance aware).
  - `valid_rate`: guardrail-valid SQL after the full repair loop.
  - `avg_repairs` / `repaired_saved`: repair-loop telemetry.
  - `generation_latency_ms` / `execution_latency_ms`: p50/p95.
- **E2E**: outcome accuracy (success exactly when expected, refusal exactly
  when expected), wall-clock latency, EX when a golden result exists.

File-based core logic lives in `backend/app/eval/` (schemas, loader,
`compare.py`, runners, reporting) and is covered by
`tests/eval/test_eval.py`.

---

## Directory layout

```text
eval/
├── cases/                     # Per-layer YAML benchmark datasets
│   ├── retrieval.yaml
│   ├── guardrail.yaml
│   ├── generation.yaml
│   └── e2e.yaml
├── golden_results/            # Materialized golden result sets (generated)
├── run_eval.py                # CLI entrypoint for all layers
└── README.md
```

---

## Running

```bash
# Deterministic layers (no API key, no DB) — CI-safe:
python eval/run_eval.py --layers retrieval guardrail

# Text-to-SQL benchmark (needs a configured LLM_PROVIDER + ingested Olist data
# + materialized golden results):
python eval/run_eval.py --layers generation --suite smoke

# Full pipeline benchmark with a specific model:
python eval/run_eval.py --layers e2e --model-override google/gemini-2.5-flash

# Everything:
python eval/run_eval.py --layers retrieval guardrail generation e2e --markdown
```

Artifacts are written to `reports/eval/<layer>_eval.json` (machine-readable,
full per-case diagnostics) and `<layer>_eval.md` (summary table + failures).
Use `--suite <tag>` to run a subset (e.g. `smoke`), `--output` to redirect.

---

## Golden results workflow

Golden SQL results are materialized **once** against PostgreSQL and stored in
`eval/golden_results/<case_id>.json`; evaluation runs compare agent output
against these stored sets only.

```bash
# Materialize missing golden results (idempotent):
python scripts/materialize_golden_results.py --layers generation e2e

# Overwrite + prove the golden SQL is deterministic (re-runs and compares):
python scripts/materialize_golden_results.py --force --validate-ex
```

Golden SQL must follow the semantic layer conventions (see the header of
`eval/cases/generation.yaml`): `COUNT(DISTINCT ...)`, `customer_unique_id`,
`DATE_TRUNC('month', ...)`, and never a direct
`payments x order_items` fan-out join.

### Expectation modes (generation / e2e)

- `result_match` — execution with EX scoring against the golden result.
- `valid_sql` — a guardrail-valid SQL candidate must be produced (no golden
  required; used while seeding).
- `skip` — retrieval must refuse the question; measured without any LLM call.

---

## Case format

`eval/cases/<layer>.yaml` — one top-level `cases` list; see the shipped files
for full examples. Every case has `id`, `tags` (complexity/category/suite),
and:

- retrieval: `questions` (each variant scored separately) + `retrieval`
  expectation (`metrics`, `dimensions`, `skip: true`);
- guardrail: `sql`, `verdict` (`allow|deny`), optional `expected_rules` rule
  codes from `ViolationRule`;
- generation/e2e: `questions`, exactly one expectation flag among
  `result_match` / `valid_sql` / `skip`, optional `golden_sql`.

Bilingual questions (Vietnamese + English) are scored as separate records so
both languages are always exercised.

---

## Regression testing in CI

`tests/eval/test_eval.py` runs the deterministic layers (case loading,
comparison semantics, guardrail verdicts, retrieval scoring, refusal path)
against the real components with no network access:

```bash
python -m pytest tests/eval -q
```

Any guardrail regression — especially `unsafe_allows` — fails the build.
