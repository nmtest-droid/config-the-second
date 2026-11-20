"""Dependency graph parsing, iterative DFS and load-order analysis."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import (
    Configuration,
    DependencyGraph,
    GraphRepositoryError,
    LoadOrderResult,
    PackageNode,
)

TEST_PACKAGE_NAME_RE = re.compile(r"^[A-Z]+$")
NO_DEPENDENCIES = 0
FIRST_INDEX = 0


def _validate_graph_document(document: object) -> dict[str, list[str]]:
    """Validate the JSON graph shape and normalize each adjacency list."""
    if not isinstance(document, dict) or not document:
        raise GraphRepositoryError(
            "описание графа должно быть непустым JSON-объектом"
        )
    graph: dict[str, list[str]] = {}
    for package, dependencies in document.items():
        _validate_package_name(package)
        graph[package] = _normalize_dependency_names(package, dependencies)
    _validate_declared_dependencies(graph)
    return graph


def _validate_package_name(package: object) -> None:
    """Validate a test graph package identifier against the A-Z rule."""
    if isinstance(package, str) and TEST_PACKAGE_NAME_RE.fullmatch(package):
        return
    raise GraphRepositoryError(
        f"недопустимое имя пакета {package!r}: используйте только "
        "заглавные латинские буквы A-Z"
    )


def _normalize_dependency_names(
    package: str,
    dependencies: object,
) -> list[str]:
    """Validate and de-duplicate a package's dependency names."""
    if not isinstance(dependencies, list):
        raise GraphRepositoryError(
            f"список зависимостей пакета {package} должен быть массивом"
        )
    normalized: list[str] = []
    for dependency in dependencies:
        _validate_package_name(dependency)
        if dependency not in normalized:
            normalized.append(dependency)
    return normalized


def _validate_declared_dependencies(graph: dict[str, list[str]]) -> None:
    """Ensure every referenced dependency is declared as a graph vertex."""
    declared = set(graph)
    for package, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in declared:
                raise GraphRepositoryError(
                    f"пакет {dependency}, указанный как зависимость {package}, "
                    "отсутствует в описании графа"
                )


def read_test_graph(repository_path: str | Path) -> dict[str, list[str]]:
    """Read and validate a JSON adjacency-list test repository."""
    path = Path(repository_path)
    try:
        payload = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise GraphRepositoryError(
            f"не удалось прочитать файл графа {path}: {exc}"
        ) from exc
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GraphRepositoryError(
            "файл тестового графа содержит некорректный JSON "
            f"(строка {exc.lineno}, столбец {exc.colno})"
        ) from exc
    return _validate_graph_document(document)


@dataclass
class DfsState:
    """Mutable working state for the iterative three-color DFS."""

    adjacency: dict[PackageNode, list[PackageNode]]
    colors: dict[PackageNode, str]
    active: list[PackageNode]
    active_positions: dict[PackageNode, int]
    loaded: set[PackageNode]
    cycles: list[tuple[PackageNode, ...]]
    frames: list[tuple[PackageNode, int]]


def _new_dfs_state(root: PackageNode) -> DfsState:
    """Create DFS state with the root as the initial active vertex."""
    return DfsState(
        adjacency={root: []},
        colors={root: "gray"},
        active=[root],
        active_positions={root: 0},
        loaded=set(),
        cycles=[],
        frames=[(root, 0)],
    )


def _load_unique_neighbors(
    node: PackageNode,
    dependency_loader: Callable[[PackageNode], list[PackageNode]],
) -> list[PackageNode]:
    """Load a vertex's dependencies while preserving their source order."""
    neighbors = dependency_loader(node)
    unique_neighbors: list[PackageNode] = []
    seen_neighbors: set[PackageNode] = set()
    for neighbor in neighbors:
        if not isinstance(neighbor, PackageNode):
            raise TypeError(
                "dependency_loader должен возвращать объекты PackageNode"
            )
        if neighbor not in seen_neighbors:
            unique_neighbors.append(neighbor)
            seen_neighbors.add(neighbor)
    return unique_neighbors


def _finish_dfs_vertex(node: PackageNode, state: DfsState) -> None:
    """Mark a vertex complete and remove it from the active DFS path."""
    state.frames.pop()
    state.colors[node] = "black"
    state.active_positions.pop(node, None)
    state.active.pop()


def _discover_dfs_neighbor(
    neighbor: PackageNode,
    state: DfsState,
) -> None:
    """Push a white neighbor or record a back edge to a gray vertex."""
    state.adjacency.setdefault(neighbor, [])
    color = state.colors.get(neighbor, "white")
    if color == "white":
        state.colors[neighbor] = "gray"
        state.active_positions[neighbor] = len(state.active)
        state.active.append(neighbor)
        state.frames.append((neighbor, 0))
    elif color == "gray":
        cycle_start = state.active_positions[neighbor]
        cycle = tuple(state.active[cycle_start:] + [neighbor])
        state.cycles.append(cycle)


def build_dependency_graph(
    root: PackageNode,
    dependency_loader: Callable[[PackageNode], list[PackageNode]],
) -> DependencyGraph:
    """Build a reachable graph using iterative DFS with cycle detection."""
    state = _new_dfs_state(root)
    while state.frames:
        node, next_index = state.frames[-1]
        if node not in state.loaded:
            state.adjacency[node] = _load_unique_neighbors(
                node,
                dependency_loader,
            )
            state.loaded.add(node)
        if next_index >= len(state.adjacency[node]):
            _finish_dfs_vertex(node, state)
            continue
        neighbor = state.adjacency[node][next_index]
        state.frames[-1] = (node, next_index + 1)
        _discover_dfs_neighbor(neighbor, state)

    return DependencyGraph(state.adjacency, state.cycles)


def build_test_dependency_graph(config: Configuration) -> DependencyGraph:
    """Строит граф из локального JSON-файла в режиме тестирования."""
    repository_graph = read_test_graph(config.repository)
    if not TEST_PACKAGE_NAME_RE.fullmatch(config.package):
        raise GraphRepositoryError(
            "в тестовом режиме --package должен содержать только "
            "заглавные латинские буквы A-Z"
        )
    if config.package not in repository_graph:
        raise GraphRepositoryError(
            f"пакет {config.package} не найден в тестовом описании графа"
        )

    def load_neighbors(node: PackageNode) -> list[PackageNode]:
        """Map the current test vertex to its declared dependency nodes."""
        packages = repository_graph[node.package]
        return [PackageNode(package) for package in packages]

    return build_dependency_graph(PackageNode(config.package), load_neighbors)


def _build_dependents(
    adjacency: dict[PackageNode, list[PackageNode]],
) -> dict[PackageNode, list[PackageNode]]:
    """Reverse dependency edges to map each node to its dependents."""
    dependents = {node: [] for node in adjacency}
    for package, dependencies in adjacency.items():
        for dependency in dependencies:
            dependents[dependency].append(package)
    return dependents


def _collect_ready_nodes(
    nodes: list[PackageNode],
    remaining_dependencies: dict[PackageNode, int],
) -> deque[PackageNode]:
    """Collect graph vertices that have no remaining dependencies."""
    ready = (
        node
        for node in nodes
        if remaining_dependencies[node] == NO_DEPENDENCIES
    )
    return deque(ready)


def _remove_root_from_load_order(
    nodes: list[PackageNode],
    root: PackageNode | None,
) -> list[PackageNode]:
    """Return dependency order without the analyzed root package."""
    return [node for node in nodes if node != root]


def calculate_load_order(graph: DependencyGraph) -> LoadOrderResult:
    """Return a dependency-first topological order using Kahn's algorithm."""
    nodes = list(graph.adjacency)
    remaining = {
        node: len(graph.adjacency.get(node, [])) for node in nodes
    }
    dependents = _build_dependents(graph.adjacency)
    ready = _collect_ready_nodes(nodes, remaining)
    order: list[PackageNode] = []

    while ready:
        dependency = ready.popleft()
        order.append(dependency)
        for package in dependents[dependency]:
            remaining[package] -= 1
            if remaining[package] == NO_DEPENDENCIES:
                ready.append(package)

    loaded = set(order)
    blocked = [node for node in nodes if node not in loaded]
    root = nodes[FIRST_INDEX] if nodes else None
    return LoadOrderResult(
        order=_remove_root_from_load_order(order, root),
        blocked=blocked,
    )


def print_load_order(graph: DependencyGraph) -> LoadOrderResult:
    """Print a complete or partial dependency-first load order."""
    result = calculate_load_order(graph)
    if result.is_complete:
        print("load_order_status=complete")
    else:
        print("load_order_status=incomplete_cycle_detected")

    if result.order:
        print(
            "load_order (transitive dependencies first; root package excluded):"
        )
        for index, node in enumerate(result.order, start=1):
            print(f"{index}. {node.label}")
    else:
        suffix = " (no dependencies)" if result.is_complete else ""
        print(f"load_order=none{suffix}")

    if result.blocked:
        blocked_labels = ", ".join(node.label for node in result.blocked)
        print(f"load_order_blocked_packages={blocked_labels}")
        print(
            "load_order_note=полный порядок невозможен: перечисленные пакеты "
            "образуют цикл или зависят от циклической компоненты"
        )
    return result


def print_dependency_graph(graph: DependencyGraph) -> None:
    """Печатает список смежности, размеры графа и информацию о циклах."""
    print(f"graph_nodes_count={len(graph.adjacency)}")
    print(f"graph_edges_count={graph.edge_count}")
    print("dependency_graph:")
    for node, neighbors in graph.adjacency.items():
        targets = ", ".join(item.label for item in neighbors)
        targets = targets if neighbors else "none"
        print(f"- {node.label} -> {targets}")

    print(f"cycles_count={len(graph.cycles)}")
    if graph.cycles:
        print("cycles:")
        for cycle in graph.cycles:
            print("- " + " -> ".join(node.label for node in cycle))
    else:
        print("cycles=none")
