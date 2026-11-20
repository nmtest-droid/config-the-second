"""CLI-прототип визуализатора графа зависимостей.

Этап 1: конфигурация и валидация параметров.
Этап 2: получение зависимостей NuGet V3.
Этап 3: построение транзитивного графа итеративным DFS и обнаружение циклов.
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
from typing import Any, Callable


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
USER_AGENT = "dependency-graph-cli/0.3"
TEST_PACKAGE_NAME_RE = re.compile(r"^[A-Z]+$")
NUGET_VERSION_RE = re.compile(
    r"^(?P<core>\d+(?:\.\d+){1,3})(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


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


class GraphRepositoryError(RuntimeError):
    """Ошибка чтения или проверки тестового описания графа."""


@dataclass(frozen=True)
class PackageNode:
    """Вершина графа; версия отсутствует только в тестовом графе этапа 3."""

    package: str
    version: str | None = None

    @property
    def label(self) -> str:
        return self.package if self.version is None else f"{self.package}@{self.version}"


@dataclass
class DependencyGraph:
    """Ориентированный граф достижимых зависимостей и найденные циклы."""

    adjacency: dict[PackageNode, list[PackageNode]]
    cycles: list[tuple[PackageNode, ...]]

    @property
    def edge_count(self) -> int:
        return sum(len(neighbors) for neighbors in self.adjacency.values())


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
        description="Построение транзитивного графа зависимостей NuGet-пакета.",
        epilog=(
            "При --test-repo-mode on параметр --repository должен указывать на JSON-файл графа "
            "с именами вершин из заглавных латинских букв. При --test-repo-mode off требуется "
            "URL NuGet V3 Service Index с протоколом http или https."
        ),
    )
    parser.add_argument(
        "--package", required=True, type=package_name, metavar="NAME",
        help="имя анализируемого NuGet-пакета",
    )
    parser.add_argument(
        "--repository", required=True, type=non_empty_string, metavar="URL_OR_PATH",
        help="URL NuGet V3 Service Index или путь к JSON-описанию графа в тестовом режиме",
    )
    parser.add_argument(
        "--test-repo-mode", choices=("on", "off"), default="off", metavar="{on,off}",
        help="режим тестирования: on — локальный JSON-граф, off — удалённый NuGet V3 (по умолчанию: off)",
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


def get_direct_dependencies_from_base_address(
    base_address: str, package: str, version: str
) -> list[Dependency]:
    """Получает зависимости одной точной версии через NuGet flat container."""
    nuspec_uri = build_nuspec_uri(base_address, package, version)
    nuspec_payload = _read_resource(nuspec_uri)
    return parse_nuspec(nuspec_payload, package, version)


def get_direct_dependencies_from_service_index(
    service_index_uri: str, package: str, version: str
) -> list[Dependency]:
    """Получает прямые зависимости из любого NuGet V3 Service Index URI."""
    base_address = discover_package_base_address(service_index_uri)
    return get_direct_dependencies_from_base_address(base_address, package, version)


def get_direct_dependencies(config: Configuration) -> list[Dependency]:
    """Получает прямые зависимости из удалённого NuGet-репозитория.

    В режиме тестирования этапа 3 repository является описанием графа, а не
    NuGet Service Index; поэтому этот helper предназначен для удалённого режима.
    """
    if config.test_repo_mode == "on":
        raise NuGetRepositoryError(
            "в тестовом режиме этапа 3 --repository является JSON-описанием графа, а не NuGet Service Index"
        )
    return get_direct_dependencies_from_service_index(
        config.repository, config.package, config.version
    )


def read_test_graph(repository_path: str | Path) -> dict[str, list[str]]:
    """Читает JSON-список смежности; все имена вершин — A..Z без цифр."""
    path = Path(repository_path)
    try:
        payload = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise GraphRepositoryError(f"не удалось прочитать файл графа {path}: {exc}") from exc
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GraphRepositoryError(
            f"файл тестового графа содержит некорректный JSON (строка {exc.lineno}, столбец {exc.colno})"
        ) from exc
    if not isinstance(document, dict) or not document:
        raise GraphRepositoryError("описание графа должно быть непустым JSON-объектом вида {\"A\": [\"B\"]}")

    graph: dict[str, list[str]] = {}
    for package, dependencies in document.items():
        if not isinstance(package, str) or not TEST_PACKAGE_NAME_RE.fullmatch(package):
            raise GraphRepositoryError(
                f"недопустимое имя пакета {package!r}: используйте только заглавные латинские буквы A-Z"
            )
        if not isinstance(dependencies, list):
            raise GraphRepositoryError(f"список зависимостей пакета {package} должен быть массивом")
        normalized: list[str] = []
        for dependency in dependencies:
            if not isinstance(dependency, str) or not TEST_PACKAGE_NAME_RE.fullmatch(dependency):
                raise GraphRepositoryError(
                    f"недопустимое имя зависимости {dependency!r} пакета {package}: "
                    "используйте только заглавные латинские буквы A-Z"
                )
            if dependency not in normalized:
                normalized.append(dependency)
        graph[package] = normalized

    declared = set(graph)
    for package, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in declared:
                raise GraphRepositoryError(
                    f"пакет {dependency}, указанный как зависимость {package}, отсутствует в описании графа"
                )
    return graph


def build_dependency_graph(
    root: PackageNode,
    dependency_loader: Callable[[PackageNode], list[PackageNode]],
) -> DependencyGraph:
    """Строит достижимую часть графа итеративным DFS с трёхцветной разметкой.

    Белая вершина ещё не посещалась, серая находится в текущем стеке DFS,
    чёрная полностью обработана. Ребро в серую вершину является обратным
    ребром и фиксирует цикл. Явный стек кадров исключает рекурсию Python.
    """
    adjacency: dict[PackageNode, list[PackageNode]] = {root: []}
    colors: dict[PackageNode, str] = {root: "gray"}
    active: list[PackageNode] = [root]
    active_positions: dict[PackageNode, int] = {root: 0}
    loaded: set[PackageNode] = set()
    cycles: list[tuple[PackageNode, ...]] = []
    # Каждый кадр хранит текущую вершину и индекс следующего ребра.
    frames: list[tuple[PackageNode, int]] = [(root, 0)]

    while frames:
        node, next_index = frames[-1]
        if node not in loaded:
            neighbors = dependency_loader(node)
            if not isinstance(neighbors, list):
                neighbors = list(neighbors)
            # Удаляем повторяющиеся рёбра, сохраняя порядок из репозитория.
            unique_neighbors: list[PackageNode] = []
            seen_neighbors: set[PackageNode] = set()
            for neighbor in neighbors:
                if not isinstance(neighbor, PackageNode):
                    raise TypeError("dependency_loader должен возвращать список PackageNode")
                if neighbor not in seen_neighbors:
                    unique_neighbors.append(neighbor)
                    seen_neighbors.add(neighbor)
            adjacency[node] = unique_neighbors
            loaded.add(node)

        neighbors = adjacency[node]
        if next_index >= len(neighbors):
            frames.pop()
            colors[node] = "black"
            active_positions.pop(node, None)
            active.pop()
            continue

        neighbor = neighbors[next_index]
        frames[-1] = (node, next_index + 1)
        adjacency.setdefault(neighbor, [])
        color = colors.get(neighbor, "white")

        if color == "white":
            colors[neighbor] = "gray"
            active_positions[neighbor] = len(active)
            active.append(neighbor)
            frames.append((neighbor, 0))
        elif color == "gray":
            cycle_start = active_positions[neighbor]
            cycle = tuple(active[cycle_start:] + [neighbor])
            # Каждое обратное ребро даёт один фундаментальный цикл DFS.
            cycles.append(cycle)
        # Чёрная вершина уже обработана; ребро остаётся в adjacency.

    return DependencyGraph(adjacency=adjacency, cycles=cycles)


def build_test_dependency_graph(config: Configuration) -> DependencyGraph:
    """Строит граф из локального JSON-файла в режиме тестирования."""
    repository_graph = read_test_graph(config.repository)
    if not TEST_PACKAGE_NAME_RE.fullmatch(config.package):
        raise GraphRepositoryError(
            "в тестовом режиме --package должен содержать только заглавные латинские буквы A-Z"
        )
    if config.package not in repository_graph:
        raise GraphRepositoryError(
            f"пакет {config.package} не найден в тестовом описании графа"
        )

    def load_neighbors(node: PackageNode) -> list[PackageNode]:
        return [PackageNode(package) for package in repository_graph[node.package]]

    return build_dependency_graph(PackageNode(config.package), load_neighbors)


def _nuget_version_sort_key(version: str) -> tuple[tuple[int, ...], int, tuple[tuple[int, int | str], ...]] | None:
    """Возвращает ключ сортировки NuGet/SemVer версии без внешних библиотек."""
    match = NUGET_VERSION_RE.fullmatch(version.strip())
    if not match:
        return None
    core = tuple(int(part) for part in match.group("core").split("."))
    core = core + (0,) * (4 - len(core))
    prerelease = match.group("pre")
    if prerelease is None:
        return core, 1, tuple()
    identifiers: list[tuple[int, int | str]] = []
    for identifier in prerelease.split("."):
        if not identifier:
            return None
        if identifier.isdigit():
            identifiers.append((0, int(identifier)))
        else:
            identifiers.append((1, identifier.casefold()))
    return core, 0, tuple(identifiers)


def _version_satisfies(version: str, version_range: str) -> bool:
    """Проверяет NuGet version range: exact, interval, open-ended или minimum."""
    candidate = _nuget_version_sort_key(version)
    if candidate is None:
        return False
    expression = (version_range or "*").strip()
    if expression in {"", "*"}:
        return True

    if expression[0:1] in {"[", "("} and expression[-1:] in {"]", ")"
    }:
        body = expression[1:-1]
        if "," not in body:
            if expression[0] != "[" or expression[-1] != "]":
                raise NuGetRepositoryError(f"некорректный диапазон версии NuGet: {version_range}")
            exact = _nuget_version_sort_key(body.strip())
            if exact is None:
                raise NuGetRepositoryError(f"некорректная версия в диапазоне NuGet: {version_range}")
            return candidate == exact
        lower_text, upper_text = body.split(",", 1)
        lower_text, upper_text = lower_text.strip(), upper_text.strip()
        lower_inclusive, upper_inclusive = expression[0] == "[", expression[-1] == "]"
    else:
        # Простая версия в .nuspec означает минимальную включительную версию.
        lower_text, upper_text = expression, ""
        lower_inclusive, upper_inclusive = True, False

    if lower_text:
        lower = _nuget_version_sort_key(lower_text)
        if lower is None:
            raise NuGetRepositoryError(f"некорректная нижняя граница диапазона: {version_range}")
        if candidate < lower or (candidate == lower and not lower_inclusive):
            return False
    if upper_text:
        upper = _nuget_version_sort_key(upper_text)
        if upper is None:
            raise NuGetRepositoryError(f"некорректная верхняя граница диапазона: {version_range}")
        if candidate > upper or (candidate == upper and not upper_inclusive):
            return False
    return True


def resolve_nuget_version(
    base_address: str,
    package: str,
    version_range: str,
    versions_cache: dict[str, list[str]] | None = None,
) -> str:
    """Выбирает максимальную опубликованную версию, удовлетворяющую диапазону."""
    package_key = package.casefold()
    if versions_cache is not None and package_key in versions_cache:
        versions = versions_cache[package_key]
    else:
        encoded_package = urllib.parse.quote(package_key, safe=".-_")
        versions_uri = urllib.parse.urljoin(base_address, f"{encoded_package}/index.json")
        document = _read_json(versions_uri)
        raw_versions = document.get("versions")
        if not isinstance(raw_versions, list) or not all(isinstance(item, str) for item in raw_versions):
            raise NuGetRepositoryError(f"индекс версий пакета {package} имеет некорректный формат")
        versions = raw_versions
        if versions_cache is not None:
            versions_cache[package_key] = versions

    matching = [version for version in versions if _version_satisfies(version, version_range)]
    matching = [version for version in matching if _nuget_version_sort_key(version) is not None]
    if not matching:
        raise NuGetRepositoryError(
            f"для зависимости {package} нет опубликованной версии, удовлетворяющей диапазону {version_range or '*'}"
        )
    return max(matching, key=lambda item: _nuget_version_sort_key(item))


def build_nuget_dependency_graph(config: Configuration) -> DependencyGraph:
    """Получает транзитивное замыкание NuGet-зависимостей итеративным DFS."""
    service_index_uri = config.repository
    base_address = discover_package_base_address(service_index_uri)
    versions_cache: dict[str, list[str]] = {}
    direct_cache: dict[tuple[str, str], list[Dependency]] = {}

    def load_neighbors(node: PackageNode) -> list[PackageNode]:
        package_key = node.package.casefold()
        version = node.version or ""
        cache_key = (package_key, version.casefold())
        if cache_key not in direct_cache:
            direct_cache[cache_key] = get_direct_dependencies_from_base_address(
                base_address, node.package, version
            )
        resolved: list[PackageNode] = []
        for dependency in direct_cache[cache_key]:
            selected_version = resolve_nuget_version(
                base_address, dependency.package, dependency.version_range, versions_cache
            )
            resolved.append(PackageNode(dependency.package.casefold(), selected_version))
        return resolved

    root = PackageNode(config.package.casefold(), config.version)
    return build_dependency_graph(root, load_neighbors)


def print_dependency_graph(graph: DependencyGraph) -> None:
    """Печатает список смежности, размеры графа и информацию о циклах."""
    print(f"graph_nodes_count={len(graph.adjacency)}")
    print(f"graph_edges_count={graph.edge_count}")
    print("dependency_graph:")
    for node, neighbors in graph.adjacency.items():
        targets = ", ".join(item.label for item in neighbors) if neighbors else "none"
        print(f"- {node.label} -> {targets}")

    print(f"cycles_count={len(graph.cycles)}")
    if graph.cycles:
        print("cycles:")
        for cycle in graph.cycles:
            print("- " + " -> ".join(node.label for node in cycle))
    else:
        print("cycles=none")


def main(argv: list[str] | None = None) -> int:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
