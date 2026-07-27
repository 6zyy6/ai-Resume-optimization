import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.ids import new_id
from app.modules.auth.schemas import ConsentInput
from app.modules.users.service import (
    ConsentRecord,
    EmailCrypto,
    IdentityRecord,
    KeyProvider,
    UserAccount,
    UserService,
)


OTP_TTL = timedelta(minutes=10)
RESEND_WAIT = timedelta(seconds=60)
IP_RATE_WINDOW = timedelta(minutes=1)
EMAIL_RATE_WINDOW = timedelta(hours=1)
SESSION_TTL = timedelta(days=30)


class Clock(Protocol):
    def now(self) -> datetime: ...


class EmailSender(Protocol):
    async def send_otp(self, email: str, code: str) -> None: ...


class WechatExchange(Protocol):
    async def exchange(self, code: str) -> str | None: ...


class SecretHasher(Protocol):
    def hash_secret(self, value: str, key: bytes) -> str: ...


class AuthProviderUnavailable(Exception):
    pass


@dataclass
class OtpChallenge:
    email_hash: str
    code_hash: str
    sent_at: datetime
    expires_at: datetime


@dataclass
class SessionRecord:
    id: str
    owner_user_id: str
    token_hash: str
    authenticated_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class AuthRepository(Protocol):
    def find_user(self, user_id: str) -> UserAccount | None: ...
    def find_user_by_email_hash(self, email_hash: str) -> UserAccount | None: ...
    def find_user_by_identity(
        self,
        identity_type: str,
        subject_hash: str,
    ) -> UserAccount | None: ...
    def save_user(self, user: UserAccount) -> None: ...
    def save_identity(self, identity: IdentityRecord) -> None: ...
    def save_consent(self, consent: ConsentRecord) -> None: ...
    def latest_challenge(self, email_hash: str) -> OtpChallenge | None: ...
    def save_challenge(self, challenge: OtpChallenge) -> None: ...
    def consume_challenge(self, email_hash: str) -> None: ...
    def rate_events(self, bucket: str, key: str) -> list[datetime]: ...
    def save_rate_event(self, bucket: str, key: str, now: datetime) -> None: ...
    def save_session(self, session: SessionRecord) -> None: ...
    def find_session(self, token_hash: str) -> SessionRecord | None: ...
    def revoke_all_sessions(self, user_id: str, now: datetime) -> None: ...


class HmacSecretHasher:
    def hash_secret(self, value: str, key: bytes) -> str:
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


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
        self.challenges: dict[str, OtpChallenge] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self._rate_events: dict[tuple[str, str], list[datetime]] = {}

    def find_user(self, user_id: str) -> UserAccount | None:
        return self.users.get(user_id)

    def find_user_by_email_hash(self, email_hash: str) -> UserAccount | None:
        return next(
            (user for user in self.users.values() if user.email_lookup_hash == email_hash),
            None,
        )

    def find_user_by_identity(
        self,
        identity_type: str,
        subject_hash: str,
    ) -> UserAccount | None:
        identity = self.identities.get((identity_type, subject_hash))
        return self.users.get(identity.owner_user_id) if identity else None

    def save_user(self, user: UserAccount) -> None:
        self.users[user.id] = user

    def save_identity(self, identity: IdentityRecord) -> None:
        self.identities[(identity.identity_type, identity.external_subject_hash)] = identity

    def save_consent(self, consent: ConsentRecord) -> None:
        self.consents.append(consent)

    def latest_challenge(self, email_hash: str) -> OtpChallenge | None:
        return self.challenges.get(email_hash)

    def save_challenge(self, challenge: OtpChallenge) -> None:
        self.challenges[challenge.email_hash] = challenge

    def consume_challenge(self, email_hash: str) -> None:
        self.challenges.pop(email_hash, None)

    def rate_events(self, bucket: str, key: str) -> list[datetime]:
        return self._rate_events.setdefault((bucket, key), [])

    def save_rate_event(self, bucket: str, key: str, now: datetime) -> None:
        self.rate_events(bucket, key).append(now)

    def save_session(self, session: SessionRecord) -> None:
        self.sessions[session.token_hash] = session

    def find_session(self, token_hash: str) -> SessionRecord | None:
        return self.sessions.get(token_hash)

    def revoke_all_sessions(self, user_id: str, now: datetime) -> None:
        for session in self.sessions.values():
            if session.owner_user_id == user_id and session.revoked_at is None:
                session.revoked_at = now


class AuthError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after


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
    return app_env != "test"


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        email_sender: EmailSender,
        wechat_exchange: WechatExchange,
        email_crypto: EmailCrypto,
        keys: KeyProvider,
        hasher: SecretHasher,
        clock: Clock,
        code_factory: Callable[[], str],
        token_factory: Callable[[], str],
        app_env: str,
    ) -> None:
        self.repository = repository
        self.email_sender = email_sender
        self.wechat_exchange = wechat_exchange
        self.keys = keys
        self.hasher = hasher
        self.clock = clock
        self.code_factory = code_factory
        self.token_factory = token_factory
        self.cookie_secure = cookie_secure_for_environment(app_env)
        self.users = UserService(repository, email_crypto, keys)

    def _hash(self, value: str, purpose: str) -> str:
        return self.hasher.hash_secret(value, self.keys.get_key(purpose))

    @staticmethod
    def _retry_after(events: list[datetime], now: datetime, window: timedelta) -> int:
        remaining = int((events[0] + window - now).total_seconds())
        return max(1, remaining)

    def _check_rate(
        self,
        bucket: str,
        key: str,
        now: datetime,
        window: timedelta,
    ) -> None:
        events = self.repository.rate_events(bucket, key)
        events[:] = [event for event in events if event > now - window]
        if len(events) >= 5:
            raise AuthError(
                "AUTH_RATE_LIMITED",
                "Too many authentication attempts",
                429,
                self._retry_after(events, now, window),
            )

    async def start_email(self, email: str, ip_address: str) -> None:
        normalized = self.users.normalize_email(email)
        email_hash = self.users.email_lookup_hash(normalized)
        ip_hash = self._hash(ip_address, "ip-rate-limit")
        now = self.clock.now()
        previous = self.repository.latest_challenge(email_hash)
        if previous and previous.sent_at + RESEND_WAIT > now:
            retry_after = int((previous.sent_at + RESEND_WAIT - now).total_seconds())
            raise AuthError(
                "AUTH_RATE_LIMITED",
                "Wait before requesting another code",
                429,
                max(1, retry_after),
            )
        self._check_rate("ip", ip_hash, now, IP_RATE_WINDOW)
        self._check_rate("email", email_hash, now, EMAIL_RATE_WINDOW)

        code = self.code_factory()
        if len(code) != 6 or not code.isdigit():
            raise RuntimeError("OTP generator must return six digits")
        self.repository.save_rate_event("ip", ip_hash, now)
        self.repository.save_rate_event("email", email_hash, now)
        self.repository.save_challenge(
            OtpChallenge(
                email_hash=email_hash,
                code_hash=self._hash(code, "otp"),
                sent_at=now,
                expires_at=now + OTP_TTL,
            )
        )
        try:
            await self.email_sender.send_otp(normalized, code)
        except AuthProviderUnavailable:
            self.repository.consume_challenge(email_hash)
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                "Authentication provider is unavailable",
                503,
            )

    def _verify_code(self, email: str, code: str) -> UserAccount | None:
        email_hash = self.users.email_lookup_hash(email)
        challenge = self.repository.latest_challenge(email_hash)
        code_hash = self._hash(code, "otp")
        now = self.clock.now()
        if (
            challenge is None
            or challenge.expires_at <= now
            or not hmac.compare_digest(challenge.code_hash, code_hash)
        ):
            raise AuthError("AUTH_CODE_INVALID", "Code is invalid or expired", 401)
        return self.repository.find_user_by_email_hash(email_hash)

    async def verify_email(
        self,
        email: str,
        code: str,
        consent: ConsentInput | None,
    ) -> LoginResult:
        normalized = self.users.normalize_email(email)
        user = self._verify_code(normalized, code)
        if user is None:
            if consent is None or consent.decision != "accepted":
                raise AuthError(
                    "CONSENT_REQUIRED",
                    "Consent is required before first login",
                    403,
                )
            user = self.users.create_email_user(
                normalized,
                self.clock.now(),
                consent.document_type,
                consent.document_version,
                consent.decision,
            )
        if user.status != "active":
            raise AuthError("AUTH_ACCOUNT_INACTIVE", "Account is not active", 403)
        self.repository.consume_challenge(self.users.email_lookup_hash(normalized))
        return self._create_session(user.id)

    async def login_wechat(self, code: str) -> LoginResult:
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
        user = self.repository.find_user_by_identity("wechat_miniprogram", subject_hash)
        if user is None:
            raise AuthError(
                "AUTH_IDENTITY_NOT_FOUND",
                "WeChat identity is not linked to an account",
                404,
            )
        if user.status != "active":
            raise AuthError("AUTH_ACCOUNT_INACTIVE", "Account is not active", 403)
        return self._create_session(user.id)

    def register_wechat_identity(self, subject: str, status: str = "active") -> str:
        now = self.clock.now()
        user = UserAccount(
            id=new_id("usr"),
            status=status,
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
        self.repository.save_user(user)
        self.repository.save_identity(
            IdentityRecord(
                id=new_id("idn"),
                owner_user_id=user.id,
                identity_type="wechat_miniprogram",
                external_subject_hash=self._hash(subject, "wechat-identity"),
                verified_at=now,
            )
        )
        return user.id

    def _create_session(
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
        self.repository.save_session(session)
        return LoginResult(user_id, raw_token, session.expires_at)

    def authenticate(self, raw_token: str | None) -> AuthenticatedSession | None:
        if not raw_token:
            return None
        session = self.repository.find_session(self._hash(raw_token, "session"))
        now = self.clock.now()
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            return None
        user = self.repository.find_user(session.owner_user_id)
        if user is None or user.status != "active":
            return None
        return AuthenticatedSession(
            user_id=user.id,
            session_id=session.id,
            authenticated_at=session.authenticated_at,
            expires_at=session.expires_at,
        )

    def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        session = self.repository.find_session(self._hash(raw_token, "session"))
        if session and session.revoked_at is None:
            session.revoked_at = self.clock.now()

    def refresh(self, raw_token: str | None) -> LoginResult:
        authenticated = self.authenticate(raw_token)
        if authenticated is None:
            raise AuthError("AUTH_REQUIRED", "Authentication required", 401)
        self.logout(raw_token)
        return self._create_session(
            authenticated.user_id,
            authenticated_at=authenticated.authenticated_at,
        )

    def bind_email(
        self,
        authenticated: AuthenticatedSession,
        email: str,
        code: str,
    ) -> None:
        normalized = self.users.normalize_email(email)
        existing = self._verify_code(normalized, code)
        if existing and existing.id != authenticated.user_id:
            raise AuthError("AUTH_IDENTITY_CONFLICT", "Email is already in use", 409)
        user = self.repository.find_user(authenticated.user_id)
        if user is None:
            raise AuthError("AUTH_REQUIRED", "Authentication required", 401)
        self.users.bind_email(user, normalized, self.clock.now())
        self.repository.consume_challenge(self.users.email_lookup_hash(normalized))

    def revoke_all_sessions(self, user_id: str) -> None:
        self.repository.revoke_all_sessions(user_id, self.clock.now())

    def deactivate_user(self, user_id: str) -> None:
        user = self.repository.find_user(user_id)
        if user is None:
            raise AuthError("AUTH_REQUIRED", "Authentication required", 401)
        user.status = "pending_deletion"
        self.repository.save_user(user)


def build_default_auth_service(
    app_env: str,
    *,
    repository: AuthRepository | None = None,
    email_sender: EmailSender | None = None,
    wechat_exchange: WechatExchange | None = None,
    email_crypto: EmailCrypto | None = None,
    keys: KeyProvider | None = None,
) -> AuthService:
    if app_env == "production" and any(
        port is None
        for port in (
            repository,
            email_sender,
            wechat_exchange,
            email_crypto,
            keys,
        )
    ):
        raise RuntimeError("production requires injected auth ports")
    return AuthService(
        repository=repository or InMemoryAuthRepository(),
        email_sender=email_sender or UnavailableEmailSender(),
        wechat_exchange=wechat_exchange or UnavailableWechatExchange(),
        email_crypto=email_crypto or EnvelopeEmailCrypto(),
        keys=keys or ProcessKeys(),
        hasher=HmacSecretHasher(),
        clock=SystemClock(),
        code_factory=lambda: f"{secrets.randbelow(1_000_000):06d}",
        token_factory=lambda: secrets.token_urlsafe(32),
        app_env=app_env,
    )
