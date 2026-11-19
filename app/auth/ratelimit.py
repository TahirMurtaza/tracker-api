from app.config import settings
from app.errors import AppError
from app.redis import redis

# INCR and EXPIRE in one atomic step, so a crash can never leave a counter without a TTL.
# Redis is the single shared counter, which is what makes the limit hold across API servers.
_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {c, redis.call('TTL', KEYS[1])}
"""


def _key(email: str) -> str:
    return f"login_attempts:{email}"


async def register_attempt(email: str) -> None:
    """Count this attempt; raise 429 once more than the allowed number was made in the window."""
    count, ttl = await redis.eval(_LUA, 1, _key(email), settings.login_window_seconds)
    if count > settings.login_max_attempts:
        retry = ttl if ttl > 0 else settings.login_window_seconds
        raise AppError(429, "rate_limited", "Too many login attempts. Try again later.", headers={"Retry-After": str(retry)})


async def reset(email: str) -> None:
    await redis.delete(_key(email))
