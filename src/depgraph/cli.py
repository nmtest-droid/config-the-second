"""Application entry point for the dependency graph CLI."""

from __future__ import annotations

from .config import build_parser, create_configuration, print_configuration
from .graph import (
    build_test_dependency_graph,
    print_dependency_graph,
    print_load_order,
)
from .models import (
    ConfigurationError,
    GraphRepositoryError,
    GraphVisualizationError,
    NuGetRepositoryError,
)
from .nuget import build_nuget_dependency_graph
from .visualization import render_dependency_graph



def main(argv: list[str] | None = None) -> int:
    """Run configuration, dependency retrieval, graph analysis and export."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = create_configuration(args)
    except ConfigurationError as exc:
        parser.error(str(exc))

    print_configuration(config)
    try:
        if config.test_repo_mode == "on":
            graph = build_test_dependency_graph(config)
        else:
            graph = build_nuget_dependency_graph(config)
    except (NuGetRepositoryError, GraphRepositoryError) as exc:
        parser.error(f"ошибка построения графа зависимостей: {exc}")

    print_dependency_graph(graph)
    if args.show_load_order:
        print_load_order(graph)
    try:
        visualization = render_dependency_graph(graph, config.output_file)
    except GraphVisualizationError as exc:
        parser.error(f"ошибка визуализации графа: {exc}")
    print(f"d2_source={visualization.d2_file}")
    print(f"svg_output={visualization.svg_file}")
    print(f"svg_renderer={visualization.renderer}")
    return 0
