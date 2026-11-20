"""Минимальный CLI-прототип визуализатора графа зависимостей.

Этап 1: разбор и проверка конфигурации. Построение графа и генерация
изображения будут добавлены на следующих этапах.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
SUPPORTED_IMAGE_FORMATS = {".png", ".svg"}


@dataclass(frozen=True)
class Configuration:
    package: str
    repository: str
    test_repo_mode: str
    version: str
    output_file: Path


class ConfigurationError(ValueError):
    """Ошибка согласованности параметров конфигурации."""


def non_empty_string(value: str) -> str:
    """Проверяет, что аргумент не пустой и не состоит из пробелов."""
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("значение не должно быть пустым")
    return normalized


def package_name(value: str) -> str:
    value = non_empty_string(value)
    if not PACKAGE_NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "недопустимое имя пакета: используйте 1–128 символов "
            "(латинские буквы, цифры, '.', '_' или '-'); первый символ — буква или цифра"
        )
    return value


def package_version(value: str) -> str:
    value = non_empty_string(value)
    if not SEMVER_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "версия должна соответствовать SemVer, например 1.2.3, 1.2.3-beta.1 или 1.2.3+build.7"
        )
    return value


def output_file(value: str) -> Path:
    value = non_empty_string(value)
    path = Path(value).expanduser()
    if not path.name or path.name in {".", ".."}:
        raise argparse.ArgumentTypeError("укажите имя выходного файла")
    if path.suffix.lower() not in SUPPORTED_IMAGE_FORMATS:
        extensions = ", ".join(sorted(SUPPORTED_IMAGE_FORMATS))
        raise argparse.ArgumentTypeError(
            f"неподдерживаемый формат изображения; допустимые расширения: {extensions}"
        )
    if path.parent.exists() and not path.parent.is_dir():
        raise argparse.ArgumentTypeError("родительский путь выходного файла не является каталогом")
    if not path.parent.exists():
        raise argparse.ArgumentTypeError("каталог для выходного файла не существует")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="depgraph",
        description="Минимальный прототип CLI для настройки визуализатора графа зависимостей.",
        epilog=(
            "В режиме test параметр --repository должен указывать на существующий локальный файл. "
            "В режиме remote требуется URL репозитория с протоколом http или https."
        ),
    )
    parser.add_argument(
        "--package",
        required=True,
        type=package_name,
        metavar="NAME",
        help="имя анализируемого пакета (1–128 символов)",
    )
    parser.add_argument(
        "--repository",
        required=True,
        type=non_empty_string,
        metavar="URL_OR_PATH",
        help="URL репозитория или путь к файлу тестового репозитория",
    )
    parser.add_argument(
        "--test-repo-mode",
        choices=("on", "off"),
        default="off",
        metavar="{on,off}",
        help="режим тестового репозитория: on — локальный файл, off — URL (по умолчанию: off)",
    )
    parser.add_argument(
        "--version",
        required=True,
        type=package_version,
        metavar="SEMVER",
        help="версия пакета в формате SemVer",
    )
    parser.add_argument(
        "--output",
        default=Path("dependency-graph.png"),
        type=output_file,
        metavar="FILE",
        help="имя файла изображения графа (.png или .svg; по умолчанию: dependency-graph.png)",
    )
    return parser


def create_configuration(args: argparse.Namespace) -> Configuration:
    """Проверяет правила, зависящие сразу от нескольких параметров."""
    repository = args.repository

    if args.test_repo_mode == "on":
        parsed_url = urlparse(repository)
        if parsed_url.scheme or parsed_url.netloc:
            raise ConfigurationError(
                "при --test-repo-mode on параметр --repository должен быть локальным путём, а не URL"
            )
        repo_path = Path(repository).expanduser()
        if not repo_path.exists():
            raise ConfigurationError(f"файл тестового репозитория не найден: {repo_path}")
        if not repo_path.is_file():
            raise ConfigurationError(f"путь тестового репозитория не является файлом: {repo_path}")
        repository = str(repo_path.resolve())
    else:
        parsed_url = urlparse(repository)
        invalid_url = (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or any(character.isspace() for character in repository)
        )
        try:
            # Доступ к .port также проверяет, что номер порта корректен.
            _ = parsed_url.port
        except ValueError:
            invalid_url = True
        if invalid_url:
            raise ConfigurationError(
                "при --test-repo-mode off параметр --repository должен быть корректным URL с http или https"
            )

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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = create_configuration(args)
    except ConfigurationError as exc:
        parser.error(str(exc))
    print_configuration(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
