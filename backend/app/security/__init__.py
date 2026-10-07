"""Security module: SQL AST validation, guardrails, and query policies.

Intended future responsibilities:
- AST Validation (e.g. sqlglot): Parse query AST to inspect clauses and structure.
- SELECT-only Enforcement: Reject DDL, DML (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE).
- Table & Column Allowlisting: Restrict query execution to approved catalog entities.
- Dangerous Function Detection: Block execution of file I/O, system commands, and malicious UDFs.
- LIMIT Enforcement: Automatically inject/enforce max row limits (e.g., LIMIT 1000).
- Query Policy Validation: Enforce query timeouts and resource thresholds.
"""

__all__: list[str] = []
