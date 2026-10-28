"""Conservative architectural projection, separate from the detailed evidence graph."""

from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from stacktrail.models import digest

LAYERS = {
    "component": "Applications",
    "service": "Execution",
    "function": "Execution",
    "api": "Ingress",
    "database": "Data and messaging",
    "cache": "Data and messaging",
    "queue": "Data and messaging",
    "topic": "Data and messaging",
    "bucket": "Storage",
    "external-api": "External integrations",
    "pipeline": "Operations",
}


def project(edges, nodes, view="solution"):
    if view not in {"solution", "infrastructure"}:
        raise ValueError("Choose solution or infrastructure.")
    selected = {}
    mapping = {}
    for node in nodes:
        kind = node["kind"]
        if kind not in LAYERS:
            continue
        name = node["name"]
        if kind == "external-api" and "://" in name:
            parsed = urlsplit(name)
            if not parsed.hostname or parsed.hostname in {"localhost", "127.0.0.1"}:
                continue
            name = parsed.hostname
        key = (
            node["id"]
            if kind != "external-api"
            else digest(node.get("repository", ""), node.get("environment", "unknown"), kind, name)
        )
        mapping[node["id"]] = key
        selected[key] = {**node, "id": key, "name": name, "layer": node.get("layer", LAYERS[kind])}
    projected = {}
    evidence = []
    for edge in edges:
        a, b = mapping.get(edge["source"]["id"]), mapping.get(edge["target"]["id"])
        if not a or not b or a == b:
            continue
        key = (a, b, edge["evidence"]["kind"])
        projected.setdefault(key, {**edge, "source": selected[a], "target": selected[b]})
        evidence.append(edge["evidence"])
    if view == "solution":
        # Hide deployment containers in the logical solution and carry their dependencies upward.
        hidden = {key for key, node in selected.items() if node["kind"] == "service"}
        parents = {}
        for edge in projected.values():
            if edge["target"]["id"] in hidden and edge["source"]["kind"] == "component":
                parents.setdefault(edge["target"]["id"], []).append(edge["source"])
        logical = {}
        for edge in projected.values():
            if edge["target"]["id"] in hidden:
                continue
            origins = (
                parents.get(edge["source"]["id"], [])
                if edge["source"]["id"] in hidden
                else [edge["source"]]
            )
            for origin in origins:
                logical[(origin["id"], edge["target"]["id"], edge["evidence"]["kind"])] = {
                    **edge,
                    "source": origin,
                }
        projected = logical
        selected = {key: node for key, node in selected.items() if key not in hidden}
    if view == "infrastructure":
        used = {e[side]["id"] for e in projected.values() for side in ("source", "target")}
        selected = {k: n for k, n in selected.items() if n["kind"] != "component" or k in used}
    return list(projected.values()), list(selected.values()), evidence


def diagram(edges, nodes, view="solution"):
    from stacktrail.export import label

    edges, nodes, evidence = project(edges, nodes, view)
    ids = {n["id"]: f"n{i}" for i, n in enumerate(sorted(nodes, key=lambda n: n["id"]))}
    names = Counter(n["name"] for n in nodes)
    lines = [
        f"# {view.title()} architecture",
        "",
        "Logical architecture reconstructed from synchronized sources; deployment state is unverified.",
        "",
        "```mermaid",
        "flowchart TB",
    ]
    for index, layer in enumerate(dict.fromkeys([*LAYERS.values(), *[n["layer"] for n in nodes]])):
        members = sorted((n for n in nodes if n["layer"] == layer), key=lambda n: n["name"])
        if not members:
            continue
        lines.append(f'  subgraph layer{index}["{layer}"]')
        for node in members:
            technology = node.get("technology") or node["kind"]
            detail = node.get("responsibility") or technology
            title = node["name"]
            if names[title] > 1:
                title += " / " + Path(node.get("repository", "unknown")).name
            lines.append(f'    {ids[node["id"]]}["{label(title)} — {label(detail)}"]')
        lines.append("  end")
        lines.append(f"classDef c{index} fill:#eef5ff,stroke:#52749c,color:#152b46")
        lines.append(f"class {','.join(ids[n['id']] for n in members)} c{index}")
    for edge in sorted(edges, key=lambda e: (e["source"]["id"], e["target"]["id"])):
        arrow = "-.->" if edge["evidence"]["certainty"] == "inferred" else "-->"
        lines.append(
            f'  {ids[edge["source"]["id"]]} {arrow}|"{label(edge["evidence"]["kind"])}"| {ids[edge["target"]["id"]]}'
        )
    lines += [
        "```",
        "",
        "## Coverage and limitations",
        "",
        "Only discovered architectural elements are shown. Unknown resource identities remain separate. Network boundaries, replica counts, and production deployment are not inferred from local configuration.",
        "",
        "## Supporting evidence",
        "",
    ]
    references = sorted({(e["file"], e["line"]) for e in evidence})
    lines.extend(f"- {label(path)}:{number}" for path, number in references)
    return "\n".join(lines) + "\n"
