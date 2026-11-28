"""Cache-aside orchestration; MongoDB remains the source of truth."""
from time import perf_counter
from redis.exceptions import RedisError


class CachedDossiers:
    def __init__(self, controller, cache=None, ttl_seconds=300):
        if ttl_seconds < 1:
            raise ValueError("CACHE_TTL_SECONDS must be positive")
        self.controller = controller
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    def get(self, player_id, *, appearances_limit=20, appearances_offset=0):
        started = perf_counter()
        page = dict(appearances_limit=appearances_limit, appearances_offset=appearances_offset)
        state = "disabled" if self.cache is None else "miss"
        result = None
        if self.cache is not None:
            try:
                result = self.cache.get_cached_dossier(player_id, **page)
                if result is not None:
                    state = "hit"
            except (RedisError, ValueError, TypeError):
                state = "unavailable"
        if result is None:
            result = self.controller.get_player(player_id, **page)
            if result.get("status") == "success":
                total = result["data"]["total_matches"]
                result["pagination"] = {
                    "offset": appearances_offset, "limit": appearances_limit,
                    "total": total, "has_more": appearances_offset + appearances_limit < total,
                }
                if self.cache is not None and state != "unavailable":
                    try:
                        self.cache.set_cached_dossier(player_id, result, self.ttl_seconds, **page)
                    except (RedisError, ValueError, TypeError):
                        state = "unavailable"
        result = dict(result)
        result["cache"] = {
            "status": state, "source": "Redis" if state == "hit" else "MongoDB",
            "ttl_seconds": self.ttl_seconds if state in {"hit", "miss"} else None,
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
        return result
