"""Pydantic schemas for evaluation cases, per-layer results, and expectations.

A benchmark is split into per-layer YAML files under ``eval/cases/``:

- ``retrieval.yaml``  -> :class:`RetrievalCase`
- ``guardrail.yaml``  -> :class:`GuardrailCase`
- ``generation.yaml`` -> :class:`GenerationCase`
- ``e2e.yaml``        -> :class:`E2ECase`

Every layer result model carries ``passed`` plus telemetry so the aggregate
report can compute accuracy, execution accuracy, latency, and repair stats.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EvalLayer(StrEnum):
    """Benchmark layer, mirroring one stage of the analytics pipeline."""

    RETRIEVAL = "retrieval"
    GUARDRAIL = "guardrail"
    GENERATION = "generation"
    END_TO_END = "e2e"


class ComparisonMode(StrEnum):
    """How the observed result matched the golden result."""

    EXACT = "EXACT"
    TOLERANT = "TOLERANT"


class RetentionMode(StrEnum):
    """What to do with non-matching rows in multiset comparison reports."""

    FAIL = "FAIL"


class RetrievalExpectation(BaseModel):
    """Expected semantic retrieval outcome for a question.

    ``skip=False`` asserts that every metric/dimension listed below was
    selected (extra selections are tolerated and reported as precision loss).
    ``skip=True`` asserts a refusal: nothing at all was selected.
    """

    model_config = ConfigDict(extra="forbid")

    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    skip: bool = Field(default=False)


class GuardrailCase(BaseModel):
    """A single SQL guardrail evaluation input.

    ``verdict`` is the expected security verdict; ``expected_rules``
    (optional) must be a subset of the observed violation rules for
    ``deny`` cases.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tags: list[str] = Field(default_factory=list)
    sql: str
    verdict: str  # "allow" | "deny"
    expected_rules: list[str] = Field(default_factory=list)


class GenerationCase(BaseModel):
    """A Text-to-SQL generation evaluation case.

    Expectations (``result_match`` and ``valid_sql`` are mutually exclusive):
    - ``result_match``: generated SQL must execute and reproduce the golden
      result set materialized in ``eval/golden_results/<id>.json``.
    - ``valid_sql``: a guardrail-safe SQL candidate must be produced (used
      before golden results are materialized).
    - ``skip``: the question must be refused without any LLM call.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tags: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    golden_sql: str | None = Field(default=None)
    result_match: bool = Field(default=False)
    valid_sql: bool = Field(default=False)
    skip: bool = Field(default=False)


class E2ECase(GenerationCase):
    """An end-to-end graph evaluation case.

    Extends the generation case with full-pipeline expectations:
    - ``success`` (mapped to ``result_match``): the pipeline returns executed
      rows matching the golden result when one is materialized.
    - ``skip``: the graph terminates with ``failed_stage="retrieval"``.
    - ``valid_sql``: degrades to "pipeline succeeded and returned rows".
    """

    model_config = ConfigDict(extra="forbid")


class RetrievalCase(BaseModel):
    """A semantic retrieval evaluation case with one or more question variants."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tags: list[str] = Field(default_factory=list)
    questions: list[str]
    retrieval: RetrievalExpectation


class GoldenResult(BaseModel):
    """Materialized reference result set for a generation/e2e case."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    case_id: str = Field(..., alias="caseId")
    golden_sql: str = Field(..., alias="goldenSql")
    columns: list[str] = Field(default_factory=list)
    rows: list[list[object]] = Field(default_factory=list)
    row_count: int = Field(default=0, alias="rowCount")
    truncated: bool = Field(default=False)
    materialized_at: str = Field(..., alias="materializedAt")


class CompareOutcome(BaseModel):
    """Verdict and diagnostics of one result-set comparison."""

    passed: bool
    mode: ComparisonMode | None = None
    expected_rows: int = 0
    actual_rows: int = 0
    arity_mismatch: bool = False
    mismatched_examples: list[str] = Field(default_factory=list)
    detail: str | None = None


class RetrievalCaseResult(BaseModel):
    """Evaluation outcome for a single (case, question) retrieval record."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    question: str
    passed: bool
    mode: str  # "expected_selection" | "expected_skip"
    recall: float = Field(default=0.0, ge=0.0, le=1.0)
    precision: float | None = None
    selected_metrics: list[str] = Field(default_factory=list)
    selected_dimensions: list[str] = Field(default_factory=list)
    expected_metrics: list[str] = Field(default_factory=list)
    expected_dimensions: list[str] = Field(default_factory=list)
    detail: str | None = None


class GuardrailCaseResult(BaseModel):
    """Evaluation outcome for a single guardrail case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    expected_verdict: str
    observed_verdict: str
    observed_rules: list[str] = Field(default_factory=list)
    missing_rules: list[str] = Field(default_factory=list)
    detail: str | None = None


class GenerationCaseResult(BaseModel):
    """Evaluation outcome for a single (case, question) generation record."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    question: str
    passed: bool
    expectation: str  # "result_match" | "valid_sql" | "skip"
    skipped_by_retrieval: bool = False
    is_valid_sql: bool = False
    repair_attempts: int = 0
    comparison: CompareOutcome | None = None
    execution_status: str | None = None
    final_sql: str | None = None
    golden_missing: bool = False
    golden_sql: str | None = None
    generation_ms: float = 0.0
    execution_ms: float = 0.0
    detail: str | None = None


class E2ECaseResult(BaseModel):
    """Evaluation outcome for a single (case, question) end-to-end record."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    question: str
    passed: bool
    expectation: str
    success: bool = False
    failed_stage: str | None = None
    repair_attempts: int = 0
    comparison: CompareOutcome | None = None
    row_count: int = 0
    wall_ms: float = 0.0
    detail: str | None = None
