from __future__ import annotations

import argparse

from pymongo import ASCENDING, DESCENDING

from command_service.config import (
    create_mongo_client,
    get_critical_collection,
    get_database,
)


def setup_pre_indexes() -> None:
    client = create_mongo_client()
    try:
        db = get_database(client)
        players = get_critical_collection(db, "players")
        clubs = get_critical_collection(db, "clubs")
        competitions = get_critical_collection(db, "competitions")

        players.create_index(
            [("player_id", ASCENDING)],
            unique=True,
            name="idx_players_player_id_unique",
        )
        clubs.create_index(
            [("club_id", ASCENDING)],
            unique=True,
            name="idx_clubs_club_id_unique",
        )
        competitions.create_index(
            [("competition_id", ASCENDING)],
            unique=True,
            name="idx_competitions_competition_id_unique",
        )
        print("[PRE] Унікальні індекси для players, clubs та competitions успішно створено.")
    finally:
        client.close()


def setup_post_indexes() -> None:
    client = create_mongo_client()
    try:
        db = get_database(client)
        players = get_critical_collection(db, "players")
        search_queries = get_critical_collection(db, "search_queries")

        # Складений індекс для параметричної фільтрації (правило ESR: position -> sub_position -> market_value_eur)
        players.create_index(
            [
                ("position", ASCENDING),
                ("sub_position", ASCENDING),
                ("market_value_eur", DESCENDING),
            ],
            name="idx_players_pos_subpos_value",
        )

        # Індекс для вибірки складу клубу (категорія 3) та підрахунку players_count
        players.create_index(
            [("current_club_id", ASCENDING)],
            name="idx_players_current_club_id",
        )

        # Перебудова складеного multikey-індексу для $elemMatch:
        # спочатку найвідсівніша рівністна умова (competition_id), далі діапазони (minutes_played, goals)
        elem_match_index_name = "idx_players_appearances_elem_match"
        desired_elem_match_keys = [
            ("appearances.competition_id", ASCENDING),
            ("appearances.minutes_played", ASCENDING),
            ("appearances.goals", ASCENDING),
        ]
        existing_indexes = players.index_information()
        if elem_match_index_name in existing_indexes:
            existing_keys = list(existing_indexes[elem_match_index_name].get("key", []))
            if existing_keys != desired_elem_match_keys:
                players.drop_index(elem_match_index_name)
                print(f"[POST] Видалено застарілий індекс '{elem_match_index_name}': {existing_keys}")

        players.create_index(
            desired_elem_match_keys,
            name=elem_match_index_name,
        )

        # Індекс для сортування телеметрії за часом
        search_queries.create_index(
            [("timestamp", DESCENDING)],
            name="idx_search_queries_timestamp_desc",
        )
        print("[POST] Аналітичні, клубні, multikey та телеметричні індекси успішно створено.")
    finally:
        client.close()


def print_indexes_summary() -> None:
    client = create_mongo_client()
    try:
        db = get_database(client)
        for col_name in ("players", "clubs", "competitions", "search_queries"):
            indexes = list(db[col_name].list_indexes())
            print(f"\nКолекція '{col_name}' ({len(indexes)} індексів):")
            for idx in indexes:
                print(f"  - {idx['name']}: {dict(idx['key'])} (unique={idx.get('unique', False)})")
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Налаштування індексів MongoDB")
    parser.add_argument(
        "--stage",
        choices=["pre", "post"],
        required=True,
        help="Етап створення індексів: pre (до ETL) або post (після ETL)",
    )
    args = parser.parse_args()

    if args.stage == "pre":
        setup_pre_indexes()
    else:
        setup_post_indexes()

    print_indexes_summary()


if __name__ == "__main__":
    main()