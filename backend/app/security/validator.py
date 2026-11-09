"""SQL AST Guardrail Validator enforcing read-only policies, table allowlisting, and limits."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.security.intent_checks import check_business_intent
from app.security.schemas import (
    GuardrailConfig,
    SecurityViolation,
    ValidationResult,
    ViolationRule,
    ViolationSeverity,
)
from app.semantic.loader import get_semantic_catalog
from app.semantic.models import SemanticCatalog
from app.semantic.retrieval.schemas import RetrievalResult

logger = get_logger(__name__)

# Mutating or administrative SQL AST node types that are strictly forbidden
DISALLOWED_MUTATING_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
    exp.Transaction,
    exp.Merge,
    exp.Into,
    exp.Pragma,
    exp.Kill,
)


class SQLGuardrailValidator:
    """Production AST validator for Text-to-SQL analytics engines."""

    def __init__(
        self,
        config: GuardrailConfig | None = None,
        catalog: SemanticCatalog | None = None,
        strict_columns: bool = False,
    ) -> None:
        """Initialize SQL guardrail validator with configuration and catalog allowlists.

        Args:
            config: Runtime GuardrailConfig settings. If None, derived from application Settings.
            catalog: SemanticCatalog containing approved tables and columns. If None, loaded via get_semantic_catalog().
            strict_columns: If True, reject columns not explicitly declared in the catalog.
        """
        settings = get_settings()

        self.config = config or GuardrailConfig(
            max_limit=settings.SQL_MAX_LIMIT,
            default_limit=settings.SQL_DEFAULT_LIMIT,
            enforce_limit=settings.SQL_ENFORCE_LIMIT,
            enforce_intent_checks=settings.SQL_ENFORCE_INTENT_CHECKS,
            intent_strict_mode=settings.SQL_INTENT_STRICT_MODE,
        )
        self.strict_columns = strict_columns

        # Load allowed tables and columns from catalog if not explicitly configured
        self.allowed_tables: set[str] | None = None
        self.allowed_columns: set[str] | None = None

        if self.config.allowed_tables is not None:
            self.allowed_tables = {t.lower() for t in self.config.allowed_tables}
        else:
            cat = catalog or get_semantic_catalog(settings.SEMANTIC_LAYER_DIR)
            if cat and cat.data_sources:
                self.allowed_tables = set()
                self.allowed_columns = set()
                for table in cat.data_sources.tables:
                    tbl_name = table.name.lower()
                    self.allowed_tables.add(tbl_name)
                    # Also permit unqualified table name if in default schema
                    if "." in tbl_name:
                        self.allowed_tables.add(tbl_name.split(".")[-1])
                    for col in table.columns:
                        self.allowed_columns.add(col.name.lower())

        if self.config.allowed_columns is not None:
            self.allowed_columns = {c.lower() for c in self.config.allowed_columns}

    def validate(
        self,
        sql: str,
        *,
        retrieval: RetrievalResult | None = None,
    ) -> ValidationResult:
        """Inspect and sanitize input SQL query against AST security and intent policies.

        Args:
            sql: Raw SQL query string to validate.
            retrieval: Optional RetrievalResult resolved for the user's question.
                Providing it enables business intent conformance checks (SQL vs.
                the metrics, mandatory filters, and join tables that retrieval
                resolved). Omit to run security-only validation.

        Returns:
            ValidationResult with validation flag, sanitized SQL, violations, and metadata.
        """
        if not sql or not sql.strip():
            return ValidationResult(
                is_valid=False,
                original_sql=sql or "",
                sanitized_sql=None,
                violations=[
                    SecurityViolation(
                        rule=ViolationRule.SYNTAX_ERROR,
                        message="SQL query is empty or contains only whitespace.",
                        severity=ViolationSeverity.HIGH,
                    )
                ],
            )

        raw_sql = sql.strip()
        violations: list[SecurityViolation] = []
        warnings: list[str] = []

        # 1. Parse SQL and check for multiple statements
        try:
            statements = sqlglot.parse(raw_sql, read=self.config.dialect)
        except ParseError as exc:
            return ValidationResult(
                is_valid=False,
                original_sql=raw_sql,
                sanitized_sql=None,
                violations=[
                    SecurityViolation(
                        rule=ViolationRule.SYNTAX_ERROR,
                        message=f"SQL syntax error: {str(exc)}",
                        severity=ViolationSeverity.CRITICAL,
                    )
                ],
            )

        # Filter out comments and standalone semicolon nodes
        meaningful_stmts = [
            s for s in statements if s is not None and not isinstance(s, exp.Semicolon)
        ]

        if not meaningful_stmts:
            return ValidationResult(
                is_valid=False,
                original_sql=raw_sql,
                sanitized_sql=None,
                violations=[
                    SecurityViolation(
                        rule=ViolationRule.SYNTAX_ERROR,
                        message="Query contains only comments or empty semicolons.",
                        severity=ViolationSeverity.HIGH,
                    )
                ],
            )

        if len(meaningful_stmts) > 1:
            violations.append(
                SecurityViolation(
                    rule=ViolationRule.MULTIPLE_STATEMENTS,
                    message=f"Multiple SQL statements detected ({len(meaningful_stmts)} statements). Only a single analytical query is permitted.",
                    severity=ViolationSeverity.CRITICAL,
                    details={"statement_count": len(meaningful_stmts)},
                )
            )
            return ValidationResult(
                is_valid=False,
                original_sql=raw_sql,
                sanitized_sql=None,
                violations=violations,
            )

        ast = meaningful_stmts[0]

        # 2. SELECT-Only & Read-Only Root Type Verification
        if not isinstance(ast, exp.Select | exp.Union):
            violations.append(
                SecurityViolation(
                    rule=ViolationRule.SELECT_ONLY,
                    message=f"Disallowed statement type '{type(ast).__name__}'. Only read-only SELECT or UNION queries are permitted.",
                    severity=ViolationSeverity.CRITICAL,
                    details={"statement_type": type(ast).__name__},
                )
            )

        # 3. Deep AST Traversal: Block mutating nodes anywhere in query tree
        for node in ast.walk():
            if isinstance(node, DISALLOWED_MUTATING_NODES):
                violations.append(
                    SecurityViolation(
                        rule=ViolationRule.SELECT_ONLY,
                        message=f"Forbidden mutating operation '{type(node).__name__}' detected within query AST.",
                        severity=ViolationSeverity.CRITICAL,
                        details={"node_type": type(node).__name__},
                    )
                )
                break

        # Check for SELECT ... INTO construct
        if isinstance(ast, exp.Select) and ast.args.get("into") is not None:
            violations.append(
                SecurityViolation(
                    rule=ViolationRule.DISALLOWED_OPERATION,
                    message="SELECT INTO operations are prohibited as they alter database state.",
                    severity=ViolationSeverity.CRITICAL,
                )
            )

        # 4. Dangerous Function Detection
        for func_node in ast.find_all(exp.Func, exp.Anonymous):
            f_name = (func_node.name or func_node.key or "").lower()
            t_name = type(func_node).__name__.lower()

            if f_name in self.config.disallowed_functions or t_name in self.config.disallowed_functions:
                violations.append(
                    SecurityViolation(
                        rule=ViolationRule.DANGEROUS_FUNCTION,
                        message=f"Execution of prohibited function '{f_name or t_name}' is forbidden.",
                        severity=ViolationSeverity.CRITICAL,
                        details={"function_name": f_name or t_name},
                    )
                )

        # 5. CTE (Common Table Expression) Extraction
        cte_nodes = list(ast.find_all(exp.CTE))
        has_cte = len(cte_nodes) > 0

        if has_cte and not self.config.allow_ctes:
            violations.append(
                SecurityViolation(
                    rule=ViolationRule.DISALLOWED_OPERATION,
                    message="Common Table Expressions (WITH clauses) are disabled by security policy.",
                    severity=ViolationSeverity.HIGH,
                )
            )

        cte_names: set[str] = {c.alias.lower() for c in cte_nodes if c.alias}
        cte_columns: set[str] = {
            alias_node.alias.lower()
            for c in cte_nodes
            for alias_node in c.find_all(exp.Alias)
            if alias_node.alias
        }

        # 6. Table Allowlisting & Schema Checking
        tables_referenced: set[str] = set()
        for tbl_node in ast.find_all(exp.Table):
            t_name = (tbl_node.name or "").lower()
            s_name = (tbl_node.db or "").lower()

            # Skip virtual CTE names
            if t_name in cte_names:
                continue

            # Block system schemas immediately
            if s_name in self.config.disallowed_schemas:
                violations.append(
                    SecurityViolation(
                        rule=ViolationRule.DISALLOWED_TABLE,
                        message=f"Access to system schema '{s_name}' is forbidden.",
                        severity=ViolationSeverity.CRITICAL,
                        details={"schema": s_name, "table": t_name},
                    )
                )
                continue

            fqn = f"{s_name}.{t_name}" if s_name else f"{self.config.default_schema}.{t_name}"
            tables_referenced.add(fqn)

            # Check table allowlist if active
            if self.allowed_tables is not None:
                if fqn not in self.allowed_tables and t_name not in self.allowed_tables:
                    violations.append(
                        SecurityViolation(
                            rule=ViolationRule.DISALLOWED_TABLE,
                            message=f"Table '{fqn}' is not in the approved analytical catalog allowlist.",
                            severity=ViolationSeverity.HIGH,
                            details={"table": fqn},
                        )
                    )

        # 7. Column Extraction & Optional Allowlisting
        columns_referenced: set[str] = set()
        for col_node in ast.find_all(exp.Column):
            c_name = (col_node.name or "").lower()
            if c_name and c_name != "*":
                columns_referenced.add(c_name)
                if self.strict_columns and self.allowed_columns is not None:
                    if c_name not in self.allowed_columns and c_name not in cte_columns:
                        violations.append(
                            SecurityViolation(
                                rule=ViolationRule.DISALLOWED_COLUMN,
                                message=f"Column '{c_name}' is not in the approved column allowlist.",
                                severity=ViolationSeverity.MEDIUM,
                                details={"column": c_name},
                            )
                        )

        # 8. Business intent conformance (candidate SQL vs. retrieval contract)
        if retrieval is not None and self.config.enforce_intent_checks:
            for violation in check_business_intent(
                ast,
                retrieval,
                tables_referenced=tables_referenced,
                default_schema=self.config.default_schema,
            ):
                if self.config.intent_strict_mode or violation.severity == ViolationSeverity.HIGH:
                    violations.append(violation)
                else:
                    # Ambiguous failures degrade to non-blocking warnings.
                    warnings.append(f"[{violation.rule}] {violation.message}")

        # 9. LIMIT Enforcement & AST Manipulation
        original_limit: int | None = None
        applied_limit: int | None = None

        if self.config.enforce_limit:
            limit_node = ast.args.get("limit")

            if limit_node is not None and hasattr(limit_node, "expression"):
                try:
                    limit_expr = limit_node.expression
                    if isinstance(limit_expr, exp.Neg):
                        violations.append(
                            SecurityViolation(
                                rule=ViolationRule.LIMIT_EXCEEDED,
                                message="LIMIT clause cannot be negative.",
                                severity=ViolationSeverity.HIGH,
                            )
                        )
                    elif isinstance(limit_expr, exp.Literal) and limit_expr.is_int:
                        parsed_val = int(limit_expr.this)
                        original_limit = parsed_val

                        if parsed_val < 0:
                            violations.append(
                                SecurityViolation(
                                    rule=ViolationRule.LIMIT_EXCEEDED,
                                    message=f"LIMIT clause cannot be negative (got {parsed_val}).",
                                    severity=ViolationSeverity.HIGH,
                                )
                            )
                        elif parsed_val > self.config.max_limit:
                            # Clamp limit to maximum allowed
                            ast.set(
                                "limit",
                                exp.Limit(expression=exp.Literal.number(self.config.max_limit)),
                            )
                            applied_limit = self.config.max_limit
                            warnings.append(
                                f"Row limit {parsed_val} exceeded maximum allowed ({self.config.max_limit}). Clamped to {self.config.max_limit}."
                            )
                        else:
                            applied_limit = parsed_val
                    else:
                        # Non-literal or complex limit expression; enforce max limit
                        ast.set(
                            "limit",
                            exp.Limit(expression=exp.Literal.number(self.config.max_limit)),
                        )
                        applied_limit = self.config.max_limit
                        warnings.append(
                            f"Dynamic LIMIT expression replaced with maximum safe limit {self.config.max_limit}."
                        )
                except Exception as exc:
                    logger.warning("Error parsing LIMIT clause: %s", exc)
                    ast.set(
                        "limit",
                        exp.Limit(expression=exp.Literal.number(self.config.default_limit)),
                    )
                    applied_limit = self.config.default_limit
            else:
                # No LIMIT clause present: automatically inject default limit
                ast.set(
                    "limit",
                    exp.Limit(expression=exp.Literal.number(self.config.default_limit)),
                )
                applied_limit = self.config.default_limit
                warnings.append(
                    f"No LIMIT clause detected. Automatically injected LIMIT {self.config.default_limit}."
                )

        # 10. Format Sanitized SQL
        sanitized_sql: str | None = None
        is_valid = len(violations) == 0

        if is_valid:
            try:
                sanitized_sql = ast.sql(dialect=self.config.dialect, pretty=True)
            except Exception as exc:
                is_valid = False
                violations.append(
                    SecurityViolation(
                        rule=ViolationRule.SYNTAX_ERROR,
                        message=f"Failed to generate sanitized SQL from AST: {str(exc)}",
                        severity=ViolationSeverity.CRITICAL,
                    )
                )

        return ValidationResult(
            is_valid=is_valid,
            original_sql=raw_sql,
            sanitized_sql=sanitized_sql,
            violations=violations,
            warnings=warnings,
            tables_referenced=sorted(tables_referenced),
            columns_referenced=sorted(columns_referenced),
            original_limit=original_limit,
            applied_limit=applied_limit,
            has_cte=has_cte,
        )


_global_validator: SQLGuardrailValidator | None = None


def get_sql_validator(
    config: GuardrailConfig | None = None,
    catalog: SemanticCatalog | None = None,
    force_reload: bool = False,
    strict_columns: bool = False,
) -> SQLGuardrailValidator:
    """Return a singleton instance of SQLGuardrailValidator.

    Args:
        config: Optional custom GuardrailConfig.
        catalog: Optional custom SemanticCatalog.
        force_reload: If True, re-instantiate validator.
        strict_columns: If True, enforce strict column allowlisting.

    Returns:
        Configured SQLGuardrailValidator instance.
    """
    global _global_validator
    if _global_validator is None or force_reload or config is not None or catalog is not None:
        _global_validator = SQLGuardrailValidator(
            config=config,
            catalog=catalog,
            strict_columns=strict_columns,
        )
    return _global_validator


def validate_sql(
    sql: str,
    config: GuardrailConfig | None = None,
    catalog: SemanticCatalog | None = None,
    retrieval: RetrievalResult | None = None,
) -> ValidationResult:
    """Convenience functional interface to validate and sanitize a SQL query.

    Args:
        sql: Raw SQL query string.
        config: Optional custom GuardrailConfig.
        catalog: Optional custom SemanticCatalog.
        retrieval: Optional RetrievalResult enabling intent conformance checks.

    Returns:
        ValidationResult.
    """
    validator = get_sql_validator(config=config, catalog=catalog)
    return validator.validate(sql, retrieval=retrieval)
