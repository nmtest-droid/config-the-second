"""Command-line configuration parsing and validation."""

from __future__ import annotations

import argparse
import re
import urllib.parse
from pathlib import Path

from .models import Configuration, ConfigurationError

PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
SUPPORTED_IMAGE_FORMATS = {".svg"}
WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def is_url(value: str) -> bool:
    """Определяет URL, не принимая Windows-пути вида C:\\repo.json за URI."""
    if WINDOWS_DRIVE_PREFIX_RE.match(value):
        return False
    return bool(urllib.parse.urlparse(value).scheme)


def non_empty_string(value: str) -> str:
    """Reject blank command-line values and return their trimmed form."""
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("значение не должно быть пустым")
    return normalized


def package_name(value: str) -> str:
    """Validate the NuGet package ID supplied by the user."""
    value = non_empty_string(value)
    if not PACKAGE_NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "недопустимое имя пакета: используйте 1–128 символов "
            "(латинские буквы, цифры, '.', '_' или '-'); "
            "первый символ — буква или цифра"
        )
    return value


def package_version(value: str) -> str:
    """Validate the exact SemVer version supplied by the user."""
    value = non_empty_string(value)
    if not SEMVER_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "версия должна соответствовать SemVer, например "
            "1.2.3, 1.2.3-beta.1 или 1.2.3+build.7"
        )
    return value


def output_file(value: str) -> Path:
    """Validate that the output path names an SVG in an existing folder."""
    value = non_empty_string(value)
    path = Path(value).expanduser()
    if not path.name or path.name in {".", ".."}:
        raise argparse.ArgumentTypeError("укажите имя выходного файла")
    if path.suffix.lower() not in SUPPORTED_IMAGE_FORMATS:
        extensions = ", ".join(sorted(SUPPORTED_IMAGE_FORMATS))
        raise argparse.ArgumentTypeError(
            "неподдерживаемый формат изображения; допустимые расширения: "
            f"{extensions}"
        )
    if path.parent.exists() and not path.parent.is_dir():
        raise argparse.ArgumentTypeError(
            "родительский путь выходного файла не является каталогом"
        )
    if not path.parent.exists():
        raise argparse.ArgumentTypeError(
            "каталог для выходного файла не существует"
        )
    return path


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    """Add package, repository and version options to the parser."""
    parser.add_argument(
        "--package",
        required=True,
        type=package_name,
        metavar="NAME",
        help="имя анализируемого NuGet-пакета",
    )
    parser.add_argument(
        "--repository",
        required=True,
        type=non_empty_string,
        metavar="URL_OR_PATH",
        help=(
            "URL NuGet V3 Service Index или путь к JSON-описанию "
            "графа в тестовом режиме"
        ),
    )
    parser.add_argument(
        "--version",
        required=True,
        type=package_version,
        metavar="SEMVER",
        help="точная версия NuGet-пакета в формате SemVer",
    )


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    """Add output-file and optional load-order arguments."""
    parser.add_argument(
        "--output",
        default=Path("dependency-graph.svg"),
        type=output_file,
        metavar="FILE",
        help=(
            "имя выходного SVG-файла; рядом сохраняется файл .d2 "
            "(по умолчанию: dependency-graph.svg)"
        ),
    )
    parser.add_argument(
        "--show-load-order",
        action="store_true",
        help=(
            "дополнительно вывести порядок загрузки: сначала зависимости, "
            "затем корневой пакет"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser and its documented options."""
    parser = argparse.ArgumentParser(
        prog="depgraph",
        description="Построение транзитивного графа зависимостей NuGet-пакета.",
        epilog=(
            "В тестовом режиме repository указывает на JSON-файл графа с "
            "именами вершин A-Z. В обычном режиме нужен NuGet V3 Service "
            "Index с протоколом http или https."
        ),
    )
    _add_identity_arguments(parser)
    parser.add_argument(
        "--test-repo-mode",
        choices=("on", "off"),
        default="off",
        metavar="{on,off}",
        help=(
            "on — локальный JSON-граф, off — удалённый NuGet V3 "
            "(по умолчанию: off)"
        ),
    )
    _add_output_arguments(parser)
    return parser


def _validate_test_repository(repository: str) -> str:
    """Resolve a valid local test repository file path."""
    if is_url(repository):
        raise ConfigurationError(
            "в тестовом режиме repository должен быть локальным путём"
        )
    repo_path = Path(repository).expanduser()
    if not repo_path.exists():
        raise ConfigurationError(
            f"файл тестового репозитория не найден: {repo_path}"
        )
    if not repo_path.is_file():
        raise ConfigurationError(
            f"путь тестового репозитория не является файлом: {repo_path}"
        )
    return str(repo_path.resolve())


def _validate_remote_repository(repository: str) -> str:
    """Validate an HTTP or HTTPS NuGet V3 Service Index URL."""
    parsed_url = urllib.parse.urlparse(repository)
    invalid_url = (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or any(character.isspace() for character in repository)
    )
    try:
        parsed_url.port
    except ValueError:
        invalid_url = True
    if invalid_url:
        raise ConfigurationError(
            "repository должен быть корректным URL с http или https"
        )
    return repository


def create_configuration(args: argparse.Namespace) -> Configuration:
    """Validate command-line values and return an immutable configuration."""
    repository = args.repository
    if args.test_repo_mode == "on":
        repository = _validate_test_repository(repository)
    else:
        repository = _validate_remote_repository(repository)
    return Configuration(
        package=args.package,
        repository=repository,
        test_repo_mode=args.test_repo_mode,
        version=args.version,
        output_file=args.output,
    )


def print_configuration(config: Configuration) -> None:
    """Выводит все пользовательские параметры в формате ключ=значение."""
    print(f"package={config.package}")
    print(f"repository={config.repository}")
    print(f"test_repo_mode={config.test_repo_mode}")
    print(f"version={config.version}")
    print(f"output={config.output_file}")
