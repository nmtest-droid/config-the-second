"""NuGet V3 metadata retrieval and dependency version resolution."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as e_t
from dataclasses import dataclass
from typing import Any

from .graph import build_dependency_graph
from .models import (
    Configuration,
    Dependency,
    DependencyGraph,
    NuGetRepositoryError,
    PackageNode,
)

NU_GET_PACKAGE_BASE_ADDRESS = "PackageBaseAddress/3.0.0"
HTTP_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
USER_AGENT = "dependency-graph-cli/0.5"
HTTP_NOT_FOUND_STATUS = 404
BYTES_PER_MEGABYTE = 1024 * 1024
NUGET_VERSION_RE = re.compile(
    r"^(?P<core>\d+(?:\.\d+){1,3})(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


def normalize_nuget_version(version: str) -> str:
    """Нормализует поддерживаемую CLI-версию для URL NuGet flat container.

    NuGet использует в URL нижний регистр и не включает SemVer build metadata.
    CLI уже проверил формат MAJOR.MINOR.PATCH, поэтому дополнительная
    нормализация числовых компонентов не требуется.
    """
    return version.split("+", 1)[0].lower()


def _read_resource(uri: str, *, timeout: int = HTTP_TIMEOUT_SECONDS) -> bytes:
    """Download a bounded HTTP or local file resource."""
    request = urllib.request.Request(
        uri,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/xml, */*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_NOT_FOUND_STATUS:
            raise NuGetRepositoryError(
                f"ресурс не найден (HTTP {HTTP_NOT_FOUND_STATUS}): {uri}"
            ) from exc
        raise NuGetRepositoryError(
            f"ошибка HTTP {exc.code} при запросе {uri}"
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise NuGetRepositoryError(
            f"не удалось подключиться к {uri}: {reason}"
        ) from exc
    except (TimeoutError, OSError, ValueError) as exc:
        raise NuGetRepositoryError(
            f"ошибка чтения ресурса {uri}: {exc}"
        ) from exc

    if len(payload) > MAX_RESPONSE_BYTES:
        raise NuGetRepositoryError(
            "размер ответа превышает допустимые "
            f"{MAX_RESPONSE_BYTES // BYTES_PER_MEGABYTE} МБ: {uri}"
        )
    return payload


def _read_json(uri: str) -> dict[str, Any]:
    """Read a UTF-8 JSON object from the repository resource."""
    payload = _read_resource(uri)
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NuGetRepositoryError(
            f"репозиторий вернул некорректный JSON: {uri}"
        ) from exc
    if not isinstance(document, dict):
        raise NuGetRepositoryError(
            f"корень Service Index должен быть JSON-объектом: {uri}"
        )
    return document


def _resource_types(resource: dict[str, Any]) -> list[Any]:
    """Return the NuGet resource type as a normalized list."""
    resource_type = resource.get("@type")
    return resource_type if isinstance(resource_type, list) else [resource_type]


def _validated_base_address(
    service_index_uri: str,
    resource: dict[str, Any],
) -> str:
    """Validate and resolve a PackageBaseAddress resource URL."""
    resource_id = resource.get("@id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise NuGetRepositoryError(
            "ресурс PackageBaseAddress не содержит корректный @id"
        )

    base_address = urllib.parse.urljoin(service_index_uri, resource_id)
    parsed = urllib.parse.urlparse(base_address)
    index_scheme = urllib.parse.urlparse(service_index_uri).scheme
    allowed_schemes = {"file"} if index_scheme == "file" else {"http", "https"}
    invalid_address = parsed.scheme not in allowed_schemes
    invalid_address = invalid_address or (
        not parsed.netloc and parsed.scheme != "file"
    )
    if invalid_address:
        raise NuGetRepositoryError(
            "ресурс PackageBaseAddress содержит недопустимый адрес"
        )
    return base_address.rstrip("/") + "/"


def discover_package_base_address(service_index_uri: str) -> str:
    """Find the PackageBaseAddress resource in a NuGet V3 Service Index."""
    service_index = _read_json(service_index_uri)
    resources = service_index.get("resources")
    if not isinstance(resources, list):
        raise NuGetRepositoryError(
            "в NuGet Service Index отсутствует список resources"
        )

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if NU_GET_PACKAGE_BASE_ADDRESS in _resource_types(resource):
            return _validated_base_address(service_index_uri, resource)

    message = f"в Service Index не найден ресурс {NU_GET_PACKAGE_BASE_ADDRESS}"
    raise NuGetRepositoryError(message)


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


def _child_text(parent: e_t.Element, child_name: str) -> str | None:
    """Return trimmed text of the first matching XML child."""
    for child in parent:
        if _local_name(child.tag) == child_name:
            return (child.text or "").strip()
    return None


def _parse_dependency(
    element: e_t.Element,
    target_framework: str | None,
) -> Dependency:
    """Convert one nuspec dependency element into a domain model."""
    package_id = (element.attrib.get("id") or "").strip()
    if not package_id:
        raise NuGetRepositoryError(
            "в .nuspec обнаружена зависимость без атрибута id"
        )
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


def _metadata_element(root: e_t.Element) -> e_t.Element:
    """Return the metadata element from a nuspec package document."""
    metadata = next(
        (child for child in root if _local_name(child.tag) == "metadata"),
        None,
    )
    if metadata is None:
        raise NuGetRepositoryError("в .nuspec отсутствует элемент metadata")
    return metadata


def _validate_nuspec_identity(
    metadata: e_t.Element,
    expected_package: str,
    expected_version: str,
) -> None:
    """Reject a nuspec whose package ID or version differs from the request."""
    actual_package = _child_text(metadata, "id")
    actual_version = _child_text(metadata, "version")
    package_matches = bool(actual_package) and (
        actual_package.casefold() == expected_package.casefold()
    )
    if not package_matches:
        raise NuGetRepositoryError(
            f"идентификатор пакета в .nuspec не совпадает с запросом: "
            f"ожидался {expected_package!r}, получен {actual_package!r}"
        )
    version_matches = bool(actual_version) and (
        normalize_nuget_version(actual_version)
        == normalize_nuget_version(expected_version)
    )
    if not version_matches:
        raise NuGetRepositoryError(
            f"версия пакета в .nuspec не совпадает с запросом: "
            f"ожидалась {expected_version!r}, получена {actual_version!r}"
        )


def _iter_dependency_elements(
    dependencies_element: e_t.Element,
) -> Any:
    """Yield dependency XML elements paired with their target frameworks."""
    for child in dependencies_element:
        child_type = _local_name(child.tag)
        if child_type == "dependency":
            yield child, None
        elif child_type == "group":
            framework = child.attrib.get("targetFramework") or None
            for dependency in child:
                if _local_name(dependency.tag) == "dependency":
                    yield dependency, framework


def _dependency_key(
    dependency: Dependency,
) -> tuple[str, str, str | None, str | None, str | None]:
    """Build a stable identity tuple for de-duplicating nuspec entries."""
    return (
        dependency.package.casefold(),
        dependency.version_range,
        dependency.target_framework,
        dependency.include,
        dependency.exclude,
    )


def parse_nuspec(
    nuspec_payload: bytes,
    expected_package: str,
    expected_version: str,
) -> list[Dependency]:
    """Extract direct dependencies from a NuGet nuspec XML document."""
    try:
        root = e_t.fromstring(nuspec_payload)
    except e_t.ParseError as exc:
        raise NuGetRepositoryError(
            "получен некорректный XML-файл .nuspec"
        ) from exc

    metadata = _metadata_element(root)
    _validate_nuspec_identity(metadata, expected_package, expected_version)
    dependencies_element = next(
        (
            child
            for child in metadata
            if _local_name(child.tag) == "dependencies"
        ),
        None,
    )
    if dependencies_element is None:
        return []

    dependencies: list[Dependency] = []
    seen: set[tuple[str, str, str | None, str | None, str | None]] = set()
    for element, framework in _iter_dependency_elements(dependencies_element):
        dependency = _parse_dependency(element, framework)
        key = _dependency_key(dependency)
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
    return get_direct_dependencies_from_base_address(
        base_address,
        package,
        version,
    )


def get_direct_dependencies(config: Configuration) -> list[Dependency]:
    """Получает прямые зависимости из удалённого NuGet-репозитория.

    В режиме тестирования этапа 3 repository является описанием графа, а не
    NuGet Service Index; поэтому этот helper предназначен для удалённого режима.
    """
    if config.test_repo_mode == "on":
        raise NuGetRepositoryError(
            "в тестовом режиме --repository является JSON-описанием графа, "
            "а не NuGet Service Index"
        )
    return get_direct_dependencies_from_service_index(
        config.repository, config.package, config.version
    )


VersionSortKey = tuple[
    tuple[int, ...],
    int,
    tuple[tuple[int, int | str], ...],
]


def _nuget_version_sort_key(version: str) -> VersionSortKey | None:
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


@dataclass(frozen=True)
class VersionConstraint:
    """Parsed NuGet version constraint with optional inclusive bounds."""

    lower: VersionSortKey | None
    upper: VersionSortKey | None
    lower_inclusive: bool
    upper_inclusive: bool
    exact: VersionSortKey | None = None


def _parse_version_constraint(expression: str) -> VersionConstraint:
    """Parse exact, interval, minimum and open-ended NuGet ranges."""
    value = (expression or "*").strip()
    if value in {"", "*"}:
        return VersionConstraint(None, None, False, False)

    if value.startswith(("[", "(")) and value.endswith(("]", ")")):
        body = value[1:-1]
        if "," not in body:
            if value[0] != "[" or value[-1] != "]":
                raise NuGetRepositoryError(
                    f"некорректный диапазон версии NuGet: {expression}"
                )
            exact = _nuget_version_sort_key(body.strip())
            if exact is None:
                raise NuGetRepositoryError(
                    f"некорректная версия в диапазоне NuGet: {expression}"
                )
            return VersionConstraint(None, None, True, True, exact)
        lower_text, upper_text = body.split(",", 1)
        lower_text = lower_text.strip()
        upper_text = upper_text.strip()
        lower_inclusive = value[0] == "["
        upper_inclusive = value[-1] == "]"
    else:
        lower_text, upper_text = value, ""
        lower_inclusive, upper_inclusive = True, False

    lower = _parse_range_bound(lower_text, expression, "нижняя")
    upper = _parse_range_bound(upper_text, expression, "верхняя")
    return VersionConstraint(
        lower,
        upper,
        lower_inclusive,
        upper_inclusive,
    )


def _parse_range_bound(
    value: str,
    expression: str,
    bound_name: str,
) -> VersionSortKey | None:
    """Parse a version range bound or return None for an open bound."""
    if not value:
        return None
    parsed = _nuget_version_sort_key(value)
    if parsed is None:
        raise NuGetRepositoryError(
            f"некорректная {bound_name} граница диапазона: {expression}"
        )
    return parsed


def _within_lower_bound(
    candidate: VersionSortKey,
    constraint: VersionConstraint,
) -> bool:
    """Check the lower side of a parsed version constraint."""
    if constraint.lower is None:
        return True
    if candidate > constraint.lower:
        return True
    return candidate == constraint.lower and constraint.lower_inclusive


def _within_upper_bound(
    candidate: VersionSortKey,
    constraint: VersionConstraint,
) -> bool:
    """Check the upper side of a parsed version constraint."""
    if constraint.upper is None:
        return True
    if candidate < constraint.upper:
        return True
    return candidate == constraint.upper and constraint.upper_inclusive


def _version_satisfies(version: str, version_range: str) -> bool:
    """Return whether a NuGet version satisfies a supported version range."""
    candidate = _nuget_version_sort_key(version)
    if candidate is None:
        return False
    constraint = _parse_version_constraint(version_range)
    if constraint.exact is not None:
        return candidate == constraint.exact
    return _within_lower_bound(candidate, constraint) and _within_upper_bound(
        candidate,
        constraint,
    )


def _load_package_versions(
    base_address: str,
    package_key: str,
    versions_cache: dict[str, list[str]] | None,
) -> list[str]:
    """Read and cache the published version index for a package."""
    if versions_cache is not None and package_key in versions_cache:
        return versions_cache[package_key]
    package_path = f"{urllib.parse.quote(package_key, safe='.-_')}/index.json"
    versions_uri = urllib.parse.urljoin(base_address, package_path)
    document = _read_json(versions_uri)
    raw_versions = document.get("versions")
    valid_versions = isinstance(raw_versions, list)
    valid_versions = valid_versions and all(
        isinstance(item, str) for item in raw_versions
    )
    if not valid_versions:
        raise NuGetRepositoryError(
            f"индекс версий пакета {package_key} имеет некорректный формат"
        )
    versions = raw_versions
    if versions_cache is not None:
        versions_cache[package_key] = versions
    return versions


def _matching_package_versions(
    versions: list[str],
    version_range: str,
) -> list[str]:
    """Return valid published versions satisfying one NuGet version range."""
    matches = [
        version
        for version in versions
        if _version_satisfies(version, version_range)
    ]
    return [
        version
        for version in matches
        if _nuget_version_sort_key(version) is not None
    ]


def resolve_nuget_version(
    base_address: str,
    package: str,
    version_range: str,
    versions_cache: dict[str, list[str]] | None = None,
) -> str:
    """Select the highest published package version matching a range."""
    package_key = package.casefold()
    versions = _load_package_versions(
        base_address,
        package_key,
        versions_cache,
    )
    matching = _matching_package_versions(versions, version_range)
    if not matching:
        raise NuGetRepositoryError(
            f"для зависимости {package} нет версии, удовлетворяющей "
            f"диапазону {version_range or '*'}"
        )
    return max(matching, key=lambda item: _nuget_version_sort_key(item))


def build_nuget_dependency_graph(config: Configuration) -> DependencyGraph:
    """Получает транзитивное замыкание NuGet-зависимостей итеративным DFS."""
    service_index_uri = config.repository
    base_address = discover_package_base_address(service_index_uri)
    versions_cache: dict[str, list[str]] = {}
    direct_cache: dict[tuple[str, str], list[Dependency]] = {}

    def load_neighbors(node: PackageNode) -> list[PackageNode]:
        """Load and resolve all direct dependency nodes for a package."""
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
                base_address,
                dependency.package,
                dependency.version_range,
                versions_cache,
            )
            resolved.append(
                PackageNode(dependency.package.casefold(), selected_version)
            )
        return resolved

    root = PackageNode(config.package.casefold(), config.version)
    return build_dependency_graph(root, load_neighbors)
