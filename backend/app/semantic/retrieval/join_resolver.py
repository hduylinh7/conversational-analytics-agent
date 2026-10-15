"""Graph-based join path resolver enforcing declared relationships and detecting aggregation risks."""

import re
from collections import deque

from app.semantic.models import (
    DataSourcesCatalog,
    FilterCondition,
    RelationshipMetadata,
)
from app.semantic.retrieval.schemas import (
    JoinStep,
    ResolvedJoinPath,
    SelectedDimension,
    SelectedMetric,
)


class JoinResolver:
    """Resolves table dependencies, shortest valid join paths, and business join restrictions."""

    def __init__(self, data_sources: DataSourcesCatalog) -> None:
        """Initialize resolver with database catalog and build relationship graph.

        Args:
            data_sources: Catalog of tables, columns, relationships, and restrictions.
        """
        self.data_sources = data_sources
        self._table_map = data_sources.table_map
        self._relationships = data_sources.relationships
        self._restrictions = data_sources.join_restrictions

        # Regex to extract schema.table.column patterns from SQL expressions
        self._col_ref_pattern = re.compile(
            r"\b([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b"
        )

        # Build adjacency graph: table -> list of (neighbor_table, RelationshipMetadata)
        self._adj: dict[str, list[tuple[str, RelationshipMetadata]]] = {
            t.name: [] for t in data_sources.tables
        }
        for rel in self._relationships:
            if rel.source_table in self._adj:
                self._adj[rel.source_table].append((rel.target_table, rel))

    def resolve(
        self,
        selected_metrics: list[SelectedMetric],
        selected_dimensions: list[SelectedDimension],
    ) -> tuple[ResolvedJoinPath, list[str], list[str], list[FilterCondition], list[str], bool]:
        """Resolve required tables, columns, join paths, and warnings for candidate selections.

        Args:
            selected_metrics: List of chosen metrics.
            selected_dimensions: List of chosen dimensions.

        Returns:
            Tuple of:
            - ResolvedJoinPath
            - required_tables (list of schema.table)
            - required_columns (list of schema.table.column)
            - mandatory_filters (list of FilterCondition)
            - all_warnings (list of strings)
            - is_safe_for_direct_execution (bool)
        """
        all_warnings: list[str] = []
        is_safe = True

        # 1. Identify Required Tables
        req_tables_set: set[str] = set()
        req_tables_ordered: list[str] = []

        def add_table(tbl: str) -> None:
            if tbl and tbl not in req_tables_set:
                req_tables_set.add(tbl)
                req_tables_ordered.append(tbl)

        for m in selected_metrics:
            for tbl in m.source_tables:
                add_table(tbl)

        for d in selected_dimensions:
            add_table(d.source_table)

        # 2. Identify Required Columns
        req_cols_set: set[str] = set()
        req_cols_ordered: list[str] = []

        def add_column(col_fqn: str) -> None:
            if col_fqn and col_fqn not in req_cols_set:
                req_cols_set.add(col_fqn)
                req_cols_ordered.append(col_fqn)

        for d in selected_dimensions:
            add_column(f"{d.source_table}.{d.source_column}")

        for m in selected_metrics:
            matches = self._col_ref_pattern.findall(m.expression)
            for tbl, col in matches:
                add_column(f"{tbl}.{col}")

        # 3. Gather Mandatory Filters and Metric Warnings
        mandatory_filters: list[FilterCondition] = []
        for m in selected_metrics:
            mandatory_filters.extend(m.filters)
            for w in m.warnings:
                if w not in all_warnings:
                    all_warnings.append(f"METRIC WARNING [{m.name}]: {w}")

        # 4. Check Explicit Join Restrictions (e.g. Chasm Traps)
        for restriction in self._restrictions:
            forbidden = set(restriction.forbidden_concurrent_joins)
            if forbidden.issubset(req_tables_set):
                is_safe = False
                msg = (
                    f"CRITICAL JOIN RESTRICTION [{restriction.name}]: {restriction.reason} "
                    f"Remediation: {restriction.remediation}"
                )
                all_warnings.append(msg)

        # 5. Resolve Join Paths
        if not req_tables_ordered:
            empty_path = ResolvedJoinPath(
                root_table="",
                required_tables=[],
                intermediate_tables=[],
                steps=[],
                relationships=[],
                is_safe_for_direct_execution=True,
                warnings=all_warnings,
            )
            return empty_path, [], [], mandatory_filters, all_warnings, True

        root_table = req_tables_ordered[0]

        if len(req_tables_ordered) == 1:
            single_path = ResolvedJoinPath(
                root_table=root_table,
                required_tables=req_tables_ordered,
                intermediate_tables=[],
                steps=[],
                relationships=[],
                is_safe_for_direct_execution=is_safe,
                warnings=all_warnings,
            )
            return single_path, req_tables_ordered, req_cols_ordered, mandatory_filters, all_warnings, is_safe

        # Multiple tables required: compute join tree via BFS
        resolved_steps: list[JoinStep] = []
        resolved_relationships: list[RelationshipMetadata] = []
        visited_tables: set[str] = {root_table}
        intermediate_tables: list[str] = []

        for target in req_tables_ordered[1:]:
            if target in visited_tables:
                continue

            path_edges = self._find_shortest_path_to_visited(target, visited_tables)
            if path_edges is None:
                is_safe = False
                all_warnings.append(
                    f"MISSING JOIN PATH: No declared relationship connects '{target}' to the query table tree "
                    f"rooted at '{root_table}'. Arbitrary JOINs are not allowed."
                )
                continue

            for from_tbl, to_tbl, rel in path_edges:
                visited_tables.add(to_tbl)
                if to_tbl not in req_tables_set and to_tbl not in intermediate_tables:
                    intermediate_tables.append(to_tbl)

                step = JoinStep(
                    from_table=from_tbl,
                    to_table=to_tbl,
                    join_type=rel.join_type,
                    join_condition=rel.join_condition,
                    relationship_name=rel.name,
                    cardinality=rel.cardinality,
                    is_safe_for_aggregation=rel.is_safe_for_aggregation,
                    warning=rel.warning,
                )
                resolved_steps.append(step)
                resolved_relationships.append(rel)

                # Check cardinality fan-out risk
                if rel.cardinality == "one_to_many":
                    warn_msg = (
                        f"ONE-TO-MANY FAN-OUT RISK [{rel.name}]: Joining {from_tbl} -> {to_tbl} "
                        f"fans out rows (1:N cardinality). Order-level metric aggregations require "
                        f"COUNT(DISTINCT) or isolated grouping. DISTINCT does NOT resolve SUM aggregation errors. "
                        f"{rel.warning or ''}"
                    )
                    if warn_msg not in all_warnings:
                        all_warnings.append(warn_msg)

        # Include intermediate tables into required tables list
        all_required_tables = list(req_tables_ordered)
        for itbl in intermediate_tables:
            if itbl not in all_required_tables:
                all_required_tables.append(itbl)

        resolved_path = ResolvedJoinPath(
            root_table=root_table,
            required_tables=all_required_tables,
            intermediate_tables=intermediate_tables,
            steps=resolved_steps,
            relationships=resolved_relationships,
            is_safe_for_direct_execution=is_safe,
            warnings=all_warnings,
        )

        return (
            resolved_path,
            all_required_tables,
            req_cols_ordered,
            mandatory_filters,
            all_warnings,
            is_safe,
        )

    def _find_shortest_path_to_visited(
        self,
        target_table: str,
        visited_tables: set[str],
    ) -> list[tuple[str, str, RelationshipMetadata]] | None:
        """Find the shortest sequence of relationships connecting any visited table to target_table."""
        # Run BFS from the set of currently visited tables towards target_table
        queue: deque[str] = deque(list(visited_tables))
        parents: dict[str, tuple[str, RelationshipMetadata]] = {}
        seen: set[str] = set(visited_tables)

        found = False
        while queue:
            current = queue.popleft()
            if current == target_table:
                found = True
                break

            for neighbor, rel in self._adj.get(current, []):
                if neighbor not in seen:
                    seen.add(neighbor)
                    parents[neighbor] = (current, rel)
                    queue.append(neighbor)

        if not found:
            return None

        # Reconstruct path from target back to a visited table
        edges_reversed: list[tuple[str, str, RelationshipMetadata]] = []
        curr = target_table
        while curr not in visited_tables:
            parent_table, rel = parents[curr]
            edges_reversed.append((parent_table, curr, rel))
            curr = parent_table

        return list(reversed(edges_reversed))
