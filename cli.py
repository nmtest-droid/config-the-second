"""CLI-прототип визуализатора графа зависимостей.

Этап 1: конфигурация и валидация параметров.
Этап 2: получение NuGet V3 .nuspec и вывод прямых зависимостей.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
SUPPORTED_IMAGE_FORMATS = {".png", ".svg"}
WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
NU_GET_PACKAGE_BASE_ADDRESS = "PackageBaseAddress/3.0.0"
HTTP_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
USER_AGENT = "dependency-graph-cli/0.2"


def is_url(value: str) -> bool:
    """Определяет URL, не принимая Windows-пути вида C:\\repo.json за URI."""
    if WINDOWS_DRIVE_PREFIX_RE.match(value):
        return False
    return bool(urllib.parse.urlparse(value).scheme)


@dataclass(frozen=True)
class Configuration:
    package: str
    repository: str
    test_repo_mode: str
    version: str
    output_file: Path


@dataclass(frozen=True)
class Dependency:
    """Прямая зависимость из NuGet .nuspec."""

    package: str
    version_range: str
    target_framework: str | None = None
    include: str | None = None
    exclude: str | None = None


class ConfigurationError(ValueError):
    """Ошибка согласованности параметров конфигурации."""


class NuGetRepositoryError(RuntimeError):
    """Ошибка получения или разбора данных NuGet-репозитория."""


def non_empty_string(value: str) -> str:
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
        description="Получение прямых зависимостей NuGet-пакета для визуализатора графа.",
        epilog=(
            "В режиме test параметр --repository должен указывать на локальный NuGet V3 Service Index. "
            "В режиме remote требуется URL NuGet V3 Service Index с протоколом http или https."
        ),
    )
    parser.add_argument(
        "--package", required=True, type=package_name, metavar="NAME",
        help="имя анализируемого NuGet-пакета",
    )
    parser.add_argument(
        "--repository", required=True, type=non_empty_string, metavar="URL_OR_PATH",
        help="URL NuGet V3 Service Index или путь к его локальной копии для тестирования",
    )
    parser.add_argument(
        "--test-repo-mode", choices=("on", "off"), default="off", metavar="{on,off}",
        help="режим локального тестового репозитория: on — файл, off — HTTP(S) URL (по умолчанию: off)",
    )
    parser.add_argument(
        "--version", required=True, type=package_version, metavar="SEMVER",
        help="точная версия NuGet-пакета в формате SemVer",
    )
    parser.add_argument(
        "--output", default=Path("dependency-graph.png"), type=output_file, metavar="FILE",
        help="имя файла будущего изображения (.png или .svg; по умолчанию: dependency-graph.png)",
    )
    return parser


def create_configuration(args: argparse.Namespace) -> Configuration:
    """Проверяет правила, зависящие сразу от нескольких параметров."""
    repository = args.repository

    if args.test_repo_mode == "on":
        if is_url(repository):
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
        parsed_url = urllib.parse.urlparse(repository)
        invalid_url = (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or any(character.isspace() for character in repository)
        )
        try:
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


def normalize_nuget_version(version: str) -> str:
    """Нормализует поддерживаемую CLI-версию для URL NuGet flat container.

    NuGet использует в URL нижний регистр и не включает SemVer build metadata.
    CLI уже проверил формат MAJOR.MINOR.PATCH, поэтому дополнительная
    нормализация числовых компонентов не требуется.
    """
    return version.split("+", 1)[0].lower()


def _read_resource(uri: str, *, timeout: int = HTTP_TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(
        uri,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, */*"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NuGetRepositoryError(f"ресурс не найден (HTTP 404): {uri}") from exc
        raise NuGetRepositoryError(f"ошибка HTTP {exc.code} при запросе {uri}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise NuGetRepositoryError(f"не удалось подключиться к {uri}: {reason}") from exc
    except (TimeoutError, OSError, ValueError) as exc:
        raise NuGetRepositoryError(f"ошибка чтения ресурса {uri}: {exc}") from exc

    if len(payload) > MAX_RESPONSE_BYTES:
        raise NuGetRepositoryError(
            f"размер ответа превышает допустимые {MAX_RESPONSE_BYTES // (1024 * 1024)} МБ: {uri}"
        )
    return payload


def _read_json(uri: str) -> dict[str, Any]:
    payload = _read_resource(uri)
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NuGetRepositoryError(f"репозиторий вернул некорректный JSON: {uri}") from exc
    if not isinstance(document, dict):
        raise NuGetRepositoryError(f"корень Service Index должен быть JSON-объектом: {uri}")
    return document


def discover_package_base_address(service_index_uri: str) -> str:
    """Находит PackageBaseAddress в NuGet V3 Service Index."""
    service_index = _read_json(service_index_uri)
    resources = service_index.get("resources")
    if not isinstance(resources, list):
        raise NuGetRepositoryError("в NuGet Service Index отсутствует список resources")

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("@type")
        resource_types = resource_type if isinstance(resource_type, list) else [resource_type]
        if NU_GET_PACKAGE_BASE_ADDRESS not in resource_types:
            continue
        resource_id = resource.get("@id")
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise NuGetRepositoryError("ресурс PackageBaseAddress не содержит корректный @id")
        base_address = urllib.parse.urljoin(service_index_uri, resource_id)
        parsed = urllib.parse.urlparse(base_address)
        index_scheme = urllib.parse.urlparse(service_index_uri).scheme
        allowed_schemes = {"file"} if index_scheme == "file" else {"http", "https"}
        if parsed.scheme not in allowed_schemes or (not parsed.netloc and parsed.scheme != "file"):
            raise NuGetRepositoryError("ресурс PackageBaseAddress содержит недопустимый адрес")
        return base_address if base_address.endswith("/") else base_address + "/"

    raise NuGetRepositoryError(
        f"в Service Index не найден ресурс {NU_GET_PACKAGE_BASE_ADDRESS}"
    )


def build_nuspec_uri(base_address: str, package: str, version: str) -> str:
    """Строит URL .nuspec по правилам NuGet PackageBaseAddress."""
    lower_id = package.lower()
    lower_version = normalize_nuget_version(version)
    escaped_id = urllib.parse.quote(lower_id, safe=".-_")
    escaped_version = urllib.parse.quote(lower_version, safe=".-_")
    relative_path = f"{escaped_id}/{escaped_version}/{escaped_id}.nuspec"
    return urllib.parse.urljoin(base_address, relative_path)


def _local_name(tag: str) -> str:
    """Возвращает локальную часть XML-тега независимо от namespace."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(parent: ET.Element, child_name: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == child_name:
            return (child.text or "").strip()
    return None


def _parse_dependency(element: ET.Element, target_framework: str | None) -> Dependency:
    package_id = (element.attrib.get("id") or "").strip()
    if not package_id:
        raise NuGetRepositoryError("в .nuspec обнаружена зависимость без атрибута id")
    version_range = (element.attrib.get("version") or "*").strip() or "*"
    include = (element.attrib.get("include") or "").strip() or None
    exclude = (element.attrib.get("exclude") or "").strip() or None
    return Dependency(
        package=package_id,
        version_range=version_range,
        target_framework=target_framework,
        include=include,
        exclude=exclude,
    )


def parse_nuspec(
    nuspec_payload: bytes,
    expected_package: str,
    expected_version: str,
) -> list[Dependency]:
    """Извлекает прямые зависимости из XML .nuspec, не обходя транзитивные."""
    try:
        root = ET.fromstring(nuspec_payload)
    except ET.ParseError as exc:
        raise NuGetRepositoryError("получен некорректный XML-файл .nuspec") from exc

    metadata = next((child for child in root if _local_name(child.tag) == "metadata"), None)
    if metadata is None:
        raise NuGetRepositoryError("в .nuspec отсутствует элемент metadata")

    actual_package = _child_text(metadata, "id")
    actual_version = _child_text(metadata, "version")
    if not actual_package or actual_package.casefold() != expected_package.casefold():
        raise NuGetRepositoryError(
            f"идентификатор пакета в .nuspec не совпадает с запросом: ожидался {expected_package!r}, "
            f"получен {actual_package!r}"
        )
    if not actual_version or normalize_nuget_version(actual_version) != normalize_nuget_version(expected_version):
        raise NuGetRepositoryError(
            f"версия пакета в .nuspec не совпадает с запросом: ожидалась {expected_version!r}, "
            f"получена {actual_version!r}"
        )

    dependencies_element = next(
        (child for child in metadata if _local_name(child.tag) == "dependencies"), None
    )
    if dependencies_element is None:
        return []

    dependencies: list[Dependency] = []
    seen: set[tuple[str, str, str | None, str | None, str | None]] = set()
    for child in dependencies_element:
        child_type = _local_name(child.tag)
        if child_type == "dependency":
            candidates = [(child, None)]
        elif child_type == "group":
            framework = child.attrib.get("targetFramework") or None
            candidates = [
                (grandchild, framework)
                for grandchild in child
                if _local_name(grandchild.tag) == "dependency"
            ]
        else:
            continue

        for dependency_element, framework in candidates:
            dependency = _parse_dependency(dependency_element, framework)
            key = (
                dependency.package.casefold(),
                dependency.version_range,
                dependency.target_framework,
                dependency.include,
                dependency.exclude,
            )
            if key not in seen:
                dependencies.append(dependency)
                seen.add(key)

    return dependencies


def get_direct_dependencies(config: Configuration) -> list[Dependency]:
    """Получает зависимости точной версии пакета через NuGet V3 API."""
    if config.test_repo_mode == "on":
        service_index_uri = Path(config.repository).resolve().as_uri()
    else:
        service_index_uri = config.repository

    base_address = discover_package_base_address(service_index_uri)
    nuspec_uri = build_nuspec_uri(base_address, config.package, config.version)
    nuspec_payload = _read_resource(nuspec_uri)
    return parse_nuspec(nuspec_payload, config.package, config.version)


def print_direct_dependencies(dependencies: list[Dependency]) -> None:
    """Печатает все найденные прямые зависимости и группы платформ."""
    print(f"direct_dependencies_count={len(dependencies)}")
    if not dependencies:
        print("direct_dependencies=none")
        return

    print("direct_dependencies:")
    for dependency in dependencies:
        fields = [
            f"package={dependency.package}",
            f"version={dependency.version_range}",
            f"target_framework={dependency.target_framework or 'any'}",
        ]
        if dependency.include:
            fields.append(f"include={dependency.include}")
        if dependency.exclude:
            fields.append(f"exclude={dependency.exclude}")
        print("- " + " ".join(fields))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = create_configuration(args)
    except ConfigurationError as exc:
        parser.error(str(exc))

    print_configuration(config)
    try:
        dependencies = get_direct_dependencies(config)
    except NuGetRepositoryError as exc:
        parser.error(f"ошибка получения зависимостей NuGet: {exc}")

    print_direct_dependencies(dependencies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
