"""Dependency graph analyzer for NuGet package repositories."""

from .cli import main
from .config import (
    build_parser,
    create_configuration,
    is_url,
    output_file,
    package_name,
    package_version,
)
from .graph import (
    build_dependency_graph,
    build_test_dependency_graph,
    calculate_load_order,
    print_dependency_graph,
    print_load_order,
    read_test_graph,
)
from .models import (
    Configuration,
    ConfigurationError,
    Dependency,
    DependencyGraph,
    GraphRepositoryError,
    GraphVisualizationError,
    LoadOrderResult,
    NuGetRepositoryError,
    PackageNode,
    VisualizationResult,
)
from .nuget import (
    _version_satisfies,
    build_nuget_dependency_graph,
    build_nuspec_uri,
    discover_package_base_address,
    get_direct_dependencies,
    get_direct_dependencies_from_service_index,
    normalize_nuget_version,
    parse_nuspec,
    resolve_nuget_version,
)
from .visualization import (
    graph_to_d2,
    render_dependency_graph,
    render_svg_builtin,
)

__all__ = [
    "Configuration",
    "ConfigurationError",
    "Dependency",
    "DependencyGraph",
    "GraphRepositoryError",
    "GraphVisualizationError",
    "LoadOrderResult",
    "NuGetRepositoryError",
    "PackageNode",
    "VisualizationResult",
    "build_dependency_graph",
    "build_nuget_dependency_graph",
    "build_nuspec_uri",
    "build_parser",
    "build_test_dependency_graph",
    "calculate_load_order",
    "create_configuration",
    "discover_package_base_address",
    "get_direct_dependencies",
    "get_direct_dependencies_from_service_index",
    "graph_to_d2",
    "is_url",
    "main",
    "normalize_nuget_version",
    "output_file",
    "package_name",
    "package_version",
    "parse_nuspec",
    "print_dependency_graph",
    "print_load_order",
    "read_test_graph",
    "render_dependency_graph",
    "render_svg_builtin",
    "resolve_nuget_version",
]
