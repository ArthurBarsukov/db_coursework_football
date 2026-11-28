from __future__ import annotations

from command_service.adapters.inbound.controllers import (
    AppearanceCommandController,
    PlayerQueryController,
)
from command_service.adapters.outbound.elastic_adapter import ElasticsearchAdapter
from command_service.adapters.outbound.kafka_adapter import KafkaPublisherAdapter
from command_service.adapters.outbound.mongo_adapter import MongoAdapter
from command_service.adapters.outbound.neo4j_adapter import Neo4jAdapter
from command_service.adapters.outbound.redis_adapter import RedisAdapter
from command_service.application.services import (
    AnalyticsService,
    AppearanceService,
    PlayerFacade,
    PlayerProfileService,
    ReferenceService,
    ScoutingService,
    TelemetryService,
)
from command_service.config import create_mongo_client, get_database
from command_service.dto import PlayerFilterCriteria


def main() -> None:
    print("--- 1. Ініціалізація інфраструктури (Dependency Injection) ---")
    mongo_client = create_mongo_client()
    try:
        db = get_database(mongo_client)
        mongo_adapter = MongoAdapter(db)

        # Заглушки суміжних підсистем (заплановані на наступні етапи)
        neo4j_db = Neo4jAdapter()
        redis_cache = RedisAdapter()
        elastic_search = ElasticsearchAdapter()
        kafka_queue = KafkaPublisherAdapter()
        _ = (redis_cache, elastic_search, kafka_queue)

        print("--- 2. Збірка прикладного ядра ---")
        profile_service = PlayerProfileService(
            repository=mongo_adapter,
            telemetry_repository=mongo_adapter,
        )
        appearance_service = AppearanceService(repository=mongo_adapter)
        scouting_service = ScoutingService(graph_repository=neo4j_db)
        reference_service = ReferenceService(reference_repository=mongo_adapter)
        analytics_service = AnalyticsService(
            analytics_repository=mongo_adapter,
            telemetry_repository=mongo_adapter,
        )
        telemetry_service = TelemetryService(telemetry_repository=mongo_adapter)

        facade = PlayerFacade(
            profile_service=profile_service,
            appearance_service=appearance_service,
            scouting_service=scouting_service,
            reference_repository=mongo_adapter,
            telemetry_repository=mongo_adapter,
        )

        print("--- 3. Ініціалізація вхідних контролерів ---")
        query_controller = PlayerQueryController(
            player_facade=facade,
            profile_service=profile_service,
            reference_service=reference_service,
            analytics_service=analytics_service,
            telemetry_service=telemetry_service,
        )
        command_controller = AppearanceCommandController(
            appearance_service=appearance_service
        )

        print("--- 4. Перевірка скаутського досьє (категорія 3) ---")
        target_player_id = 10  # Miroslav Klose
        dossier_resp = query_controller.get_player(
            player_id=target_player_id,
            appearances_limit=3,
        )
        print("Статус досьє:", dossier_resp["status"])
        if dossier_resp["status"] == "success":
            data = dossier_resp["data"]
            club_name = data["club"]["club_name"] if data.get("club") else None
            print(
                f"Гравець: {data['name']} (ID={data['player_id']}), "
                f"позиція={data['position']}, клуб={club_name}"
            )
            print(
                f"Кар'єра: матчів={data['total_matches']}, голів={data['total_goals']}, "
                f"середній рейтинг={data['avg_performance_score']}"
            )
            print(
                f"Останні виступи ({len(data['appearances'])}): "
                f"{[str(a['date'].date()) for a in data['appearances']]}"
            )
            print("Попередження деградації:", data["warnings"])

        print("\n--- 5. Перевірка атомарного запису виступу ---")
        new_appearance_payload = {
            "appearance_id": "DEMO_STAGE3_APP_1",
            "player_id": target_player_id,
            "game_id": 9999001,
            "player_club_id": 398,
            "competition_id": "IT1",
            "date": "2026-05-20T18:30:00",
            "goals": 2,
            "assists": 1,
            "minutes_played": 90,
            "yellow_cards": 0,
            "red_cards": 0,
        }

        res_saved = command_controller.record_match_appearance(new_appearance_payload)
        print("1) Запис нового виступу:", res_saved)

        after_save = query_controller.get_player(
            player_id=target_player_id,
            appearances_limit=1,
            consistent_read=True,
        )
        if after_save["status"] == "success":
            updated = after_save["data"]
            print(
                f"   Після запису (consistent_read=True): "
                f"total_matches={updated['total_matches']}, "
                f"total_goals={updated['total_goals']}, "
                f"avg_performance_score={updated['avg_performance_score']}"
            )

        res_dup = command_controller.record_match_appearance(new_appearance_payload)
        print("2) Повторний запис того самого виступу:", res_dup)

        res_missing = command_controller.record_match_appearance(
            {**new_appearance_payload, "appearance_id": "DEMO_STAGE3_APP_2", "player_id": 999999999}
        )
        print("3) Запис для неіснуючого гравця:", res_missing)

        print("\n--- 6. Перевірка фільтрації та аналітики ---")
        filtered = query_controller.filter_players(
            PlayerFilterCriteria(
                position="Attack",
                sub_position="Centre-Forward",
                max_market_value_eur=15_000_000,
                limit=3,
            )
        )
        print(
            "Фільтрація нападників (<= 15M EUR):",
            [(p["name"], p["market_value_eur"]) for p in filtered.get("data", [])],
        )

        top_comps = query_controller.get_top_competitions(limit=3, min_matches=100)
        print(
            "Топ-3 турніри за голами за матч:",
            [
                (c["competition_id"], c["goals_per_match"])
                for c in top_comps.get("data", [])
            ],
        )
    finally:
        mongo_client.close()
        print("\n--- Клієнт MongoDB закрито ---")


if __name__ == "__main__":
    main()