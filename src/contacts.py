from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import ensure_dir, iso_now, write_json


@dataclass(slots=True)
class SpeakerContact:
    discord_user_id: str
    preferred_name: str = ""
    discord_display_name: str = ""
    relationship: str = ""
    aliases: list[str] = field(default_factory=list)
    first_seen_at: str = ""
    last_seen_at: str = ""
    meeting_count: int = 0
    notes: str = ""

    @property
    def best_name(self) -> str:
        return self.preferred_name.strip() or self.discord_display_name.strip() or f"Discord User {self.discord_user_id}"

    @property
    def transcript_label(self) -> str:
        name = self.best_name
        if self.discord_user_id and self.discord_user_id not in name:
            return f"{name} ({self.discord_user_id})"
        return name

    def to_dict(self) -> dict[str, Any]:
        return {
            "discord_user_id": self.discord_user_id,
            "preferred_name": self.preferred_name,
            "discord_display_name": self.discord_display_name,
            "relationship": self.relationship,
            "aliases": self.aliases,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "meeting_count": self.meeting_count,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, user_id: str, data: dict[str, Any]) -> "SpeakerContact":
        return cls(
            discord_user_id=str(data.get("discord_user_id") or user_id),
            preferred_name=str(data.get("preferred_name") or ""),
            discord_display_name=str(data.get("discord_display_name") or ""),
            relationship=str(data.get("relationship") or ""),
            aliases=[str(item) for item in data.get("aliases", []) if str(item).strip()]
            if isinstance(data.get("aliases", []), list)
            else [],
            first_seen_at=str(data.get("first_seen_at") or ""),
            last_seen_at=str(data.get("last_seen_at") or ""),
            meeting_count=int(data.get("meeting_count") or 0),
            notes=str(data.get("notes") or ""),
        )


class SpeakerContactBook:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.contacts: dict[str, SpeakerContact] = {}
        self._loaded = False

    def load(self) -> "SpeakerContactBook":
        self.contacts.clear()
        if not self.path.exists():
            self._loaded = True
            return self
        data = read_json_object(self.path)
        users = data.get("users", {}) if isinstance(data, dict) else {}
        if isinstance(users, dict):
            for user_id, raw_contact in users.items():
                if isinstance(raw_contact, dict):
                    self.contacts[str(user_id)] = SpeakerContact.from_dict(str(user_id), raw_contact)
        self._loaded = True
        return self

    def save(self) -> None:
        ensure_dir(self.path.parent)
        data = {
            "version": 1,
            "updated_at": iso_now(),
            "users": {user_id: contact.to_dict() for user_id, contact in sorted(self.contacts.items())},
        }
        write_json(self.path, data)

    def upsert_seen(self, user_id: str | int | None, discord_display_name: str | None = None) -> SpeakerContact | None:
        if user_id is None:
            return None
        self._ensure_loaded()
        key = str(user_id)
        now = iso_now()
        contact = self.contacts.get(key)
        if contact is None:
            contact = SpeakerContact(discord_user_id=key, first_seen_at=now)
            self.contacts[key] = contact
        contact.last_seen_at = now
        contact.meeting_count += 1
        if discord_display_name:
            contact.discord_display_name = discord_display_name
        self.save()
        return contact

    def set_preferred_name(self, user_id: str | int, preferred_name: str) -> SpeakerContact:
        self._ensure_loaded()
        key = str(user_id)
        contact = self.contacts.get(key)
        now = iso_now()
        if contact is None:
            contact = SpeakerContact(discord_user_id=key, first_seen_at=now, last_seen_at=now)
            self.contacts[key] = contact
        contact.preferred_name = preferred_name.strip()
        contact.last_seen_at = now
        self.save()
        return contact

    def set_contact(
        self,
        user_id: str | int,
        preferred_name: str,
        relationship: str = "",
        alias: str = "",
        discord_display_name: str = "",
    ) -> SpeakerContact:
        self._ensure_loaded()
        key = str(user_id)
        now = iso_now()
        contact = self.contacts.get(key)
        if contact is None:
            contact = SpeakerContact(discord_user_id=key, first_seen_at=now)
            self.contacts[key] = contact
        contact.preferred_name = preferred_name.strip()
        contact.relationship = relationship.strip()
        if discord_display_name.strip():
            contact.discord_display_name = discord_display_name.strip()
        alias = alias.strip().lower()
        if alias and alias not in contact.aliases:
            contact.aliases.append(alias)
            contact.aliases.sort()
        contact.last_seen_at = now
        self.save()
        return contact

    def get(self, user_id: str | int | None) -> SpeakerContact | None:
        if user_id is None:
            return None
        self._ensure_loaded()
        return self.contacts.get(str(user_id))

    def label_for(self, user_id: str | int | None, fallback_name: str = "Speaker") -> str:
        contact = self.get(user_id)
        if contact:
            return contact.transcript_label
        if user_id is None:
            return fallback_name
        fallback = fallback_name.strip()
        if fallback and fallback != "Speaker":
            return f"{fallback} ({user_id})"
        return f"Discord User {user_id}"

    def all_contacts(self) -> list[SpeakerContact]:
        self._ensure_loaded()
        return [self.contacts[user_id] for user_id in sorted(self.contacts)]

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
