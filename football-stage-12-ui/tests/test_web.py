"""HTTP -> real Controller -> real Service -> real MongoAdapter -> in-memory Mongo mock.

These tests do not prove replica-set connectivity; run the manual MongoDB check too.
"""
import unittest
from datetime import datetime
from unittest.mock import patch

import mongomock

from command_service.adapters.outbound.mongo_adapter import MongoAdapter
from web_app import create_app


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().football_scouting
        self.db.players.insert_many([
            {"player_id": i, "name": "A.* literal" if i == 1 else f"Player {i:02}",
             "position": "Attack" if i <= 24 else "Defender",
             "market_value_eur": None if i == 27 else 1000,
             "total_matches": 1, "current_club_id": 398,
             "appearances": [{"appearance_id": str(i), "date": datetime(2025, 5, 10),
                              "competition_id": "IT1", "goals": 1}]}
            for i in range(1, 28)
        ])
        self.db.clubs.insert_one({"club_id": 398, "club_name": "Lazio"})
        self.db.competitions.insert_one({"competition_id": "IT1", "competition_name": "Serie A"})
        self.repository = MongoAdapter(self.db)
        self.app = create_app(self.repository)
        self.client = self.app.test_client()

    def test_page_and_static_assets(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('lang="uk"', response.text)
        for url in ("/static/app.js", "/static/app.css"):
            with self.client.get(url) as asset:
                self.assertEqual(asset.status_code, 200)

    def test_pagination_stable_with_equal_sort_values(self):
        first = self.client.get("/api/players?position=Attack").json
        second = self.client.get("/api/players?position=Attack&offset=20").json
        self.assertTrue(first["pagination"]["has_more"])
        self.assertFalse(second["pagination"]["has_more"])
        self.assertEqual([p["player_id"] for p in first["data"] + second["data"]], list(range(1, 25)))
        self.assertNotIn("appearances", first["data"][0])

    def test_name_search_is_case_insensitive_and_literal(self):
        result = self.client.get("/api/players", query_string={"name": "a.*"}).json
        self.assertEqual([p["player_id"] for p in result["data"]], [1])

    def test_price_filter_excludes_null(self):
        result = self.client.get("/api/players?max_market_value_eur=1000&limit=100").json
        self.assertEqual(result["count"], 26)

    def test_empty_results_and_missing_player(self):
        self.assertEqual(self.client.get("/api/players?name=missing").json["data"], [])
        response = self.client.get("/api/players/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["status"], "not_found")

    def test_dossier_date_reference_and_graceful_graph_degradation(self):
        response = self.client.get("/api/players/1?appearances_limit=1")
        self.assertEqual(response.status_code, 200)
        dossier = response.json["data"]
        self.assertEqual(dossier["appearances"][0]["date"], "2025-05-10T00:00:00")
        self.assertEqual(dossier["club"]["club_name"], "Lazio")
        self.assertEqual(dossier["competitions"]["IT1"]["competition_name"], "Serie A")
        self.assertIn("tactical_analogs_unavailable", dossier["warnings"])

    def test_invalid_input(self):
        for query in ("limit=201", "offset=-1", "limit=1.5", "min_market_value_eur=nan",
                      "max_market_value_eur=inf", "min_avg_score=11", "sort_by=$where",
                      "sort_order=0", "name=" + "a" * 101,
                      "min_market_value_eur=100&max_market_value_eur=10",
                      "unknown=1", "limit=1&limit=2", "current_club_id=0"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/players?" + query).status_code, 400)
        self.assertEqual(self.client.get("/api/players/1?appearances_limit=0").status_code, 400)
        self.assertEqual(self.client.get("/api/players/0").status_code, 400)
        self.assertEqual(self.client.get("/api/players/1?appearances_offset=-1").status_code, 400)

    def test_appearance_pages_cover_all_records(self):
        apps = [{"appearance_id": str(i), "date": datetime(2025, 5, 10), "goals": i} for i in range(45)]
        self.db.players.update_one({"player_id": 1}, {"$set": {"appearances": apps, "total_matches": 45}})
        seen = []
        for offset, expected_more, expected_length in ((0, True, 20), (20, True, 20), (40, False, 5)):
            response = self.client.get(f"/api/players/1?appearances_offset={offset}")
            self.assertEqual(response.status_code, 200)
            page = response.json
            self.assertEqual(page["pagination"]["has_more"], expected_more)
            self.assertEqual(page["pagination"]["total"], 45)
            self.assertEqual(len(page["data"]["appearances"]), expected_length)
            seen.extend(a["appearance_id"] for a in page["data"]["appearances"])
        self.assertEqual(seen, [str(i) for i in range(45)])

    def test_database_failure_does_not_leak_exception(self):
        with patch.object(self.repository, "filter_players", side_effect=RuntimeError("secret URI")):
            response = self.client.get("/api/players")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret", response.text)
        with patch.object(self.repository, "get_player_with_appearances", side_effect=RuntimeError("secret URI")):
            self.assertEqual(self.client.get("/api/players/1").status_code, 503)

    def test_read_only_web_does_not_write_telemetry(self):
        self.client.get("/api/players")
        self.client.get("/api/players/1")
        self.assertEqual(self.db.search_queries.count_documents({}), 0)
        self.assertEqual(self.client.post("/api/players", json={}).status_code, 405)


if __name__ == "__main__":
    unittest.main()
