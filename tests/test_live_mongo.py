"""Optional read-only integration checks against the imported Transfermarkt dataset.

Run explicitly with STAGE12_TEST_MONGO_URI and STAGE12_TEST_DB set.
No collections or documents are modified by these checks.
"""
import os
import unittest

from pymongo import MongoClient

from command_service.adapters.outbound.mongo_adapter import MongoAdapter
from web_app import create_app


@unittest.skipUnless(os.getenv("STAGE12_TEST_MONGO_URI"), "Live MongoDB checks are opt-in")
class LiveMongoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoClient(os.environ["STAGE12_TEST_MONGO_URI"], serverSelectionTimeoutMS=5000)
        cls.db = cls.mongo[os.environ.get("STAGE12_TEST_DB", "football_scouting")]
        cls.mongo.admin.command("ping")
        cls.web = create_app(MongoAdapter(cls.db)).test_client()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.close()

    def test_live_search_and_dossier_match_stored_data(self):
        stored = self.db.players.find_one({"player_id": 10})
        self.assertIsNotNone(stored, "Import the supplied Transfermarkt dataset before this check")
        result = self.web.get("/api/players?name=Klose")
        self.assertEqual(result.status_code, 200)
        self.assertIn(10, [p["player_id"] for p in result.json["data"]])
        result = self.web.get("/api/players/10")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["data"]["name"], stored["name"])
        self.assertEqual(result.json["data"]["total_matches"], len(stored["appearances"]))

    def test_live_appearance_pages_cover_stored_history(self):
        stored = self.db.players.find_one({"player_id": 10})
        actual = []
        for offset in range(0, len(stored["appearances"]), 20):
            response = self.web.get(f"/api/players/10?appearances_offset={offset}")
            self.assertEqual(response.status_code, 200)
            page = response.json
            self.assertEqual(page["pagination"]["has_more"], offset + 20 < len(stored["appearances"]))
            actual.extend(a["appearance_id"] for a in page["data"]["appearances"])
        self.assertEqual(actual, [a["appearance_id"] for a in stored["appearances"]])

    def test_live_filter_and_catalog_pages(self):
        params = "position=Attack&max_market_value_eur=15000000"
        first = self.web.get("/api/players?" + params).json
        second = self.web.get("/api/players?" + params + "&offset=20").json
        self.assertTrue(first["pagination"]["has_more"])
        self.assertTrue(first["data"])
        self.assertTrue(second["data"])
        ids = [p["player_id"] for p in first["data"] + second["data"]]
        self.assertEqual(len(ids), len(set(ids)))
        for player in first["data"] + second["data"]:
            self.assertEqual(player["position"], "Attack")
            self.assertIsNotNone(player["market_value_eur"])
            self.assertLessEqual(player["market_value_eur"], 15000000)
