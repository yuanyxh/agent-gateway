"""File-backed service registry; one JSON file per service."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agent_gateway.services.mesh.models import MeshErrorCode, ServiceCard

SERVICES_DIR = "services"
TRASH_DIR = ".trash"
MAX_SERVICES = 500
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RegistryError(Exception):
    def __init__(self, code: MeshErrorCode, detail: str = ""):
        super().__init__(detail or code.value)
        self.code = code


class FileRegistry:
    """Cards live as `<root>/services/<id>.json`, written atomically."""

    def __init__(self, root: Path, gateway_id: str, max_services: int = MAX_SERVICES):
        self.root = Path(root)
        self.services_dir = self.root / SERVICES_DIR
        self.trash_dir = self.services_dir / TRASH_DIR
        self.gateway_id = gateway_id
        self.max_services = max_services

    def _ensure_dirs(self) -> None:
        self.services_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, service_id: str) -> Path:
        if not ID_PATTERN.match(service_id):
            raise RegistryError(
                MeshErrorCode.INVALID_CARD, f"bad service id: {service_id!r}"
            )
        return self.services_dir / f"{service_id}.json"

    def _count(self) -> int:
        if not self.services_dir.is_dir():
            return 0
        return sum(1 for path in self.services_dir.glob("*.json"))

    def register(self, card: ServiceCard) -> ServiceCard:
        self._ensure_dirs()
        target = self._path(card.id)
        if not target.exists() and self._count() >= self.max_services:
            if not self._prune_stale():
                raise RegistryError(
                    MeshErrorCode.REGISTRY_FULL, f"limit {self.max_services}"
                )
        stored = card.model_copy(
            update={"owner_gateway": card.owner_gateway or self.gateway_id}
        )
        self._write(target, stored)
        return stored

    def _prune_stale(self) -> bool:
        """Expired or unreadable cards free their slot; they move to .trash."""
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        for path in list(self.services_dir.glob("*.json")):
            card = self._read(path)
            if card is not None and card.is_alive():
                continue
            try:
                os.replace(path, self.trash_dir / path.name)
            except OSError:
                continue
        return self._count() < self.max_services

    def _write(self, target: Path, card: ServiceCard) -> None:
        payload = card.model_dump_json(indent=2).encode("utf-8")
        handle = tempfile.NamedTemporaryFile(
            dir=self.services_dir,
            prefix=f".{target.stem}-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, target)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def get(self, service_id: str, include_expired: bool = False) -> ServiceCard | None:
        try:
            target = self._path(service_id)
        except RegistryError:
            return None
        if not target.is_file():
            return None
        card = self._read(target)
        if card is None:
            return None
        if not include_expired and not card.is_alive():
            return None
        return self._with_status(card)

    def _read(self, target: Path) -> ServiceCard | None:
        try:
            return ServiceCard.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _with_status(self, card: ServiceCard) -> ServiceCard:
        return (
            card if card.is_alive() else card.model_copy(update={"status": "offline"})
        )

    def list(self) -> list[ServiceCard]:
        if not self.services_dir.is_dir():
            return []
        cards = []
        for path in sorted(self.services_dir.glob("*.json")):
            card = self._read(path)
            if card is not None:
                cards.append(self._with_status(card))
        return cards

    def touch(
        self,
        service_id: str,
        status: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        try:
            target = self._path(service_id)
        except RegistryError:
            return False
        card = self._read(target) if target.is_file() else None
        if card is None:
            return False
        updates: dict = {"heartbeat_at": now or datetime.now(timezone.utc)}
        if status is not None:
            updates["status"] = status
        try:
            stored = ServiceCard.model_validate({**card.model_dump(), **updates})
        except ValueError:
            return False
        self._write(target, stored)
        return True

    def deregister(self, service_id: str) -> bool:
        try:
            target = self._path(service_id)
        except RegistryError:
            return False
        if not target.is_file():
            return False
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        os.replace(target, self.trash_dir / target.name)
        return True
