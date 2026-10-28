"""Graph-guided retrieval and source references for terminal answers."""

import re

from stacktrail.config import Config
from stacktrail.graph import GraphStore
from stacktrail.llm import ModelProvider

STOPWORDS = {
    "who",
    "uses",
    "use",
    "can",
    "you",
    "tell",
    "me",
    "about",
    "de",
    "do",
    "da",
    "em",
    "os",
    "as",
    "um",
    "uma",
    "to",
    "of",
    "on",
    "in",
    "is",
    "which",
    "what",
    "where",
    "does",
    "the",
    "and",
    "are",
    "how",
    "this",
    "that",
    "with",
    "from",
    "services",
    "service",
    "depend",
    "depends",
    "dependencies",
    "components",
    "component",
    "have",
    "has",
    "using",
    "quais",
    "qual",
    "como",
    "onde",
    "que",
    "servicos",
    "serviços",
    "dependem",
    "usa",
}
ALIASES = {
    "s3": "bucket",
    "pg": "postgresql",
    "postgresql": "postgres",
    "postgres": "postgresql",
    "banco": "database",
    "notificações": "notifications",
    "notificacoes": "notifications",
    "fila": "queue",
}


def search_terms(question: str) -> str:
    tokens = [
        token.lower()
        for token in re.findall(r"[^\W_]+", question[:2000], flags=re.UNICODE)
        if len(token) >= 2 and token.lower() not in STOPWORDS
    ]
    tokens = list(dict.fromkeys(tokens))[:12]
    tokens += [ALIASES[t] for t in tokens if t in ALIASES]
    return " OR ".join(f'"{token}"' for token in tokens)


def source_snippet(content: str, terms: list[str], budget: int) -> tuple[str, int]:
    lines = content.splitlines()
    matches = [i for i, line in enumerate(lines) if any(term in line.lower() for term in terms)]
    selected = set()
    for index in matches[:8] or [0]:
        selected.update(range(max(0, index - 1), min(len(lines), index + 3)))
    output = []
    used = 0
    for index in sorted(selected):
        item = f"{index + 1}: {lines[index]}"
        if used + len(item) + 1 > budget:
            if not output:
                output.append(item[:budget])
            break
        output.append(item)
        used += len(item) + 1
    first_line = min(selected) + 1 if selected else 1
    return "\n".join(output), first_line


def answer_question(config: Config, store: GraphStore, model: ModelProvider, question: str) -> str:
    search = search_terms(question)
    if not search:
        return "Please name a component, resource, or technology to search for."
    sources = store.retrieve(search)
    if not sources:
        return "Insufficient evidence: no matching synchronized sources were found. Run sync or refine the question."
    terms = re.findall(r'"([^\"]+)"', search)
    edges = store.neighborhood([source["id"] for source in sources], terms)

    def matches(entity):
        description = " ".join(
            [entity["name"], entity["kind"], entity.get("technology", "")]
        ).lower()
        return any(term in description for term in terms)

    executable = {"component", "service", "function", "api"}
    edges = [
        edge
        for edge in edges
        if matches(edge["source"])
        or matches(edge["target"])
        or (edge["source"]["kind"] in executable and edge["target"]["kind"] in executable)
    ]

    def relevance(edge):
        text = " ".join(
            [edge["source"]["name"], edge["target"]["name"], edge["evidence"]["excerpt"]]
        ).lower()
        return (
            -sum(term in text for term in terms),
            edge["evidence"]["file"],
            edge["evidence"]["line"],
        )

    edges.sort(key=relevance)
    unique = {}
    for edge in edges:
        key = (edge["source"]["id"], edge["target"]["id"], edge["evidence"]["kind"])
        unique.setdefault(key, edge)
    edges = list(unique.values())
    context = []
    citations = []
    remaining = config.context_chars
    graph_budget = remaining // 2
    for edge in edges:
        evidence = edge["evidence"]
        item = (
            f"{edge['source']['name']} --{evidence['kind']}--> {edge['target']['name']} "
            f"(type={edge['target']['kind']}, technology={edge['target'].get('technology', '')}) "
            f"({evidence['certainty']}, stale={evidence.get('stale', False)}); {evidence['excerpt']}"
        )
        if len(item) + 20 > graph_budget:
            break
        label = f"E{len(citations) + 1}"
        context.append(f"[{label}] {item}")
        citations.append(
            f"[{label}] {evidence['file']}:{evidence['line']} ({evidence['certainty']})"
        )
        remaining -= len(context[-1]) + 1
        graph_budget -= len(context[-1]) + 1
    for index, source in enumerate(sources):
        if remaining < 150:
            break
        numbered, first_line = source_snippet(
            source.get("content", ""),
            terms,
            min(max(100, remaining // (len(sources) - index) - 150), 2000),
        )
        label = f"E{len(citations) + 1}"
        item = (
            f"[{label}] {source['path']} (component={source.get('component', 'unknown')}, "
            f"stale={bool(source.get('error'))}, "
            f"enrichment_pending={source.get('pending')})\n{numbered}"
        )
        context.append(item)
        citations.append(
            f"[{label}] {source['path']}:{first_line} (source excerpt; line numbers shown in context)"
        )
        remaining -= len(item) + 1
    if not model.available():
        return (
            "The local model is unavailable. Retrieved evidence is shown below; no generated answer was produced.\n\n"
            + "\n".join(context)
            + "\n\nSources:\n"
            + "\n".join(citations)
        )
    answer = model.answer(question, "\n\n".join(context))
    valid = {f"E{i}" for i in range(1, len(citations) + 1)}
    cited = set(re.findall(r"\[(E\d+)\]", answer))
    if not cited or not cited <= valid:
        return (
            "The model returned an answer without valid evidence references. "
            "Review the retrieved evidence instead:\n\n"
            + "\n\n".join(context)
            + "\n\nSources:\n"
            + "\n".join(citations)
        )
    return answer + "\n\nSources:\n" + "\n".join(citations)
