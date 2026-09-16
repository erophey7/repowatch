"""Own shared storage handles and the process-local bandwidth budget."""

from __future__ import annotations

from pathlib import Path
from repowatch.bandwidth import BandwidthBudget
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
