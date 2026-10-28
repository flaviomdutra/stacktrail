"""Deterministic Mermaid output; architecture data stays outside Git by default."""

import html
import re


def label(value: str) -> str:
    return html.escape(re.sub(r"[\r\n\t]", " ", value), quote=True).replace("`", "&#96;")


def mermaid(edges: list[dict], nodes: list[dict] | None = None) -> str:
    entities = {node["id"]: node for node in nodes or [] if node["kind"] != "library"}
    relations = set()
    for edge in edges:
        if edge["target"]["kind"] == "library":
            continue
        for side in ("source", "target"):
            entities[edge[side]["id"]] = edge[side]
        relations.add(
            (
                edge["source"]["id"],
                edge["target"]["id"],
                edge["evidence"]["kind"],
                edge["evidence"]["certainty"],
            )
        )
    nodes = {key: f"n{i}" for i, key in enumerate(sorted(entities))}
    lines = [
        "# Architecture",
        "",
        "Generated from the last synchronized sources. "
        "Dashed arrows indicate inferred relationships.",
        "",
        "```mermaid",
        "flowchart LR",
    ]
    for key in sorted(entities):
        entity = entities[key]
        lines.append(f'    {nodes[key]}["{label(entity["name"])} ({label(entity["kind"])})"]')
    for source, target, kind, certainty in sorted(relations):
        arrow = "-.->" if certainty == "inferred" else "-->"
        lines.append(f'    {nodes[source]} {arrow}|"{label(kind)}"| {nodes[target]}')
    lines += ["```", "", "## Evidence", ""]
    evidence_rows = set()
    for edge in edges:
        if edge["target"]["kind"] == "library":
            continue
        evidence = edge["evidence"]
        row = (
            edge["source"]["name"],
            edge["target"]["name"],
            evidence["file"],
            evidence["line"],
            evidence["certainty"],
            evidence["content_hash"],
        )
        evidence_rows.add(row)
    for source, target, path, number, certainty, version in sorted(evidence_rows):
        lines.append(
            f"- {label(source)} → {label(target)}: {label(path)}:{number} "
            f"({certainty}; content {version[:12]})"
        )
    if not entities:
        lines.append("No architecture relationships found for this selection.")
    return "\n".join(lines) + "\n"
