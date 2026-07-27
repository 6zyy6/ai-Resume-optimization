from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.ids import new_id


@dataclass
class UserAccount:
    id: str
    status: str
    email_encrypted: str | None
    email_lookup_hash: str | None
    created_at: datetime


@dataclass
class IdentityRecord:
    id: str
    owner_user_id: str
    identity_type: str
    external_subject_hash: str
    verified_at: datetime


@dataclass
class ConsentRecord:
    id: str
    owner_user_id: str
    document_type: str
    document_version: str
    decision: str
    decided_at: datetime


class UserStore(Protocol):
    def find_user_by_email_hash(self, email_hash: str) -> UserAccount | None: ...
    def save_user(self, user: UserAccount) -> None: ...
    def save_identity(self, identity: IdentityRecord) -> None: ...
    def save_consent(self, consent: ConsentRecord) -> None: ...


class EmailCrypto(Protocol):
    def encrypt(self, email: str, key: bytes) -> str: ...
    def lookup_hash(self, email: str, key: bytes) -> str: ...


class KeyProvider(Protocol):
    def get_key(self, purpose: str) -> bytes: ...


class UserService:
    def __init__(
        self,
        repository: UserStore,
        email_crypto: EmailCrypto,
        keys: KeyProvider,
    ) -> None:
        self.repository = repository
        self.email_crypto = email_crypto
        self.keys = keys

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    def email_lookup_hash(self, email: str) -> str:
        normalized = self.normalize_email(email)
        return self.email_crypto.lookup_hash(
            normalized,
            self.keys.get_key("email-lookup"),
        )

    def find_by_email(self, email: str) -> UserAccount | None:
        return self.repository.find_user_by_email_hash(self.email_lookup_hash(email))

    def create_email_user(
        self,
        email: str,
        now: datetime,
        document_type: str,
        document_version: str,
        decision: str,
    ) -> UserAccount:
        normalized = self.normalize_email(email)
        lookup_hash = self.email_lookup_hash(normalized)
        user = UserAccount(
            id=new_id("usr"),
            status="active",
            email_encrypted=self.email_crypto.encrypt(
                normalized,
                self.keys.get_key("email-encryption"),
            ),
            email_lookup_hash=lookup_hash,
            created_at=now,
        )
        self.repository.save_user(user)
        self.repository.save_identity(
            IdentityRecord(
                id=new_id("idn"),
                owner_user_id=user.id,
                identity_type="email_otp",
                external_subject_hash=lookup_hash,
                verified_at=now,
            )
        )
        self.repository.save_consent(
            ConsentRecord(
                id=new_id("cns"),
                owner_user_id=user.id,
                document_type=document_type,
                document_version=document_version,
                decision=decision,
                decided_at=now,
            )
        )
        return user

    def bind_email(self, user: UserAccount, email: str, now: datetime) -> None:
        normalized = self.normalize_email(email)
        lookup_hash = self.email_lookup_hash(normalized)
        user.email_encrypted = self.email_crypto.encrypt(
            normalized,
            self.keys.get_key("email-encryption"),
        )
        user.email_lookup_hash = lookup_hash
        self.repository.save_user(user)
        self.repository.save_identity(
            IdentityRecord(
                id=new_id("idn"),
                owner_user_id=user.id,
                identity_type="email_otp",
                external_subject_hash=lookup_hash,
                verified_at=now,
            )
        )
