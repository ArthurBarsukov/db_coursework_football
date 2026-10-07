from command_service.ports.output import CacheStorePort

class RedisAdapter(CacheStorePort):
    def __init__(self, redis_client=None):
        self._redis_client = redis_client  # TODO: підключення до Redis

    def get_cached_dossier(self, player_id: int) -> dict | None:
        raise NotImplementedError

    def set_cached_dossier(self, player_id: int, data: dict, ttl_seconds: int = 300) -> None:
        raise NotImplementedError