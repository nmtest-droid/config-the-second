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
    build_nuspec_uri,
    build_parser,
    create_configuration,
    discover_package_base_address,
    get_direct_dependencies,
    is_url,
    main,
    normalize_nuget_version,
    output_file,
    package_name,
    package_version,
    parse_nuspec,
)


FIXTURES = Path(__file__).parent / "fixtures"


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

    def test_get_direct_dependencies_from_local_test_repository(self):
        config = build_parser().parse_args(
            ["--package", "Demo", "--repository", str(FIXTURES / "repository.json"),
             "--test-repo-mode", "on", "--version", "1.2.3"]
        )
        dependencies = get_direct_dependencies(create_configuration(config))
        self.assertEqual(len(dependencies), 4)
        self.assertEqual(dependencies[0].package, "Newtonsoft.Json")

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

    def test_main_prints_all_configuration_and_direct_dependencies(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--package", "Demo", "--repository", str(FIXTURES / "repository.json"),
                 "--test-repo-mode", "on", "--version", "1.2.3", "--output",
                 str(Path(tempfile.gettempdir()) / "depgraph-stage2.svg")]
            )
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        for key in ("package=", "repository=", "test_repo_mode=", "version=", "output="):
            self.assertIn(key, rendered)
        self.assertIn("direct_dependencies_count=4", rendered)
        self.assertIn("package=Newtonsoft.Json", rendered)
        self.assertIn("target_framework=netstandard2.0", rendered)


if __name__ == "__main__":
    unittest.main()
