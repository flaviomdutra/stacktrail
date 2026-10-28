"""Explicit, incremental synchronization; no watcher and no background inference."""

import os
import tempfile
from pathlib import Path

from filelock import FileLock, Timeout

from stacktrail.config import Config
from stacktrail.extract import extract
from stacktrail.graph import GraphStore
from stacktrail.llm import ModelError, ModelProvider
from stacktrail.models import SyncReport, digest
from stacktrail.scanner import scan


def synchronize(
    config: Config, store: GraphStore, model: ModelProvider, full: bool = False, progress=None
) -> SyncReport:
    lock_key = digest(config.uri, config.database, config.workspace)
    lock_dir = Path(tempfile.gettempdir()) / f"stacktrail-{os.getuid()}"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    lock = FileLock(lock_dir / f"{lock_key}.lock", timeout=0)
    try:
        with lock:
            return _synchronize(config, store, model, full, progress)
    except Timeout as error:
        raise ValueError(
            "Another synchronization is already running for this workspace."
        ) from error


def _synchronize(config, store, model, full, progress):
    # Finish inventory collection before deleting anything. A missing/unreadable root aborts.
    sources = scan(config)
    store.initialize()
    previous = store.inventory()
    report = SyncReport()
    available = None
    current_ids = {source.id for source in sources}
    for source in sources:
        if progress:
            progress(source.path)
        prior = previous.get(source.id, {})
        fingerprint = digest(config.fingerprint, source.repository, source.component)
        changed = (
            full
            or prior.get("content_hash") != source.content_hash
            or prior.get("fingerprint") != fingerprint
            or bool(prior.get("error"))
        )
        pending = bool(prior.get("pending")) and config.enrichment
        if not changed and not pending:
            report.unchanged += 1
            continue
        if config.enrichment and available is None:
            available = model.available()
        if not changed and not available:
            report.unchanged += 1
            report.pending += 1
            continue
        try:
            result = extract(source)
            enrichment_pending = config.enrichment
            enrichment_error = ""
            if config.enrichment and available:
                try:
                    model.enrich(source, result)
                    enrichment_pending = False
                except ModelError as error:
                    enrichment_error = str(error)
                    # Preserve successful inferred results on an unchanged source during retry.
                    if not changed:
                        report.pending += 1
                        report.errors.append(f"{source.path}: {error}")
                        continue
            if enrichment_pending and not enrichment_error:
                enrichment_error = "Ollama or the configured model is unavailable."
            store.replace(source, result, fingerprint, enrichment_pending, enrichment_error)
            report.updated += 1
            report.pending += int(enrichment_pending)
        except Exception as error:
            # Never persist raw exception details: malformed documents may include credentials.
            message = f"Extraction or storage failed ({type(error).__name__}); previous evidence retained."
            store.mark_error(source, message)
            report.errors.append(f"{source.path}: {message}")
    for source_id in previous.keys() - current_ids:
        store.delete(source_id)
        report.deleted += 1
    store.reconcile()
    store.finish(len(report.errors))
    return report
