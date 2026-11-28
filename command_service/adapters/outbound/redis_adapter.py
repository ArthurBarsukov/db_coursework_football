"""Redis storage for paginated dossiers."""
import json
import os
from datetime import date, datetime
from redis import Redis
from command_service.ports.output import CacheStorePort


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported cache value: {type(value).__name__}")


class RedisAdapter(CacheStorePort):
    def __init__(self, redis_client=None, namespace="football:dossier:v1"):
        self._redis_client = redis_client if redis_client is not None else Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_connect_timeout=0.3, socket_timeout=0.3, decode_responses=True,
        )
        self.namespace = namespace

    def _key(self, player_id, appearances_limit, appearances_offset):
        return f"{self.namespace}:{player_id}:{appearances_limit}:{appearances_offset}"

    def get_cached_dossier(self, player_id, *, appearances_limit=20, appearances_offset=0):
        raw = self._redis_client.get(self._key(player_id, appearances_limit, appearances_offset))
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("status") != "success" or not isinstance(value.get("data"), dict) or not isinstance(value.get("pagination"), dict):
            raise ValueError("Invalid cached dossier")
        return value

    def set_cached_dossier(self, player_id, data, ttl_seconds=300, *, appearances_limit=20, appearances_offset=0):
        self._redis_client.set(
            self._key(player_id, appearances_limit, appearances_offset),
            json.dumps(data, ensure_ascii=False, default=_json_default), ex=ttl_seconds,
        )
