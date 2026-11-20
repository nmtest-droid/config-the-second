import argparse
import contextlib
import http.server
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cli import (
    ConfigurationError,
    NuGetRepositoryError,
    GraphRepositoryError,
    PackageNode,
    _version_satisfies,
    build_dependency_graph,
    build_nuget_dependency_graph,
    build_test_dependency_graph,
    build_nuspec_uri,
    build_parser,
    create_configuration,
    discover_package_base_address,
    get_direct_dependencies,
    get_direct_dependencies_from_service_index,
    is_url,
    main,
    normalize_nuget_version,
    output_file,
    package_name,
    package_version,
    parse_nuspec,
    read_test_graph,
    resolve_nuget_version,
)


FIXTURES = Path(__file__).parent / "fixtures"
GRAPH_FIXTURES = FIXTURES / "graphs"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def make_service_index(root: Path, base_address: str = "packages/") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "version": "3.0.0",
                "resources": [
                    {"@id": base_address, "@type": "PackageBaseAddress/3.0.0"}
                ],
            }
        ),
        encoding="utf-8",
    )
    return index_path


def write_nuspec(root: Path, package: str, version: str, xml_text: str) -> Path:
    package_id = package.lower()
    package_version = normalize_nuget_version(version)
    destination = root / "packages" / package_id / package_version / f"{package_id}.nuspec"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(xml_text, encoding="utf-8")
    return destination


def sample_nuspec(package="Demo", version="1.2.3"):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
  <metadata>
    <id>{package}</id><version>{version}</version>
    <dependencies>
      <group targetFramework="net8.0">
        <dependency id="Newtonsoft.Json" version="[13.0.1, 14.0.0)" />
        <dependency id="Utility.Core" version="2.5.0" />
      </group>
      <group targetFramework="netstandard2.0">
        <dependency id="System.Text.Json" version="[8.0.0, )" />
      </group>
      <dependency id="Plain.Dependency" version="1.0.0" />
    </dependencies>
  </metadata>
</package>'''


class ParameterValidationTests(unittest.TestCase):
    def test_valid_package_name(self):
        self.assertEqual(package_name("requests-core_2"), "requests-core_2")

    def test_invalid_package_name(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            package_name("bad package")

    def test_valid_semver(self):
        self.assertEqual(package_version("1.2.3-beta.1+build.7"), "1.2.3-beta.1+build.7")

    def test_invalid_semver(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            package_version("latest")

    def test_invalid_output_extension(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            output_file("graph.txt")

    def test_output_parent_must_exist(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            output_file("missing-directory/graph.png")

    def test_windows_drive_path_is_not_mistaken_for_url(self):
        self.assertFalse(is_url(r"C:\Users\Test\repo.json"))
        self.assertTrue(is_url("https://example.com/repo.json"))

    def test_test_mode_requires_existing_file(self):
        args = build_parser().parse_args(
            ["--package", "demo", "--repository", "missing-repository.json",
             "--test-repo-mode", "on", "--version", "1.0.0"]
        )
        with self.assertRaisesRegex(ConfigurationError, "не найден"):
            create_configuration(args)

    def test_test_mode_rejects_url(self):
        args = build_parser().parse_args(
            ["--package", "demo", "--repository", "https://example.com/repo.json",
             "--test-repo-mode", "on", "--version", "1.0.0"]
        )
        with self.assertRaisesRegex(ConfigurationError, "локальным путём"):
            create_configuration(args)

    def test_remote_mode_requires_http_or_https_url(self):
        args = build_parser().parse_args(
            ["--package", "demo", "--repository", "./repo.json",
             "--test-repo-mode", "off", "--version", "1.0.0"]
        )
        with self.assertRaisesRegex(ConfigurationError, "корректным URL"):
            create_configuration(args)

    def test_test_mode_accepts_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = Path(temp_dir) / "repo.json"
            fixture.write_text('{"version":"3.0.0","resources":[]}', encoding="utf-8")
            args = build_parser().parse_args(
                ["--package", "demo", "--repository", str(fixture),
                 "--test-repo-mode", "on", "--version", "1.0.0"]
            )
            config = create_configuration(args)
            self.assertEqual(config.repository, str(fixture.resolve()))

    def test_mode_choice_is_validated_by_argparse(self):
        with self.assertRaises(SystemExit) as error:
            build_parser().parse_args(
                ["--package", "demo", "--repository", "https://example.com/repo",
                 "--test-repo-mode", "sometimes", "--version", "1.0.0"]
            )
        self.assertEqual(error.exception.code, 2)


class DependencyGraphTests(unittest.TestCase):
    def _config(self, graph_name: str, package: str = "A"):
        args = build_parser().parse_args(
            ["--package", package, "--repository", str(GRAPH_FIXTURES / graph_name),
             "--test-repo-mode", "on", "--version", "1.0.0"]
        )
        return create_configuration(args)

    def test_test_graph_rejects_lowercase_package_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "graph.json"
            path.write_text('{"A": ["b"]}', encoding="utf-8")
            with self.assertRaisesRegex(GraphRepositoryError, "заглавные латинские"):
                read_test_graph(path)

    def test_test_graph_rejects_undeclared_dependency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "graph.json"
            path.write_text('{"A": ["B"]}', encoding="utf-8")
            with self.assertRaisesRegex(GraphRepositoryError, "отсутствует"):
                read_test_graph(path)

    def test_test_graph_rejects_missing_root_package(self):
        with self.assertRaisesRegex(GraphRepositoryError, "не найден"):
            build_test_dependency_graph(self._config("chain.json", package="Z"))

    def test_test_graph_requires_uppercase_root_package(self):
        with self.assertRaisesRegex(GraphRepositoryError, "заглавные латинские"):
            build_test_dependency_graph(self._config("chain.json", package="a"))

    def test_dfs_builds_dependency_chain(self):
        graph = build_test_dependency_graph(self._config("chain.json"))
        self.assertEqual([node.label for node in graph.adjacency], ["A", "B", "C"])
        self.assertEqual(graph.edge_count, 2)
        self.assertEqual(graph.cycles, [])

    def test_dfs_handles_branching_and_shared_dependency_once(self):
        graph = build_test_dependency_graph(self._config("branching.json"))
        self.assertEqual(len(graph.adjacency), 6)  # G не достижим из A
        self.assertEqual(graph.edge_count, 7)
        self.assertEqual(graph.adjacency[PackageNode("F")], [])
        self.assertEqual(graph.cycles, [])

    def test_dfs_detects_cycle_and_continues_other_branches(self):
        graph = build_test_dependency_graph(self._config("cycle.json"))
        self.assertEqual(len(graph.adjacency), 4)
        self.assertEqual(graph.edge_count, 4)
        self.assertEqual(len(graph.cycles), 1)
        self.assertEqual([node.label for node in graph.cycles[0]], ["A", "B", "C", "A"])
        self.assertIn(PackageNode("D"), graph.adjacency)

    def test_dfs_detects_self_loop(self):
        graph = build_test_dependency_graph(self._config("self-loop.json"))
        self.assertEqual(len(graph.cycles), 1)
        self.assertEqual([node.label for node in graph.cycles[0]], ["A", "A"])

    def test_dfs_handles_isolated_root(self):
        graph = build_test_dependency_graph(self._config("isolated.json"))
        self.assertEqual(list(graph.adjacency), [PackageNode("A")])
        self.assertEqual(graph.edge_count, 0)

    def test_dfs_is_iterative_for_deep_graph(self):
        count = 2500
        root = PackageNode("P0")

        def load(node):
            index = int(node.package[1:])
            return [PackageNode(f"P{index + 1}")] if index + 1 < count else []

        graph = build_dependency_graph(root, load)
        self.assertEqual(len(graph.adjacency), count)
        self.assertEqual(graph.edge_count, count - 1)

    def test_main_prints_graph_metrics_and_cycle_information(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--package", "A", "--repository", str(GRAPH_FIXTURES / "cycle.json"),
                 "--test-repo-mode", "on", "--version", "1.0.0",
                 "--output", str(Path(tempfile.gettempdir()) / "depgraph-stage3.svg")]
            )
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("graph_nodes_count=4", rendered)
        self.assertIn("graph_edges_count=4", rendered)
        self.assertIn("cycles_count=1", rendered)
        self.assertIn("A -> B -> C -> A", rendered)

    def test_version_range_selection(self):
        versions = ["1.0.0-beta.1", "1.0.0", "1.5.0", "2.0.0"]
        self.assertTrue(_version_satisfies("1.5.0", "[1.0.0, 2.0.0)"))
        self.assertFalse(_version_satisfies("2.0.0", "[1.0.0, 2.0.0)"))
        self.assertTrue(_version_satisfies("1.5.0", "1.0.0"))
        self.assertTrue(_version_satisfies("1.5.0", "*"))
        self.assertEqual(
            resolve_nuget_version("https://example.test/flat/", "Dep", "[1.0.0, 2.0.0)",
                                  {"dep": versions}),
            "1.5.0",
        )


class NuGetMetadataTests(unittest.TestCase):
    def test_build_nuspec_uri_drops_semver_build_metadata(self):
        self.assertEqual(
            normalize_nuget_version("1.2.3-BETA.1+build.7"), "1.2.3-beta.1"
        )
        self.assertEqual(
            build_nuspec_uri("https://example.org/flat/", "Demo", "1.2.3+build.7"),
            "https://example.org/flat/demo/1.2.3/demo.nuspec",
        )

    def test_parse_nuspec_reads_direct_dependencies_from_all_groups(self):
        dependencies = parse_nuspec(sample_nuspec(), "demo", "1.2.3")
        self.assertEqual(
            [item.package for item in dependencies],
            ["Newtonsoft.Json", "Utility.Core", "System.Text.Json", "Plain.Dependency"],
        )
        self.assertEqual(dependencies[0].version_range, "[13.0.1, 14.0.0)")
        self.assertEqual(dependencies[0].target_framework, "net8.0")
        self.assertEqual(dependencies[2].target_framework, "netstandard2.0")
        self.assertIsNone(dependencies[3].target_framework)

    def test_parse_nuspec_rejects_wrong_package_or_version(self):
        content = sample_nuspec()
        with self.assertRaisesRegex(NuGetRepositoryError, "идентификатор пакета"):
            parse_nuspec(content.encode(), "AnotherPackage", "1.2.3")
        with self.assertRaisesRegex(NuGetRepositoryError, "версия пакета"):
            parse_nuspec(content.encode(), "Demo", "2.0.0")

    def test_parse_nuspec_rejects_invalid_xml(self):
        with self.assertRaisesRegex(NuGetRepositoryError, "некорректный XML"):
            parse_nuspec(b"<package><metadata>", "Demo", "1.2.3")

    def test_parse_nuspec_returns_empty_for_package_without_dependencies(self):
        xml_text = (
            '<package><metadata><id>Demo</id><version>1.2.3</version>'
            '<description>No dependencies</description></metadata></package>'
        )
        self.assertEqual(parse_nuspec(xml_text.encode(), "Demo", "1.2.3"), [])

    def test_discovery_requires_package_base_address(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index = Path(temp_dir) / "index.json"
            index.write_text('{"version":"3.0.0","resources":[]}', encoding="utf-8")
            with self.assertRaisesRegex(NuGetRepositoryError, "PackageBaseAddress"):
                discover_package_base_address(index.as_uri())

    def test_get_direct_dependencies_from_local_nuget_service_index(self):
        service_index_uri = (FIXTURES / "repository.json").resolve().as_uri()
        dependencies = get_direct_dependencies_from_service_index(
            service_index_uri, "Demo", "1.2.3"
        )
        self.assertEqual(len(dependencies), 4)
        self.assertEqual(dependencies[0].package, "Newtonsoft.Json")

    def test_direct_nuget_helper_rejects_stage3_test_graph_mode(self):
        args = build_parser().parse_args(
            ["--package", "A", "--repository", str(GRAPH_FIXTURES / "chain.json"),
             "--test-repo-mode", "on", "--version", "1.0.0"]
        )
        with self.assertRaisesRegex(NuGetRepositoryError, "JSON-описанием графа"):
            get_direct_dependencies(create_configuration(args))

    def test_get_direct_dependencies_from_http_nuget_v3_repository(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_service_index(root)
            write_nuspec(root, "Demo", "1.2.3", sample_nuspec())
            handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(root), **kwargs)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                service_index_url = f"http://127.0.0.1:{server.server_port}/index.json"
                args = build_parser().parse_args(
                    ["--package", "Demo", "--repository", service_index_url,
                     "--test-repo-mode", "off", "--version", "1.2.3"]
                )
                dependencies = get_direct_dependencies(create_configuration(args))
                self.assertEqual(len(dependencies), 4)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_build_nuget_dependency_graph_resolves_transitive_versions_and_cycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_service_index(root)

            def add_nuspec(package, version, dependencies):
                xml = (
                    f'<package><metadata><id>{package}</id><version>{version}</version>'
                    f'<dependencies>{dependencies}</dependencies></metadata></package>'
                )
                write_nuspec(root, package, version, xml)

            (root / "packages" / "a").mkdir(parents=True)
            (root / "packages" / "a" / "index.json").write_text(
                json.dumps({"versions": ["1.0.0", "1.1.0"]}), encoding="utf-8"
            )
            (root / "packages" / "b").mkdir(parents=True)
            (root / "packages" / "b" / "index.json").write_text(
                json.dumps({"versions": ["2.0.0"]}), encoding="utf-8"
            )
            add_nuspec("Root", "1.0.0", '<dependency id="A" version="[1.0.0, 2.0.0)" />')
            add_nuspec("A", "1.0.0", "")
            add_nuspec("A", "1.1.0", '<dependency id="B" version="[2.0.0]" />')
            add_nuspec("B", "2.0.0", '<dependency id="A" version="[1.0.0, 2.0.0)" />')

            handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(root), **kwargs)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                service_index_url = f"http://127.0.0.1:{server.server_port}/index.json"
                args = build_parser().parse_args(
                    ["--package", "Root", "--repository", service_index_url,
                     "--test-repo-mode", "off", "--version", "1.0.0"]
                )
                graph = build_nuget_dependency_graph(create_configuration(args))
                self.assertEqual(
                    {node.label for node in graph.adjacency},
                    {"root@1.0.0", "a@1.1.0", "b@2.0.0"},
                )
                self.assertEqual(graph.edge_count, 3)
                self.assertEqual(len(graph.cycles), 1)
                self.assertEqual(
                    [node.label for node in graph.cycles[0]],
                    ["a@1.1.0", "b@2.0.0", "a@1.1.0"],
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_404_for_requested_package_version_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_service_index(root)
            handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(root), **kwargs)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                service_index_url = f"http://127.0.0.1:{server.server_port}/index.json"
                args = build_parser().parse_args(
                    ["--package", "Demo", "--repository", service_index_url,
                     "--test-repo-mode", "off", "--version", "9.9.9"]
                )
                with self.assertRaisesRegex(NuGetRepositoryError, "HTTP 404"):
                    get_direct_dependencies(create_configuration(args))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_main_prints_all_configuration_and_dependency_graph(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--package", "A", "--repository", str(GRAPH_FIXTURES / "chain.json"),
                 "--test-repo-mode", "on", "--version", "1.2.3", "--output",
                 str(Path(tempfile.gettempdir()) / "depgraph-stage3.svg")]
            )
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        for key in ("package=", "repository=", "test_repo_mode=", "version=", "output="):
            self.assertIn(key, rendered)
        self.assertIn("graph_nodes_count=3", rendered)
        self.assertIn("A -> B", rendered)
        self.assertIn("B -> C", rendered)


if __name__ == "__main__":
    unittest.main()
