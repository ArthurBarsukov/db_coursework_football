"""HTTP -> controller -> Mongo mock, with Redis protocol emulation."""
import unittest
from unittest.mock import patch
from datetime import datetime

import fakeredis
import mongomock
from redis.exceptions import ConnectionError

from command_service.adapters.outbound.mongo_adapter import MongoAdapter
from command_service.adapters.outbound.redis_adapter import RedisAdapter
from web_app import create_app


class CacheFlowTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().football_scouting
        self.db.players.insert_one({
            "player_id": 1, "name": "Гравець", "total_matches": 3,
            "appearances": [{"appearance_id": str(i), "date": datetime(2025, 5, 10), "goals": i} for i in range(3)],
        })
        self.repository = MongoAdapter(self.db)
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.adapter = RedisAdapter(self.redis)
        self.app = create_app(self.repository, self.adapter)
        self.client = self.app.test_client()

    def test_hit_bypasses_mongo_preserves_dates_and_sets_ttl(self):
        first = self.client.get("/api/players/1").json
        self.assertEqual(first["cache"]["status"], "miss")
        key, = self.redis.keys()
        self.assertGreater(self.redis.ttl(key), 0)
        self.assertLessEqual(self.redis.ttl(key), 300)
        self.assertNotIn("cache", self.redis.get(key))
        with patch.object(self.repository, "get_player_with_appearances", side_effect=AssertionError("Mongo must not be queried")):
            second = self.client.get("/api/players/1").json
        self.assertEqual(second["cache"]["status"], "hit")
        self.assertEqual(second["cache"]["source"], "Redis")
        self.assertEqual(first["data"], second["data"])
        self.assertEqual(first["pagination"], second["pagination"])
        self.assertEqual(second["data"]["appearances"][0]["date"], "2025-05-10T00:00:00")

    def test_pagination_and_namespace_isolation(self):
        paths = ["/api/players/1?appearances_limit=1", "/api/players/1?appearances_limit=1&appearances_offset=1", "/api/players/1?appearances_limit=2"]
        pages = [self.client.get(path).json for path in paths]
        self.assertTrue(all(page["cache"]["status"] == "miss" for page in pages))
        self.assertEqual(len(self.redis.keys()), 3)
        self.assertNotEqual(pages[0]["data"]["appearances"], pages[1]["data"]["appearances"])
        for path, page in zip(paths, pages):
            result = self.client.get(path).json
            self.assertEqual(result["cache"]["status"], "hit")
            self.assertEqual(page["data"], result["data"])
        other = RedisAdapter(self.redis, "another-dataset")
        self.assertIsNone(other.get_cached_dossier(1, appearances_limit=1))

    def test_expired_entry_reads_updated_mongo(self):
        self.client.get("/api/players/1")
        self.db.players.update_one({"player_id": 1}, {"$set": {"name": "Оновлено"}})
        self.assertEqual(self.client.get("/api/players/1").json["data"]["name"], "Гравець")
        # Expire through Redis command without a blocking sleep.
        key, = self.redis.keys()
        self.redis.expire(key, -1)
        result = self.client.get("/api/players/1").json
        self.assertEqual(result["cache"]["status"], "miss")
        self.assertEqual(result["data"]["name"], "Оновлено")

    def test_read_and_write_outages_fall_back(self):
        for method in ("get", "set"):
            with self.subTest(method=method), patch.object(self.redis, method, side_effect=ConnectionError("secret URI")):
                response = self.client.get("/api/players/1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["cache"]["status"], "unavailable")
                self.assertEqual(response.json["cache"]["source"], "MongoDB")
                self.assertNotIn("secret", response.text)

    def test_corrupt_cache_falls_back(self):
        key = "football:dossier:v1:1:20:0"
        for payload in ("bad json", "[]", '{"status":"success","data":{}}'):
            self.redis.set(key, payload)
            response = self.client.get("/api/players/1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["cache"]["status"], "unavailable")
            self.assertEqual(response.json["data"]["player_id"], 1)

    def test_errors_and_invalid_requests_are_not_cached(self):
        self.assertEqual(self.client.get("/api/players/999").status_code, 404)
        with patch.object(self.repository, "get_player_with_appearances", side_effect=RuntimeError("database down")):
            self.assertEqual(self.client.get("/api/players/1").status_code, 503)
        for suffix in ("?appearances_limit=0", "?appearances_offset=-1", "?appearances_limit=1&appearances_limit=2"):
            self.assertEqual(self.client.get("/api/players/1" + suffix).status_code, 400)
        self.assertEqual(self.redis.dbsize(), 0)


if __name__ == "__main__":
    unittest.main()
