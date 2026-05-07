"""Billing group configuration: YAML loader and group resolution."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BillingGroup:
    key: str
    display_name: str
    contact: Optional[str]
    users: list[str] = field(default_factory=list)  # match by username (any lab)
    labs: list[str] = field(default_factory=list)   # match by lab (any user)


@dataclass
class BillingConfig:
    groups: dict[str, BillingGroup]  # key → group, ordered (insertion order)

    def resolve_group(self, username: str, lab: str) -> Optional[str]:
        """Return the group key for a session. User-level match beats lab-level."""
        for key, group in self.groups.items():
            if username in group.users:
                return key
        for key, group in self.groups.items():
            if lab in group.labs:
                return key
        return None

    def get_display_name(self, group_key: str) -> str:
        if group_key == "unassigned":
            return "Unassigned"
        g = self.groups.get(group_key)
        return g.display_name if g else group_key

    def get_contact(self, group_key: str) -> Optional[str]:
        g = self.groups.get(group_key)
        return g.contact if g else None

    def all_groups(self) -> list[str]:
        return list(self.groups.keys())


def load_billing_config(path: Path) -> BillingConfig:
    """Load billing_config.yaml. Returns empty config if file is missing."""
    if not path.exists():
        logger.warning("billing_config.yaml not found at %s — no group assignments", path)
        return BillingConfig(groups={})

    import yaml

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    raw_groups = data.get("billing_groups") or {}
    groups: dict[str, BillingGroup] = {}
    for key, val in raw_groups.items():
        if not isinstance(val, dict):
            logger.warning("billing_groups.%s: expected a dict, skipping", key)
            continue
        groups[key] = BillingGroup(
            key=key,
            display_name=val.get("display_name") or key,
            contact=val.get("contact"),
            users=[str(u) for u in (val.get("users") or [])],
            labs=[str(l) for l in (val.get("labs") or [])],
        )

    logger.info("Loaded %d billing groups from %s", len(groups), path)
    return BillingConfig(groups=groups)
