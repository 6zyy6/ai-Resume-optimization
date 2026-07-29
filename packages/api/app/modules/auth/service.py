import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.ids import new_id
from app.modules.auth.preflight import (
    OTP_TTL,
    AuthPreflightRejected,
    AuthPreflightStore,
    AuthPreflightUnavailable,
    InMemoryAuthPreflightStore,
    OtpChallenge,
    UnavailableAuthPreflightStore,
)
from app.modules.auth.schemas import ConsentInput
from app.modules.users.service import (
    ConsentRecord,
    EmailCrypto,
    IdentityRecord,
    KeyProvider,
    UserAccount,
    UserService,
)


SESSION_TTL = timedelta(days=30)
REQUIRED_CONSENTS = {
    "user_agreement": "2026-07-27",
    "privacy_policy": "2026-07-27",
}


class Clock(Protocol):
    def now(self) -> datetime: ...


class EmailSender(Protocol):
    async def send_otp(self, email: str, code: str) -> None: ...


class WechatExchange(Protocol):
    async def exchange(self, code: str) -> str | None: ...


class SecretHasher(Protocol):
    def hash_secret(self, value: str, key: bytes) -> str: ...


class PasswordHasher(Protocol):
    def hash_password(self, password: str) -> str: ...
    def verify_password(self, password: str, encoded: str) -> bool: ...


class AuthProviderUnavailable(Exception):
    pass


@dataclass
class SessionRecord:
    id: str
    owner_user_id: str
    token_hash: str
    authenticated_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class AuthRepository(Protocol):
    async def find_user(self, user_id: str) -> UserAccount | None: ...
    async def find_user_by_email_hash(self, email_hash: str) -> UserAccount | None: ...
    async def find_user_by_identity(
        self,
        identity_type: str,
        subject_hash: str,
    ) -> UserAccount | None: ...
    async def save_user(self, user: UserAccount) -> None: ...
    async def save_identity(self, identity: IdentityRecord) -> None: ...
    async def save_consent(self, consent: ConsentRecord) -> None: ...
    async def save_session(self, session: SessionRecord) -> None: ...
    async def find_session(self, token_hash: str) -> SessionRecord | None: ...
    async def revoke_all_sessions(self, user_id: str, now: datetime) -> None: ...
    async def consents_for_user(self, user_id: str) -> tuple[ConsentRecord, ...]: ...
    async def set_password_if_missing(
        self,
        user_id: str,
        password_hash: str,
    ) -> bool: ...
    async def merge_users(self, source_user_id: str, target_user_id: str) -> None: ...
    async def create_user_with_identity_and_consents(
        self,
        user: UserAccount,
        identity: IdentityRecord,
        consents: tuple[ConsentRecord, ...],
    ) -> None: ...


class HmacSecretHasher:
    def hash_secret(self, value: str, key: bytes) -> str:
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


class ScryptPasswordHasher:
    algorithm = "scrypt"
    version = "v1"
    n = 2**14
    r = 8
    p = 1
    salt_bytes = 16
    derived_bytes = 32

    def hash_password(self, password: str) -> str:
        salt = secrets.token_bytes(self.salt_bytes)
        derived = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.derived_bytes,
        )
        return "$".join(
            (
                self.algorithm,
                self.version,
                str(self.n),
                str(self.r),
                str(self.p),
                base64.urlsafe_b64encode(salt).decode(),
                base64.urlsafe_b64encode(derived).decode(),
            )
        )

    def verify_password(self, password: str, encoded: str) -> bool:
        try:
            algorithm, version, n, r, p, salt_value, expected_value = encoded.split(
                "$"
            )
            if (
                algorithm != self.algorithm
                or version != self.version
                or int(n) != self.n
                or int(r) != self.r
                or int(p) != self.p
            ):
                return False
            salt = base64.urlsafe_b64decode(salt_value.encode())
            expected = base64.urlsafe_b64decode(expected_value.encode())
            if len(salt) != self.salt_bytes or len(expected) != self.derived_bytes:
                return False
        except (binascii.Error, TypeError, ValueError):
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.derived_bytes,
        )
        return hmac.compare_digest(actual, expected)


DEFAULT_PASSWORD_HASHER = ScryptPasswordHasher()
DUMMY_PASSWORD_HASH = DEFAULT_PASSWORD_HASHER.hash_password(
    "this-password-does-not-belong-to-an-account"
)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ProcessKeys:
    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}

    def get_key(self, purpose: str) -> bytes:
        if purpose not in self._keys:
            self._keys[purpose] = secrets.token_bytes(32)
        return self._keys[purpose]


class EnvelopeEmailCrypto:
    def encrypt(self, email: str, key: bytes) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, email.encode(), b"email:v1")
        return base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def lookup_hash(self, email: str, key: bytes) -> str:
        return hmac.new(key, email.encode(), hashlib.sha256).hexdigest()

    def decrypt(self, encrypted: str, key: bytes) -> str:
        payload = base64.urlsafe_b64decode(encrypted.encode())
        nonce, ciphertext = payload[:12], payload[12:]
        return AESGCM(key).decrypt(nonce, ciphertext, b"email:v1").decode()


class UnavailableEmailSender:
    async def send_otp(self, email: str, code: str) -> None:
        raise AuthProviderUnavailable


class UnavailableWechatExchange:
    async def exchange(self, code: str) -> str | None:
        raise AuthProviderUnavailable


class InMemoryAuthRepository:
    def __init__(self) -> None:
        self.users: dict[str, UserAccount] = {}
        self.identities: dict[tuple[str, str], IdentityRecord] = {}
        self.consents: list[ConsentRecord] = []
        self.sessions: dict[str, SessionRecord] = {}

    async def find_user(self, user_id: str) -> UserAccount | None:
        return self.users.get(user_id)

    async def find_user_by_email_hash(self, email_hash: str) -> UserAccount | None:
        return next(
            (user for user in self.users.values() if user.email_lookup_hash == email_hash),
            None,
        )

    async def find_user_by_identity(
        self,
        identity_type: str,
        subject_hash: str,
    ) -> UserAccount | None:
        identity = self.identities.get((identity_type, subject_hash))
        return self.users.get(identity.owner_user_id) if identity else None

    async def save_user(self, user: UserAccount) -> None:
        self.users[user.id] = user

    async def save_identity(self, identity: IdentityRecord) -> None:
        self.identities[(identity.identity_type, identity.external_subject_hash)] = identity

    async def save_consent(self, consent: ConsentRecord) -> None:
        self.consents.append(consent)

    async def save_session(self, session: SessionRecord) -> None:
        self.sessions[session.token_hash] = session

    async def find_session(self, token_hash: str) -> SessionRecord | None:
        return self.sessions.get(token_hash)

    async def revoke_all_sessions(self, user_id: str, now: datetime) -> None:
        for session in self.sessions.values():
            if session.owner_user_id == user_id and session.revoked_at is None:
                session.revoked_at = now

    async def consents_for_user(self, user_id: str) -> tuple[ConsentRecord, ...]:
        return tuple(
            consent
            for consent in self.consents
            if consent.owner_user_id == user_id
        )

    async def set_password_if_missing(
        self,
        user_id: str,
        password_hash: str,
    ) -> bool:
        user = self.users.get(user_id)
        if user is None or user.password_hash is not None:
            return False
        user.password_hash = password_hash
        return True

    async def merge_users(self, source_user_id: str, target_user_id: str) -> None:
        source = self.users[source_user_id]
        source.status = "merged"
        for identity in self.identities.values():
            if identity.owner_user_id == source_user_id:
                identity.owner_user_id = target_user_id
        for consent in self.consents:
            if consent.owner_user_id == source_user_id:
                consent.owner_user_id = target_user_id
        for session in self.sessions.values():
            if session.owner_user_id == source_user_id:
                session.owner_user_id = target_user_id

    async def create_user_with_identity_and_consents(
        self,
        user: UserAccount,
        identity: IdentityRecord,
        consents: tuple[ConsentRecord, ...],
    ) -> None:
        self.users[user.id] = user
        self.identities[
            (identity.identity_type, identity.external_subject_hash)
        ] = identity
        self.consents.extend(consents)


class AuthError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        retry_after: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = details or {}


@dataclass(frozen=True)
class AuthenticatedSession:
    user_id: str
    session_id: str
    authenticated_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LoginResult:
    user_id: str
    raw_token: str
    expires_at: datetime


def cookie_secure_for_environment(app_env: str) -> bool:
    return app_env == "production"


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        preflight_store: AuthPreflightStore,
        email_sender: EmailSender,
        wechat_exchange: WechatExchange,
        email_crypto: EmailCrypto,
        keys: KeyProvider,
        hasher: SecretHasher,
        clock: Clock,
        code_factory: Callable[[], str],
        token_factory: Callable[[], str],
        app_env: str,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        self.repository = repository
        self.preflight_store = preflight_store
        self.email_sender = email_sender
        self.wechat_exchange = wechat_exchange
        self.keys = keys
        self.hasher = hasher
        self.clock = clock
        self.code_factory = code_factory
        self.token_factory = token_factory
        self.password_hasher = password_hasher or DEFAULT_PASSWORD_HASHER
        self.cookie_secure = cookie_secure_for_environment(app_env)
        self.users = UserService(repository, email_crypto, keys)

    def _hash(self, value: str, purpose: str) -> str:
        return self.hasher.hash_secret(value, self.keys.get_key(purpose))

    async def start_email(self, email: str, ip_address: str) -> None:
        normalized = self.users.normalize_email(email)
        email_hash = self.users.email_lookup_hash(normalized)
        ip_hash = self._hash(ip_address, "ip-rate-limit")
        now = self.clock.now()
        code = self.code_factory()
        if len(code) != 6 or not code.isdigit():
            raise RuntimeError("OTP generator must return six digits")
        try:
            await self.preflight_store.issue(
                OtpChallenge(
                    email_hash=email_hash,
                    code_hash=self._hash(code, "otp"),
                    sent_at=now,
                    expires_at=now + OTP_TTL,
                ),
                ip_hash,
                now,
            )
        except AuthPreflightRejected as error:
            raise AuthError(
                "AUTH_RATE_LIMITED",
                "Too many authentication attempts",
                429,
                error.retry_after,
            )
        except AuthPreflightUnavailable:
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                "Authentication provider is unavailable",
                503,
            )
        try:
            await self.email_sender.send_otp(normalized, code)
        except AuthProviderUnavailable:
            await self.preflight_store.consume_challenge(email_hash)
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                "Authentication provider is unavailable",
                503,
            )

    async def _verify_code(self, email: str, code: str) -> UserAccount | None:
        email_hash = self.users.email_lookup_hash(email)
        try:
            challenge = await self.preflight_store.get_challenge(
                email_hash,
                self.clock.now(),
            )
        except AuthPreflightUnavailable:
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                "Authentication provider is unavailable",
                503,
            )
        code_hash = self._hash(code, "otp")
        now = self.clock.now()
        if (
            challenge is None
            or challenge.expires_at <= now
            or not hmac.compare_digest(challenge.code_hash, code_hash)
        ):
            raise AuthError("AUTH_CODE_INVALID", "Code is invalid or expired", 401)
        return await self.repository.find_user_by_email_hash(email_hash)

    @staticmethod
    def _validated_consents(
        consents: list[ConsentInput] | None,
    ) -> tuple[ConsentInput, ...] | None:
        if consents is None:
            return None
        submitted = {
            consent.document_type: consent.document_version
            for consent in consents
            if consent.decision == "accepted"
        }
        if len(consents) != len(REQUIRED_CONSENTS) or submitted != REQUIRED_CONSENTS:
            return None
        return tuple(
            ConsentInput(
                document_type=document_type,
                document_version=document_version,
                decision="accepted",
            )
            for document_type, document_version in REQUIRED_CONSENTS.items()
        )

    async def _has_required_consents(self, user_id: str) -> bool:
        accepted = {
            consent.document_type: consent.document_version
            for consent in await self.repository.consents_for_user(user_id)
            if consent.decision == "accepted"
        }
        return all(accepted.get(name) == version for name, version in REQUIRED_CONSENTS.items())

    async def _accept_consents(
        self,
        user_id: str,
        consents: tuple[ConsentInput, ...],
    ) -> None:
        now = self.clock.now()
        for consent in consents:
            await self.repository.save_consent(
                ConsentRecord(
                    id=new_id("cns"),
                    owner_user_id=user_id,
                    document_type=consent.document_type,
                    document_version=consent.document_version,
                    decision="accepted",
                    decided_at=now,
                )
            )

    async def verify_email(
        self,
        email: str,
        code: str,
        consents: list[ConsentInput] | None,
    ) -> LoginResult:
        normalized = self.users.normalize_email(email)
        user = await self._verify_code(normalized, code)
        validated_consents = self._validated_consents(consents)
        if user is None:
            if validated_consents is None:
                raise AuthError(
                    "CONSENT_REQUIRED",
                    "Consent is required before first login",
                    403,
                )
            user = await self.users.create_email_user(
                normalized,
                self.clock.now(),
                validated_consents,
            )
        elif not await self._has_required_consents(user.id):
            if validated_consents is None:
                raise AuthError(
                    "CONSENT_REQUIRED",
                    "Current consent is required before login",
                    403,
                )
            await self._accept_consents(user.id, validated_consents)
        if user.status != "active":
            raise AuthError("AUTH_ACCOUNT_INACTIVE", "Account is not active", 403)
        await self.preflight_store.consume_challenge(
            self.users.email_lookup_hash(normalized)
        )
        return await self._create_session(user.id)

    async def register_password(
        self,
        email: str,
        code: str,
        password: str,
        consents: list[ConsentInput] | None,
    ) -> LoginResult:
        normalized = self.users.normalize_email(email)
        user = await self._verify_code(normalized, code)
        validated_consents = self._validated_consents(consents)
        password_hash = self.password_hasher.hash_password(password)
        if user is None:
            if validated_consents is None:
                raise AuthError(
                    "CONSENT_REQUIRED",
                    "Consent is required before registration",
                    403,
                )
            user = await self.users.create_email_user(
                normalized,
                self.clock.now(),
                validated_consents,
                password_hash,
            )
        else:
            if user.status != "active":
                raise AuthError("AUTH_ACCOUNT_INACTIVE", "Account is not active", 403)
            if user.password_hash is not None:
                raise AuthError(
                    "AUTH_ACCOUNT_EXISTS",
                    "An account with this email already has a password",
                    409,
                )
            if not await self._has_required_consents(user.id):
                if validated_consents is None:
                    raise AuthError(
                        "CONSENT_REQUIRED",
                        "Current consent is required before registration",
                        403,
                    )
                await self._accept_consents(user.id, validated_consents)
            if not await self.repository.set_password_if_missing(
                user.id,
                password_hash,
            ):
                raise AuthError(
                    "AUTH_ACCOUNT_EXISTS",
                    "An account with this email already has a password",
                    409,
                )
        await self.preflight_store.consume_challenge(
            self.users.email_lookup_hash(normalized)
        )
        return await self._create_session(user.id)

    async def login_password(
        self,
        email: str,
        password: str,
        ip_address: str,
        consents: list[ConsentInput] | None = None,
    ) -> LoginResult:
        normalized = self.users.normalize_email(email)
        email_hash = self.users.email_lookup_hash(normalized)
        ip_hash = self._hash(ip_address, "ip-rate-limit")
        try:
            await self.preflight_store.check_password_attempt(
                email_hash,
                ip_hash,
                self.clock.now(),
            )
        except AuthPreflightRejected as error:
            raise AuthError(
                "AUTH_RATE_LIMITED",
                "Too many authentication attempts",
                429,
                error.retry_after,
            )
        except AuthPreflightUnavailable:
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                "Authentication provider is unavailable",
                503,
            )
        user = await self.repository.find_user_by_email_hash(email_hash)
        encoded = (
            user.password_hash
            if user is not None and user.password_hash is not None
            else DUMMY_PASSWORD_HASH
        )
        password_matches = self.password_hasher.verify_password(password, encoded)
        if (
            user is None
            or user.password_hash is None
            or not password_matches
            or user.status != "active"
        ):
            raise AuthError(
                "AUTH_CREDENTIALS_INVALID",
                "Email or password is invalid",
                401,
            )
        if not await self._has_required_consents(user.id):
            validated_consents = self._validated_consents(consents)
            if validated_consents is None:
                raise AuthError(
                    "CONSENT_REQUIRED",
                    "Current consent is required before login",
                    403,
                )
            await self._accept_consents(user.id, validated_consents)
        try:
            await self.preflight_store.clear_password_attempts(email_hash, ip_hash)
        except AuthPreflightUnavailable:
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                "Authentication provider is unavailable",
                503,
            )
        return await self._create_session(user.id)

    async def login_wechat(
        self,
        code: str,
        consents: list[ConsentInput] | None = None,
    ) -> LoginResult:
        try:
            subject = await self.wechat_exchange.exchange(code)
        except AuthProviderUnavailable:
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                "Authentication provider is unavailable",
                503,
            )
        if subject is None:
            raise AuthError("AUTH_CODE_INVALID", "WeChat code is invalid", 401)
        subject_hash = self._hash(subject, "wechat-identity")
        user = await self.repository.find_user_by_identity(
            "wechat_miniprogram",
            subject_hash,
        )
        if user is None:
            validated_consents = self._validated_consents(consents)
            if validated_consents is None:
                raise AuthError(
                    "CONSENT_REQUIRED",
                    "Consent is required before first login",
                    403,
                )
            now = self.clock.now()
            user = UserAccount(
                id=new_id("usr"),
                status="active",
                email_encrypted=None,
                email_lookup_hash=None,
                created_at=now,
            )
            identity = IdentityRecord(
                id=new_id("idn"),
                owner_user_id=user.id,
                identity_type="wechat_miniprogram",
                external_subject_hash=subject_hash,
                verified_at=now,
            )
            consent_records = tuple(
                ConsentRecord(
                    id=new_id("cns"),
                    owner_user_id=user.id,
                    document_type=consent.document_type,
                    document_version=consent.document_version,
                    decision="accepted",
                    decided_at=now,
                )
                for consent in validated_consents
            )
            await self.repository.create_user_with_identity_and_consents(
                user,
                identity,
                consent_records,
            )
        elif not await self._has_required_consents(user.id):
            validated_consents = self._validated_consents(consents)
            if validated_consents is None:
                raise AuthError(
                    "CONSENT_REQUIRED",
                    "Current consent is required before login",
                    403,
                )
            await self._accept_consents(user.id, validated_consents)
        if user.status != "active":
            raise AuthError("AUTH_ACCOUNT_INACTIVE", "Account is not active", 403)
        return await self._create_session(user.id)

    async def register_wechat_identity(self, subject: str, status: str = "active") -> str:
        now = self.clock.now()
        user = UserAccount(
            id=new_id("usr"),
            status=status,
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
        await self.repository.save_user(user)
        await self.repository.save_identity(
            IdentityRecord(
                id=new_id("idn"),
                owner_user_id=user.id,
                identity_type="wechat_miniprogram",
                external_subject_hash=self._hash(subject, "wechat-identity"),
                verified_at=now,
            )
        )
        await self._accept_consents(
            user.id,
            tuple(
                ConsentInput(
                    document_type=document_type,
                    document_version=document_version,
                    decision="accepted",
                )
                for document_type, document_version in REQUIRED_CONSENTS.items()
            ),
        )
        return user.id

    async def _create_session(
        self,
        user_id: str,
        authenticated_at: datetime | None = None,
    ) -> LoginResult:
        now = self.clock.now()
        raw_token = self.token_factory()
        token_hash = self._hash(raw_token, "session")
        session = SessionRecord(
            id=new_id("ses"),
            owner_user_id=user_id,
            token_hash=token_hash,
            authenticated_at=authenticated_at or now,
            expires_at=now + SESSION_TTL,
        )
        await self.repository.save_session(session)
        return LoginResult(user_id, raw_token, session.expires_at)

    async def authenticate(self, raw_token: str | None) -> AuthenticatedSession | None:
        if not raw_token:
            return None
        session = await self.repository.find_session(self._hash(raw_token, "session"))
        now = self.clock.now()
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            return None
        user = await self.repository.find_user(session.owner_user_id)
        if user is None or user.status != "active":
            return None
        return AuthenticatedSession(
            user_id=user.id,
            session_id=session.id,
            authenticated_at=session.authenticated_at,
            expires_at=session.expires_at,
        )

    async def identify_deletion_replay(
        self,
        raw_token: str | None,
    ) -> AuthenticatedSession | None:
        if not raw_token:
            return None
        session = await self.repository.find_session(self._hash(raw_token, "session"))
        if session is None or session.expires_at <= self.clock.now():
            return None
        return AuthenticatedSession(
            user_id=session.owner_user_id,
            session_id=session.id,
            authenticated_at=session.authenticated_at,
            expires_at=session.expires_at,
        )

    async def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        session = await self.repository.find_session(self._hash(raw_token, "session"))
        if session and session.revoked_at is None:
            session.revoked_at = self.clock.now()
            await self.repository.save_session(session)

    async def refresh(self, raw_token: str | None) -> LoginResult:
        authenticated = await self.authenticate(raw_token)
        if authenticated is None:
            raise AuthError("AUTH_REQUIRED", "Authentication required", 401)
        await self.logout(raw_token)
        return await self._create_session(
            authenticated.user_id,
            authenticated_at=authenticated.authenticated_at,
        )

    async def bind_email(
        self,
        authenticated: AuthenticatedSession,
        email: str,
        code: str,
        confirm_merge: bool = False,
    ) -> None:
        normalized = self.users.normalize_email(email)
        existing = await self._verify_code(normalized, code)
        user = await self.repository.find_user(authenticated.user_id)
        if user is None:
            raise AuthError("AUTH_REQUIRED", "Authentication required", 401)
        if existing and existing.id != authenticated.user_id:
            if not confirm_merge:
                raise AuthError(
                    "AUTH_MERGE_CONFIRMATION_REQUIRED",
                    "Confirm merging the current account into the email account",
                    409,
                    details={
                        "canonical_account": {
                            "user_id": existing.id,
                            "has_email": existing.email_lookup_hash is not None,
                        },
                        "current_account": {
                            "user_id": user.id,
                            "has_email": user.email_lookup_hash is not None,
                        },
                    },
                )
            await self.repository.merge_users(authenticated.user_id, existing.id)
            await self.preflight_store.consume_challenge(
                self.users.email_lookup_hash(normalized)
            )
            return
        await self.users.bind_email(user, normalized, self.clock.now())
        await self.preflight_store.consume_challenge(
            self.users.email_lookup_hash(normalized)
        )

    async def revoke_all_sessions(self, user_id: str) -> None:
        await self.repository.revoke_all_sessions(user_id, self.clock.now())

    async def deactivate_user(self, user_id: str) -> None:
        user = await self.repository.find_user(user_id)
        if user is None:
            raise AuthError("AUTH_REQUIRED", "Authentication required", 401)
        user.status = "pending_deletion"
        await self.repository.save_user(user)


def build_default_auth_service(
    app_env: str,
    *,
    repository: AuthRepository | None = None,
    preflight_store: AuthPreflightStore | None = None,
    email_sender: EmailSender | None = None,
    wechat_exchange: WechatExchange | None = None,
    email_crypto: EmailCrypto | None = None,
    keys: KeyProvider | None = None,
    code_factory: Callable[[], str] | None = None,
) -> AuthService:
    return AuthService(
        repository=repository or InMemoryAuthRepository(),
        preflight_store=preflight_store
        or (
            UnavailableAuthPreflightStore()
            if app_env == "production"
            else InMemoryAuthPreflightStore()
        ),
        email_sender=email_sender or UnavailableEmailSender(),
        wechat_exchange=wechat_exchange or UnavailableWechatExchange(),
        email_crypto=email_crypto or EnvelopeEmailCrypto(),
        keys=keys or ProcessKeys(),
        hasher=HmacSecretHasher(),
        clock=SystemClock(),
        code_factory=code_factory
        or (lambda: f"{secrets.randbelow(1_000_000):06d}"),
        token_factory=lambda: secrets.token_urlsafe(32),
        app_env=app_env,
    )
