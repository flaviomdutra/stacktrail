"""Conservative architecture extraction. Package presence is only a library dependency."""

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from stacktrail.models import Entity, Extraction, Relation, SourceFile, digest

URL = re.compile(r"\b(?:https?|postgres(?:ql)?|redis|amqps?|mongodb(?:\+srv)?)://[^\s\"'`<>)}\]]+")
RESOURCE_TYPES = {
    "AWS::S3::Bucket": "bucket",
    "AWS::SQS::Queue": "queue",
    "AWS::SNS::Topic": "topic",
    "AWS::DynamoDB::Table": "database",
    "AWS::RDS::DBInstance": "database",
    "AWS::RDS::DBCluster": "database",
    "AWS::Serverless::Function": "function",
    "AWS::Lambda::Function": "function",
    "AWS::Serverless::Api": "api",
    "AWS::ApiGateway::RestApi": "api",
    "AWS::Serverless::HttpApi": "api",
}


class TemplateLoader(yaml.SafeLoader):
    """Keep CloudFormation intrinsic references as data, never execute constructors."""


def intrinsic(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag: value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def component(source: SourceFile) -> Entity:
    return Entity(
        digest("component", source.repository), source.component, "component", source.repository
    )


def line_of(content: str, needle: str) -> int:
    return next((i for i, line in enumerate(content.splitlines(), 1) if needle in line), 1)


def extract(source: SourceFile) -> Extraction:
    result = Extraction()
    owner = result.add(component(source))
    path = Path(source.path)
    lines = source.content.splitlines()

    def link(
        target: Entity,
        kind: str,
        needle: str,
        origin: Entity = owner,
        certainty: str = "source-backed",
        line_hint: int | None = None,
    ) -> None:
        result.add(target)
        line = line_hint or line_of(source.content, needle)
        excerpt = lines[line - 1][:500] if lines else ""
        relation = Relation(origin.id, target.id, kind, line, excerpt, certainty)
        if relation not in result.relations:
            result.relations.append(relation)

    if path.name == "package.json":
        data = json.loads(source.content)
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            for name in data.get(section) or {}:
                target = Entity(digest("npm", name), name, "library", "npm")
                link(
                    target,
                    "uses-library" if section != "devDependencies" else "uses-dev-library",
                    name,
                )

    if path.name == "pyproject.toml":
        data = tomllib.loads(source.content)
        for dependency in data.get("project", {}).get("dependencies", []):
            name = re.split(r"[\[<>=!~; @]", dependency)[0]
            link(Entity(digest("pypi", name), name, "library", "pypi"), "uses-library", name)

    if path.suffix in {".yaml", ".yml"}:
        data = yaml.load(source.content, Loader=TemplateLoader) or {}
        if not isinstance(data, dict):
            return result
        locations = yaml_locations(source.content)

        def location(parts: tuple) -> int:
            while parts not in locations and parts:
                parts = parts[:-1]
            return locations.get(parts, 1)

        services = data.get("services", {})
        if isinstance(services, dict):
            environment = f"compose:{path.relative_to(Path(source.repository))}"
            service_entities = {}
            for name, settings in services.items():
                if not isinstance(settings, dict):
                    continue
                image = str(settings.get("image", ""))
                kind = (
                    "database"
                    if re.search(r"(?:postgres|mysql|mariadb|mongo|neo4j)", image)
                    else (
                        "queue"
                        if "rabbitmq" in image
                        else "cache"
                        if "redis" in image
                        else "service"
                    )
                )
                entity = Entity(
                    digest(source.repository, environment, name),
                    name,
                    kind,
                    source.repository,
                    environment,
                    image.split(":")[0].split("/")[-1],
                )
                service_entities[name] = result.add(entity)
                link(entity, "declares", f"{name}:", line_hint=location(("services", name)))
            for name, settings in services.items():
                if name not in service_entities:
                    continue
                dependencies = settings.get("depends_on", []) or []
                for index, dependency in enumerate(dependencies):
                    if dependency in service_entities:
                        link(
                            service_entities[dependency],
                            "depends-on",
                            str(dependency),
                            service_entities[name],
                            line_hint=location(
                                (
                                    "services",
                                    name,
                                    "depends_on",
                                    dependency if isinstance(dependencies, dict) else index,
                                )
                            ),
                        )
                for parts, value in scalar_values(settings.get("environment", {})):
                    for match in URL.finditer(str(value)):
                        hostname = urlsplit(match[0]).hostname
                        if hostname in service_entities:
                            link(
                                service_entities[hostname],
                                "connects-to",
                                match[0],
                                service_entities[name],
                                line_hint=location(("services", name, "environment", *parts)),
                            )
        resources = data.get("Resources", {})
        if isinstance(resources, dict):
            entities = {}
            environment = f"template:{path.relative_to(Path(source.repository))}"
            for name, settings in resources.items():
                if not isinstance(settings, dict):
                    continue
                resource_type = settings.get("Type", "")
                if resource_type in RESOURCE_TYPES:
                    entity = Entity(
                        digest(source.repository, environment, name),
                        name,
                        RESOURCE_TYPES[resource_type],
                        source.repository,
                        environment,
                    )
                    entities[name] = result.add(entity)
                    link(entity, "declares", f"{name}:", line_hint=location(("Resources", name)))
            for name, entity in entities.items():
                for dependency, parts in reference_paths(resources[name]):
                    if dependency in entities and dependency != name:
                        link(
                            entities[dependency],
                            "references",
                            dependency,
                            entity,
                            line_hint=location(("Resources", name, *parts)),
                        )

    # Only executable source/config is explicit connection evidence. Prose is inferred.
    certainty = "inferred" if path.suffix in {".md", ".rst"} else "source-backed"
    for number, line in enumerate(lines, 1):
        # Documentation and package metadata URLs are often citations, not runtime endpoints.
        # Their text remains searchable; model enrichment may propose explicit dependencies.
        if path.suffix in {".md", ".rst"} or path.name in {"package.json", "pyproject.toml"}:
            continue
        for match in URL.finditer(line):
            value = match[0].rstrip(".,;:")
            if "[REDACTED]@" in value:
                value = value.replace("[REDACTED]@", "")
            try:
                parsed = urlsplit(value)
                if not parsed.hostname or any(x in value for x in ("${", "{{", "{", "\\")):
                    continue
                host = parsed.hostname.lower()
                if host in {"www.w3.org", "schemas.xmlsoap.org", "json-schema.org"}:
                    continue
                port = f":{parsed.port}" if parsed.port else ""
                endpoint = urlunsplit((parsed.scheme, host + port, parsed.path.rstrip("/"), "", ""))
            except ValueError:
                continue
            scheme = parsed.scheme
            kind = (
                "database"
                if scheme.startswith(("postgres", "mongo"))
                else (
                    "cache"
                    if scheme == "redis"
                    else "queue"
                    if scheme.startswith("amqp")
                    else "external-api"
                )
            )
            # Endpoint identity is scoped to a repository: localhost and aliases are ambiguous.
            target = result.add(
                Entity(
                    digest(source.repository, "endpoint", endpoint),
                    endpoint,
                    kind,
                    source.repository,
                    technology=scheme,
                )
            )
            relation = Relation(owner.id, target.id, "connects-to", number, line[:500], certainty)
            if relation not in result.relations:
                result.relations.append(relation)

    # A literal AWS resource identifier is stronger than a library import.
    for number, line in enumerate(lines, 1):
        for match in re.finditer(r"\barn:(?:aws|aws-cn|aws-us-gov):[a-z0-9-]+:[^\s\"'`<>]*", line):
            arn = match[0].rstrip("),.;}")
            if any(x in arn for x in ("${", "{{", "{")):
                continue
            service = arn.split(":")[2]
            kind = {
                "s3": "bucket",
                "sqs": "queue",
                "sns": "topic",
                "lambda": "function",
                "dynamodb": "database",
            }.get(service, "resource")
            target = result.add(Entity(digest("arn", arn), arn, kind, "aws"))
            result.relations.append(
                Relation(owner.id, target.id, "references", number, line[:500], certainty)
            )
    return result


def reference_paths(value, path: tuple = ()) -> set[tuple[str, tuple]]:
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = (*path, key)
            if key in {"Ref", "GetAtt", "Fn::GetAtt"}:
                if isinstance(child, str):
                    found.add((child.split(".")[0], child_path))
                elif isinstance(child, list) and child:
                    found.add((str(child[0]), (*child_path, 0)))
            if key in {"Sub", "Fn::Sub"}:
                found.update(
                    (name, child_path)
                    for name in re.findall(r"\$\{([A-Za-z0-9]+)(?:\.[^}]+)?\}", str(child))
                )
            if key == "DependsOn":
                if isinstance(child, str):
                    found.add((child, child_path))
                elif isinstance(child, list):
                    found.update((str(name), (*child_path, i)) for i, name in enumerate(child))
            found.update(reference_paths(child, child_path))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.update(reference_paths(child, (*path, i)))
    return found


def scalar_values(value, path: tuple = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from scalar_values(child, (*path, key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from scalar_values(child, (*path, i))
    else:
        yield path, value


def yaml_locations(content: str) -> dict[tuple, int]:
    locations = {}

    def visit(node, path=()):
        locations.setdefault(path, node.start_mark.line + 1)
        if isinstance(node, yaml.MappingNode):
            for key, child in node.value:
                child_path = (*path, key.value)
                locations[child_path] = key.start_mark.line + 1
                visit(child, child_path)
        elif isinstance(node, yaml.SequenceNode):
            for i, child in enumerate(node.value):
                visit(child, (*path, i))

    root = yaml.compose(content, Loader=TemplateLoader)
    if root:
        visit(root)
    return locations
