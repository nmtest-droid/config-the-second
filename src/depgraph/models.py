"""Shared data models and domain exceptions for dependency graphs."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Configuration:
    """Validated command-line configuration for one graph request."""

    package: str
    repository: str
    test_repo_mode: str
    version: str
    output_file: Path


@dataclass(frozen=True)
class Dependency:
    """Direct package dependency parsed from NuGet nuspec metadata."""

    package: str
    version_range: str
    target_framework: str | None = None
    include: str | None = None
    exclude: str | None = None


class ConfigurationError(ValueError):
    """Raised when supplied command-line options are inconsistent."""


class NuGetRepositoryError(RuntimeError):
    """Raised when NuGet metadata cannot be retrieved or interpreted."""


class GraphRepositoryError(RuntimeError):
    """Raised when a local test graph cannot be read or validated."""


@dataclass(frozen=True)
class PackageNode:
    """One package vertex; test graph nodes omit package versions."""

    package: str
    version: str | None = None

    @property
    def label(self) -> str:
        """Return the printable package identifier and optional version."""
        if self.version is None:
            return self.package
        return f"{self.package}@{self.version}"


@dataclass
class DependencyGraph:
    """Directed reachable dependency graph and cycles found during DFS."""

    adjacency: dict[PackageNode, list[PackageNode]]
    cycles: list[tuple[PackageNode, ...]]

    @property
    def edge_count(self) -> int:
        """Return the number of directed dependency edges."""
        return sum(len(neighbors) for neighbors in self.adjacency.values())


@dataclass(frozen=True)
class LoadOrderResult:
    """Dependency-first order and nodes blocked by cyclic components."""

    order: list[PackageNode]
    blocked: list[PackageNode]

    @property
    def is_complete(self) -> bool:
        """Return whether all reachable dependencies have a valid order."""
        return not self.blocked


class GraphVisualizationError(RuntimeError):
    """Raised when D2 source or SVG output cannot be generated."""


@dataclass(frozen=True)
class VisualizationResult:
    """Paths and renderer identifier for generated visualization files."""

    d2_file: Path
    svg_file: Path
    renderer: str
