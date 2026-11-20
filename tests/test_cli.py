import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from cli import (
    ConfigurationError,
    build_parser,
    create_configuration,
    is_url,
    main,
    output_file,
    package_name,
    package_version,
)


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
            [
                "--package", "demo",
                "--repository", "missing-repository.json",
                "--test-repo-mode", "on",
                "--version", "1.0.0",
            ]
        )
        with self.assertRaisesRegex(ConfigurationError, "не найден"):
            create_configuration(args)

    def test_test_mode_rejects_url(self):
        args = build_parser().parse_args(
            [
                "--package", "demo",
                "--repository", "https://example.com/repo.json",
                "--test-repo-mode", "on",
                "--version", "1.0.0",
            ]
        )
        with self.assertRaisesRegex(ConfigurationError, "локальным путём"):
            create_configuration(args)

    def test_remote_mode_requires_http_or_https_url(self):
        args = build_parser().parse_args(
            [
                "--package", "demo",
                "--repository", "./repo.json",
                "--test-repo-mode", "off",
                "--version", "1.0.0",
            ]
        )
        with self.assertRaisesRegex(ConfigurationError, "корректным URL"):
            create_configuration(args)

    def test_test_mode_accepts_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = Path(temp_dir) / "repo.json"
            fixture.write_text('{"packages": []}', encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "--package", "demo",
                    "--repository", str(fixture),
                    "--test-repo-mode", "on",
                    "--version", "1.0.0",
                ]
            )
            config = create_configuration(args)
            self.assertEqual(config.repository, str(fixture.resolve()))

    def test_mode_choice_is_validated_by_argparse(self):
        with self.assertRaises(SystemExit) as error:
            build_parser().parse_args(
                [
                    "--package", "demo",
                    "--repository", "https://example.com/repo",
                    "--test-repo-mode", "sometimes",
                    "--version", "1.0.0",
                ]
            )
        self.assertEqual(error.exception.code, 2)

    def test_all_configuration_values_are_printed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = Path(temp_dir) / "repo.json"
            fixture.write_text("{}", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(
                    [
                        "--package", "demo",
                        "--repository", str(fixture),
                        "--test-repo-mode", "on",
                        "--version", "1.2.3",
                        "--output", str(Path(temp_dir) / "graph.svg"),
                    ]
                )
            self.assertEqual(result, 0)
            rendered = output.getvalue()
            for key in ("package=", "repository=", "test_repo_mode=", "version=", "output="):
                self.assertIn(key, rendered)


if __name__ == "__main__":
    unittest.main()
