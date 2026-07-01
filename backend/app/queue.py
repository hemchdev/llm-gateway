from redis.asyncio import Redis

from .config import Settings


def get_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def enqueue_request(redis: Redis, queue_name: str, payload: str) -> int:
    return await redis.rpush(queue_name, payload)
