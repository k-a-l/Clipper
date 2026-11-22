"""Posting accounts: any number of YouTube channels and Facebook Pages.

Each account has a *key* used everywhere a platform used to be (queue jobs, limits, schedules):

* ``youtube`` / ``facebook``: the first channel/Page (the one set up from ``.env`` / the CLI);
  keeping the bare names means everything posted before multi-account support stays valid.
* ``youtube:<channel id>`` / ``facebook:<page id>``: channels and Pages added later.

Tokens live encrypted in ``storage/secrets`` (one file per account); this registry keeps only
public details (name, id, whether new clips post there by default) in ``state/accounts.json``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clipper.config import Platform, Settings, account_platform
from clipper.modules.utils import EncryptedTokenStore, TokenStoreError

log = logging.getLogger(__name__)


@dataclass
class Account:
    key: str
    platform: Platform
    name: str = ""
    ext_id: str = ""  # YouTube channel id / Facebook Page id
    default: bool = True  # new clips post here unless a style picks accounts explicitly
    added_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return f"{'YouTube' if self.platform == 'youtube' else 'Facebook'}{f' · {self.name}' if self.name else ''}"


def token_store(s: Settings, key: str) -> EncryptedTokenStore:
    """Encrypted token file for an account (the first accounts keep their original file names)."""
    secret = s.token_encryption_key.get_secret_value() if s.token_encryption_key else None
    plat, _, ext = key.partition(":")
    name = f"{plat}_token.enc" if not ext else f"{plat}_{ext}.enc"
    return EncryptedTokenStore(s.secrets_dir / name, secret)


def key_for(s: Settings, platform: Platform, ext_id: str, existing: list[Account]) -> str:
    """The key a newly connected channel/Page gets (re-connecting reuses its key)."""
    for a in existing:
        if a.platform == platform and a.ext_id and a.ext_id == ext_id:
            return a.key
    if platform == "facebook" and s.facebook_page_id and ext_id == s.facebook_page_id:
        return "facebook"
    base = next((a for a in existing if a.key == platform), None)
    if base is None and not (platform == "facebook" and s.facebook_page_id):
        return platform  # first account of this platform
    if base is not None and not base.ext_id:
        return platform  # the original account whose id wasn't known yet: same login, same key
    return f"{platform}:{ext_id}"


class AccountRegistry:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.path = s.state_dir / "accounts.json"
        self.accounts: dict[str, Account] = {}
        self._load()

    # ---------------------------------------------------------------- io
    def _load(self) -> None:
        if self.path.is_file():
            try:
                for item in json.loads(self.path.read_text(encoding="utf-8")):
                    a = Account(**item)
                    self.accounts[a.key] = a
            except (ValueError, TypeError, OSError) as exc:
                log.error("cannot read %s: %s", self.path, exc)
        self._adopt_existing_tokens()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(a) for a in self.accounts.values()], indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _adopt_existing_tokens(self) -> None:
        """Accounts connected before this registry existed (or via the CLI) appear automatically."""
        changed = False
        for key in ("youtube", "facebook"):
            if key in self.accounts or account_platform(key) not in self.s.publish_platforms:
                continue
            try:
                store = token_store(self.s, key)
                if not store.exists():
                    continue
                data = store.load() if key == "facebook" else {}
            except TokenStoreError:
                continue
            self.accounts[key] = Account(key=key, platform=account_platform(key),
                                         name=str(data.get("page_name") or ""),
                                         ext_id=str(self.s.facebook_page_id or "") if key == "facebook" else "")
            changed = True
        # extra accounts whose token file exists but whose registry entry was lost
        for f in self.s.secrets_dir.glob("*_*.enc") if self.s.secrets_dir.is_dir() else []:
            plat, _, ext = f.stem.partition("_")
            if plat in ("youtube", "facebook") and ext not in ("token", "") and f"{plat}:{ext}" not in self.accounts:
                self.accounts[f"{plat}:{ext}"] = Account(key=f"{plat}:{ext}", platform=account_platform(plat),
                                                         ext_id=ext, default=False)
                changed = True
        if changed:
            self.save()

    # --------------------------------------------------------------- queries
    def get(self, key: str) -> Account | None:
        return self.accounts.get(key)

    def all(self) -> list[Account]:
        return sorted(self.accounts.values(), key=lambda a: (a.platform != "youtube", a.added_at))

    def defaults(self) -> list[str]:
        return [a.key for a in self.all() if a.default]

    def connected(self, key: str) -> bool:
        try:
            return token_store(self.s, key).exists()
        except TokenStoreError:
            return False

    # --------------------------------------------------------------- changes
    def upsert(self, platform: Platform, ext_id: str, name: str) -> Account:
        key = key_for(self.s, platform, ext_id, list(self.accounts.values()))
        acct = self.accounts.get(key)
        if acct is None:
            first = not any(a.platform == platform for a in self.accounts.values())
            acct = Account(key=key, platform=platform, name=name, ext_id=ext_id, default=first)
            self.accounts[key] = acct
        else:
            acct.name, acct.ext_id = name or acct.name, ext_id or acct.ext_id
        self.save()
        return acct

    def set_default(self, key: str, default: bool) -> Account:
        acct = self.accounts[key]
        acct.default = default
        self.save()
        return acct

    def remove(self, key: str) -> Account:
        acct = self.accounts.pop(key)
        self.save()
        try:
            token_store(self.s, key).path.unlink(missing_ok=True)
        except TokenStoreError:
            pass
        return acct

    def describe(self, key: str) -> dict[str, Any]:
        a = self.accounts.get(key)
        return asdict(a) | {"label": a.label} if a else {"key": key, "label": key}


def token_path(s: Settings, key: str) -> Path:
    return token_store(s, key).path
