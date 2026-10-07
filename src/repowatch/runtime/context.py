"""Own shared storage handles and the process-local bandwidth budget."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from repowatch.bandwidth import BandwidthBudget
from repowatch.config.models import RepoConfig
from repowatch.storage.cache import CacheStore
from repowatch.storage.database import Database
from repowatch.storage.notifications import NotificationsStore
from repowatch.storage.queries import QueriesStore
from repowatch.storage.repositories import RepositoriesStore
from repowatch.storage.requests import RequestsStore

class ServiceState:
    """Shared stores and bandwidth budget for one running service instance."""

    def __init__(self, db_path: str | Path):
        self.database = Database(db_path)
        self.repositories = RepositoriesStore(self.database)
        self.cache = CacheStore(self.database)
        self.requests = RequestsStore(self.database)
        self.notifications = NotificationsStore(self.database)
        self.queries = QueriesStore(self.database)
        self.bandwidth = BandwidthBudget()
        self.completeness_lock = Lock()
        # Full inventory reports and dedup maintenance share one admission gate.
        self.dedup_cleanup_lock = self.completeness_lock
        self.dedup_cleanup_cursor = ""
        self.dedup_cleanup_priority_cursor = ""
        self.dedup_cleanup_recent: dict[str, float] = {}
        self.dedup_cleanup_evidence = None
        self.dedup_cleanup_pending: tuple[str, str, list[str]] | None = None
        self.automatic_warm_repos: dict[str, RepoConfig] | None = None

    def automatic_warm_enabled(self, repo: RepoConfig) -> bool:
        """Stop queued automatic downloads after the scheduler observes an opt-out."""
        repos = self.automatic_warm_repos
        current = repo if repos is None else repos.get(repo.id)
        return bool(current and current.prefetch and current.catalog_identity() == repo.catalog_identity())
