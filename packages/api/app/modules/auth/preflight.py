import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from redis.asyncio import Redis


OTP_TTL = timedelta(minutes=10)
RESEND_WAIT = timedelta(seconds=60)
IP_RATE_WINDOW = timedelta(minutes=1)
EMAIL_RATE_WINDOW = timedelta(hours=1)
RATE_LIMIT = 5


@dataclass(frozen=True)
class OtpChallenge:
    email_hash: str
    code_hash: str
    sent_at: datetime
    expires_at: datetime


class AuthPreflightRejected(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Authentication preflight rejected")
        self.retry_after = retry_after


class AuthPreflightUnavailable(Exception):
    pass


class AuthPreflightStore(Protocol):
    async def issue(
        self,
        challenge: OtpChallenge,
        ip_hash: str,
        now: datetime,
    ) -> None: ...

    async def get_challenge(
        self,
        email_hash: str,
        now: datetime,
    ) -> OtpChallenge | None: ...

    async def consume_challenge(self, email_hash: str) -> None: ...


class InMemoryAuthPreflightBackend:
    def __init__(self) -> None:
        self.challenges: dict[str, OtpChallenge] = {}
        self.rate_events: dict[tuple[str, str], list[datetime]] = {}
        self.lock = asyncio.Lock()


class InMemoryAuthPreflightStore:
    def __init__(
        self,
        backend: InMemoryAuthPreflightBackend | None = None,
    ) -> None:
        self.backend = backend or InMemoryAuthPreflightBackend()

    @staticmethod
    def _retry_after(
        events: list[datetime],
        now: datetime,
        window: timedelta,
    ) -> int:
        return max(1, int((events[0] + window - now).total_seconds()))

    async def issue(
        self,
        challenge: OtpChallenge,
        ip_hash: str,
        now: datetime,
    ) -> None:
        async with self.backend.lock:
            previous = self.backend.challenges.get(challenge.email_hash)
            if previous is not None and previous.expires_at <= now:
                self.backend.challenges.pop(challenge.email_hash, None)
                previous = None
            if previous is not None and previous.sent_at + RESEND_WAIT > now:
                raise AuthPreflightRejected(
                    max(
                        1,
                        int(
                            (
                                previous.sent_at + RESEND_WAIT - now
                            ).total_seconds()
                        ),
                    )
                )

            ip_events = self.backend.rate_events.setdefault(("ip", ip_hash), [])
            email_events = self.backend.rate_events.setdefault(
                ("email", challenge.email_hash),
                [],
            )
            ip_events[:] = [
                event for event in ip_events if event > now - IP_RATE_WINDOW
            ]
            email_events[:] = [
                event for event in email_events if event > now - EMAIL_RATE_WINDOW
            ]
            if len(ip_events) >= RATE_LIMIT:
                raise AuthPreflightRejected(
                    self._retry_after(ip_events, now, IP_RATE_WINDOW)
                )
            if len(email_events) >= RATE_LIMIT:
                raise AuthPreflightRejected(
                    self._retry_after(email_events, now, EMAIL_RATE_WINDOW)
                )

            ip_events.append(now)
            email_events.append(now)
            self.backend.challenges[challenge.email_hash] = challenge

    async def get_challenge(
        self,
        email_hash: str,
        now: datetime,
    ) -> OtpChallenge | None:
        async with self.backend.lock:
            challenge = self.backend.challenges.get(email_hash)
            if challenge is not None and challenge.expires_at <= now:
                self.backend.challenges.pop(email_hash, None)
                return None
            return challenge

    async def consume_challenge(self, email_hash: str) -> None:
        async with self.backend.lock:
            self.backend.challenges.pop(email_hash, None)


class UnavailableAuthPreflightStore:
    async def issue(
        self,
        challenge: OtpChallenge,
        ip_hash: str,
        now: datetime,
    ) -> None:
        raise AuthPreflightUnavailable

    async def get_challenge(
        self,
        email_hash: str,
        now: datetime,
    ) -> OtpChallenge | None:
        raise AuthPreflightUnavailable

    async def consume_challenge(self, email_hash: str) -> None:
        raise AuthPreflightUnavailable


class RedisAuthPreflightStore:
    _ISSUE_SCRIPT = """
local now = tonumber(ARGV[1])
local ip_window = tonumber(ARGV[2])
local email_window = tonumber(ARGV[3])
local challenge_ttl = tonumber(ARGV[4])
local resend_ttl = tonumber(ARGV[5])

local resend_remaining = redis.call('PTTL', KEYS[2])
if resend_remaining > 0 then
  return {1, resend_remaining}
end

redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now - ip_window)
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now - email_window)

if redis.call('ZCARD', KEYS[3]) >= 5 then
  local oldest = redis.call('ZRANGE', KEYS[3], 0, 0, 'WITHSCORES')
  return {2, tonumber(oldest[2]) + ip_window - now}
end
if redis.call('ZCARD', KEYS[4]) >= 5 then
  local oldest = redis.call('ZRANGE', KEYS[4], 0, 0, 'WITHSCORES')
  return {3, tonumber(oldest[2]) + email_window - now}
end

local sequence = redis.call('INCR', KEYS[5])
local member = tostring(now) .. ':' .. tostring(sequence)
redis.call('SET', KEYS[1], ARGV[6], 'PX', challenge_ttl)
redis.call('SET', KEYS[2], '1', 'PX', resend_ttl)
redis.call('ZADD', KEYS[3], now, member)
redis.call('ZADD', KEYS[4], now, member)
redis.call('PEXPIRE', KEYS[3], ip_window)
redis.call('PEXPIRE', KEYS[4], email_window)
redis.call('PEXPIRE', KEYS[5], email_window)
return {0, 0}
"""

    def __init__(self, client: Redis, prefix: str = "auth-preflight") -> None:
        self.client = client
        self.prefix = prefix

    def _key(self, kind: str, value: str) -> str:
        return f"{self.prefix}:{kind}:{value}"

    @staticmethod
    def _payload(challenge: OtpChallenge) -> str:
        return json.dumps(
            {
                "email_hash": challenge.email_hash,
                "code_hash": challenge.code_hash,
                "sent_at": challenge.sent_at.isoformat(),
                "expires_at": challenge.expires_at.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    async def issue(
        self,
        challenge: OtpChallenge,
        ip_hash: str,
        now: datetime,
    ) -> None:
        now_ms = int(now.timestamp() * 1000)
        result = await self.client.eval(
            self._ISSUE_SCRIPT,
            5,
            self._key("challenge", challenge.email_hash),
            self._key("resend", challenge.email_hash),
            self._key("rate-ip", ip_hash),
            self._key("rate-email", challenge.email_hash),
            self._key("sequence", "global"),
            now_ms,
            int(IP_RATE_WINDOW.total_seconds() * 1000),
            int(EMAIL_RATE_WINDOW.total_seconds() * 1000),
            max(1, int((challenge.expires_at - now).total_seconds() * 1000)),
            int(RESEND_WAIT.total_seconds() * 1000),
            self._payload(challenge),
        )
        if int(result[0]) != 0:
            raise AuthPreflightRejected(max(1, (int(result[1]) + 999) // 1000))

    async def get_challenge(
        self,
        email_hash: str,
        now: datetime,
    ) -> OtpChallenge | None:
        value = await self.client.get(self._key("challenge", email_hash))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        payload = json.loads(value)
        challenge = OtpChallenge(
            email_hash=payload["email_hash"],
            code_hash=payload["code_hash"],
            sent_at=datetime.fromisoformat(payload["sent_at"]).astimezone(
                timezone.utc
            ),
            expires_at=datetime.fromisoformat(payload["expires_at"]).astimezone(
                timezone.utc
            ),
        )
        if challenge.expires_at <= now:
            await self.consume_challenge(email_hash)
            return None
        return challenge

    async def consume_challenge(self, email_hash: str) -> None:
        await self.client.getdel(self._key("challenge", email_hash))
