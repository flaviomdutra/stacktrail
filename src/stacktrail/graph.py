"""Neo4j persistence. Each source replacement is a single transaction."""

from dataclasses import asdict
from datetime import UTC, datetime

from neo4j import GraphDatabase

from stacktrail.config import Config
from stacktrail.models import Extraction, SourceFile, digest


def now() -> str:
    return datetime.now(UTC).isoformat()


class GraphStore:
    def __init__(self, config: Config):
        self.workspace = config.workspace
        self.database = config.database
        self.driver = GraphDatabase.driver(
            config.uri,
            auth=(config.user, config.password),
            connection_timeout=5,
            max_transaction_retry_time=5,
        )

    def __enter__(self):
        self.driver.verify_connectivity()
        return self

    def __exit__(self, *args):
        self.driver.close()

    def query(self, cypher: str, **parameters) -> list[dict]:
        records, _, _ = self.driver.execute_query(
            cypher,
            parameters_={"workspace": self.workspace, **parameters},
            database_=self.database,
        )
        return [record.data() for record in records]

    def initialize(self) -> None:
        for label in ("Workspace", "Source", "Entity"):
            self.query(
                f"CREATE CONSTRAINT stacktrail_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
        self.query(
            "CREATE FULLTEXT INDEX stacktrail_sources IF NOT EXISTS FOR (s:Source) ON EACH [s.content, s.path]"
        )
        self.query(
            "MERGE (w:Workspace {id: $workspace}) ON CREATE SET w.created = $time", time=now()
        )

    def inventory(self) -> dict[str, dict]:
        rows = self.query("MATCH (s:Source {workspace: $workspace}) RETURN s {.*} AS source")
        return {row["source"]["id"]: row["source"] for row in rows}

    def replace(
        self,
        source: SourceFile,
        result: Extraction,
        fingerprint: str,
        pending: bool,
        enrichment_error: str = "",
    ) -> None:
        entities = [
            {**asdict(entity), "id": self.key(entity.id)} for entity in result.entities.values()
        ]
        edges = [
            {
                **asdict(relation),
                "source": self.key(relation.source),
                "target": self.key(relation.target),
                "id": digest(source.id, str(i)),
                "source_id": source.id,
                "file": source.path,
                "repository": source.repository,
                "content_hash": source.content_hash,
            }
            for i, relation in enumerate(result.relations)
        ]
        properties = {
            **asdict(source),
            "fingerprint": fingerprint,
            "pending": pending,
            "error": "",
            "enrichment_error": enrichment_error,
            "synced_at": now(),
            "workspace": self.workspace,
        }

        def write(tx):
            tx.run(
                "MATCH ()-[r:DEPENDS_ON {workspace: $workspace, source_id: $source}]->() DELETE r",
                workspace=self.workspace,
                source=source.id,
            ).consume()
            tx.run(
                "MATCH (s:Source {id: $source})-[r:DECLARES]->() DELETE r", source=source.id
            ).consume()
            tx.run(
                "MERGE (s:Source {id: $source}) SET s = $properties",
                source=source.id,
                properties=properties,
            ).consume()
            tx.run(
                """UNWIND $entities AS entity
                MERGE (e:Entity {id: entity.id}) SET e += entity, e.workspace = $workspace
                WITH e MATCH (s:Source {id: $source}) MERGE (s)-[:DECLARES]->(e)""",
                entities=entities,
                workspace=self.workspace,
                source=source.id,
            ).consume()
            tx.run(
                """UNWIND $edges AS edge
                MATCH (a:Entity {id: edge.source}), (b:Entity {id: edge.target})
                CREATE (a)-[r:DEPENDS_ON]->(b) SET r = edge, r.workspace = $workspace""",
                edges=edges,
                workspace=self.workspace,
            ).consume()

        with self.driver.session(database=self.database) as session:
            session.execute_write(write)

    def key(self, entity_id: str) -> str:
        return self.workspace + ":" + entity_id

    def mark_error(self, source: SourceFile, error: str) -> None:
        self.query(
            """MERGE (s:Source {id: $id})
            ON CREATE SET s.workspace = $workspace, s.path = $path, s.repository = $repository,
                          s.content_hash = '', s.fingerprint = '', s.pending = true
            SET s.error = $error, s.attempted_at = $time""",
            id=source.id,
            path=source.path,
            repository=source.repository,
            error=error,
            time=now(),
        )

    def delete(self, source_id: str) -> None:
        def write(tx):
            tx.run(
                "MATCH ()-[r:DEPENDS_ON {workspace: $workspace, source_id: $id}]->() DELETE r",
                workspace=self.workspace,
                id=source_id,
            ).consume()
            tx.run(
                "MATCH (s:Source {id: $id, workspace: $workspace}) DETACH DELETE s",
                id=source_id,
                workspace=self.workspace,
            ).consume()

        with self.driver.session(database=self.database) as session:
            session.execute_write(write)

    def reconcile(self) -> None:
        """Resolve exact npm package references against unique, declared local packages."""

        def write(tx):
            tx.run(
                "MATCH ()-[r:DEPENDS_ON {workspace: $workspace, resolved: true}]->() DELETE r",
                workspace=self.workspace,
            ).consume()
            tx.run(
                """MATCH (a:Entity)-[r:DEPENDS_ON {workspace: $workspace}]->(library:Entity)
                WHERE library.kind = 'library' AND library.repository = 'npm'
                  AND r.kind IN ['uses-library', 'uses-dev-library']
                MATCH (manifest:Source {workspace: $workspace})-[:DECLARES]->(candidate:Entity)
                WHERE manifest.path ENDS WITH '/package.json' AND manifest.error = ''
                  AND candidate.kind = 'component' AND candidate.name = library.name
                  AND candidate.id <> a.id
                WITH a, r, collect(DISTINCT candidate) AS candidates
                WHERE size(candidates) = 1
                WITH a, r, candidates[0] AS target
                CREATE (a)-[resolved:DEPENDS_ON]->(target)
                SET resolved = properties(r), resolved.resolved = true,
                    resolved.kind = 'uses-local-package', resolved.id = r.id + ':resolved'""",
                workspace=self.workspace,
            ).consume()
            tx.run(
                """MATCH (e:Entity {workspace: $workspace})
                WHERE NOT (e)-[:DEPENDS_ON]-() AND NOT (:Source)-[:DECLARES]->(e)
                DELETE e""",
                workspace=self.workspace,
            ).consume()

        with self.driver.session(database=self.database) as session:
            session.execute_write(write)

    def finish(self, errors: int) -> None:
        self.query(
            "MATCH (w:Workspace {id: $workspace}) SET w.last_sync = $time, w.errors = $errors",
            time=now(),
            errors=errors,
        )

    def summary(self) -> dict:
        rows = self.query("""MATCH (w:Workspace {id: $workspace})
            RETURN w.last_sync AS last_sync, w.errors AS last_errors,
            COUNT { MATCH (:Entity {workspace: $workspace}) } AS entities,
            COUNT { MATCH ()-[:DEPENDS_ON {workspace: $workspace}]->() } AS relationships""")
        return rows[0] if rows else {"last_sync": None, "entities": 0, "relationships": 0}

    def edges(self, repository: str | None = None) -> list[dict]:
        return self.query(
            """MATCH (a:Entity)-[r:DEPENDS_ON {workspace: $workspace}]->(b:Entity)
            WHERE $repository IS NULL OR a.repository = $repository
                OR a.repository ENDS WITH '/' + $repository
            RETURN a {.*} AS source, b {.*} AS target, r {.*} AS evidence
            ORDER BY a.name, b.name, r.file, r.line""",
            repository=repository,
        )

    def entities(self, repository: str | None = None) -> list[dict]:
        return [
            row["entity"]
            for row in self.query(
                """MATCH (e:Entity {workspace: $workspace}) WHERE e.kind <> 'library'
            AND ($repository IS NULL OR e.repository = $repository
                OR e.repository ENDS WITH '/' + $repository)
            RETURN e {.*} AS entity ORDER BY e.name""",
                repository=repository,
            )
        ]

    def retrieve(self, search: str) -> list[dict]:
        # Full-text query syntax is produced by us, never by the model or user.
        rows = self.query(
            """CALL db.index.fulltext.queryNodes('stacktrail_sources', $search)
            YIELD node, score WHERE node.workspace = $workspace
            WITH node, score ORDER BY score DESC
            WITH node.repository AS repository, collect({source: node, score: score})[0..2] AS hits
            UNWIND range(0, size(hits) - 1) AS rank
            WITH hits[rank].source AS node, hits[rank].score AS score, rank
            RETURN node.id AS id, node.path AS path, node.content AS content,
                node.component AS component, node.repository AS repository,
                node.error AS error, node.pending AS pending, node.synced_at AS synced_at,
                score ORDER BY rank, score DESC LIMIT 16""",
            search=search,
        )
        return rows

    def neighborhood(self, source_ids: list[str], terms: list[str] | None = None) -> list[dict]:
        return self.query(
            """MATCH (s:Source {workspace: $workspace})-[:DECLARES]->(seed:Entity)
            WHERE s.id IN $ids AND seed.kind <> 'library'
            WITH DISTINCT seed ORDER BY CASE WHEN seed.kind = 'component' THEN 0 ELSE 1 END,
                seed.name LIMIT 24
            MATCH path = (seed)-[:DEPENDS_ON*1..3]-(neighbor:Entity)
            WHERE all(r IN relationships(path) WHERE r.workspace = $workspace)
                AND all(n IN nodes(path) WHERE n.kind <> 'library')
                AND (size($terms) = 0 OR any(term IN $terms WHERE
                    toLower(neighbor.name) CONTAINS term
                    OR toLower(coalesce(neighbor.technology, '')) CONTAINS term
                    OR toLower(neighbor.kind) CONTAINS term))
            WITH path LIMIT 100
            UNWIND relationships(path) AS r
            WITH DISTINCT r
            WITH r, startNode(r) AS a, endNode(r) AS b
            OPTIONAL MATCH (support:Source {id: r.source_id, workspace: $workspace})
            RETURN a {.*} AS source, b {.*} AS target,
                r {.*, stale: coalesce(support.error, '') <> ''} AS evidence LIMIT 80""",
            ids=source_ids,
            terms=terms or [],
        )

    def purge(self) -> None:
        """Used only by explicitly enabled integration tests for their unique workspace."""
        self.query("MATCH (n) WHERE n.workspace = $workspace OR n.id = $workspace DETACH DELETE n")
