from __future__ import annotations

import argparse
import gc
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pymongo.collection import Collection
from pymongo.database import Database

from command_service.config import (
    create_mongo_client,
    get_critical_collection,
    get_database,
)
from command_service.domain import (
    ASSISTS_WEIGHT,
    BASE_SCORE,
    GOALS_WEIGHT,
    MAX_SCORE,
    MIN_MINUTES_FOR_RATING,
    MIN_SCORE,
    MINUTES_PER_GAME,
    MINUTES_WEIGHT,
    RED_CARD_PENALTY,
    SCORE_DECIMALS,
    YELLOW_CARD_PENALTY,
    calculate_performance_score,
    calculate_player_overall_score,
)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_BATCH_SIZE = 5000


def calculate_performance_score_vectorized(
    goals: pd.Series | np.ndarray,
    assists: pd.Series | np.ndarray,
    minutes_played: pd.Series | np.ndarray,
    yellow_cards: pd.Series | np.ndarray,
    red_cards: pd.Series | np.ndarray,
) -> np.ndarray:
    """Векторний розрахунок рейтингу за формулою з domain.py (NaN для < 10 хв)."""
    goals_arr = np.asarray(goals, dtype=np.float64)
    assists_arr = np.asarray(assists, dtype=np.float64)
    minutes_arr = np.asarray(minutes_played, dtype=np.float64)
    yellow_arr = np.asarray(yellow_cards, dtype=np.float64)
    red_arr = np.asarray(red_cards, dtype=np.float64)

    raw_scores = (
        BASE_SCORE
        + goals_arr * GOALS_WEIGHT
        + assists_arr * ASSISTS_WEIGHT
        + MINUTES_WEIGHT * (minutes_arr / MINUTES_PER_GAME)
        - yellow_arr * YELLOW_CARD_PENALTY
        - red_arr * RED_CARD_PENALTY
    )
    clamped_scores = np.clip(raw_scores, MIN_SCORE, MAX_SCORE)
    rated = minutes_arr >= MIN_MINUTES_FOR_RATING
    return np.where(rated, np.round(clamped_scores, SCORE_DECIMALS), np.nan)


def verify_formula_consistency() -> None:
    """Перевіряє ідентичність скалярної (domain.py) та векторної (ETL) формул."""
    test_cases = [
        # (goals, assists, minutes_played, yellow_cards, red_cards)
        (0, 0, 0, 0, 0),    # 0 хв -> None / NaN
        (1, 1, 9, 0, 0),    # 9 хв (< 10) -> None / NaN
        (0, 0, 10, 0, 0),   # 10 хв (нижня межа оцінювання) -> 5.06
        (0, 0, 11, 0, 0),   # 11 хв -> 5.06
        (3, 2, 90, 0, 0),   # високий результат -> обмеження 10.0
        (0, 0, 15, 2, 3),   # великий штраф (-1.92) -> обмеження 0.0
        (1, 0, 45, 1, 0),   # стандартний матч -> 6.25
    ]

    goals = np.array([c[0] for c in test_cases])
    assists = np.array([c[1] for c in test_cases])
    minutes = np.array([c[2] for c in test_cases])
    yellow = np.array([c[3] for c in test_cases])
    red = np.array([c[4] for c in test_cases])

    vectorized_results = calculate_performance_score_vectorized(
        goals=goals,
        assists=assists,
        minutes_played=minutes,
        yellow_cards=yellow,
        red_cards=red,
    )

    for case, vec_val in zip(test_cases, vectorized_results):
        scalar_val = calculate_performance_score(*case)
        if scalar_val is None:
            assert np.isnan(vec_val), (
                f"Розбіжність для {case}: scalar=None, vectorized={vec_val}"
            )
        else:
            assert not np.isnan(vec_val) and math.isclose(
                scalar_val, float(vec_val), rel_tol=1e-9
            ), f"Розбіжність для {case}: scalar={scalar_val}, vectorized={vec_val}"

    print("[TEST] Узгодженість скалярної та векторної формул підтверджено (0, 9, 10, 11 хв та межі 0–10).")


def _to_int_or_none(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(float(value))


def _to_float_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _to_str_or_none(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def _parse_datetime_series(series: pd.Series) -> list[datetime | None]:
    parsed = pd.to_datetime(series, errors="coerce")
    return [dt.to_pydatetime() if pd.notnull(dt) else None for dt in parsed]


def _insert_in_batches(
    collection: Collection,
    documents: list[dict[str, Any]],
    batch_size: int,
    label: str,
) -> None:
    total = len(documents)
    if total == 0:
        print(f"[{label}] Немає документів для вставки.")
        return

    for start in range(0, total, batch_size):
        chunk = documents[start : start + batch_size]
        collection.insert_many(chunk, ordered=False)
        done = min(start + batch_size, total)
        print(f"[{label}] Завантажено {done}/{total} документів...")


def load_appearances_grouped(
    appearances_path: Path,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int], dict[str, int], int]:
    print("Читання appearances.csv...")
    df = pd.read_csv(appearances_path, low_memory=False)

    # Видалення порожніх і дубльованих appearance_id до будь-якої обробки
    before = len(df)
    df = df.dropna(subset=["appearance_id"]).drop_duplicates(subset=["appearance_id"])
    print(f"  Видалено дублікатів/порожніх appearance_id: {before - len(df)}")

    # Заповнення NaN у метриках виступів нулями
    for col in ("yellow_cards", "red_cards", "goals", "assists", "minutes_played"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
        else:
            df[col] = 0

    df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce")
    df = df.dropna(subset=["player_id"]).copy()
    df["player_id"] = df["player_id"].astype(int)
    valid_appearances_count = len(df)

    club_col = "player_club_id" if "player_club_id" in df.columns else "player_current_club_id"

    # Підрахунок унікальних матчів (nunique за game_id) для клубів і турнірів
    club_matches_count: dict[int, int] = {}
    if club_col in df.columns and "game_id" in df.columns:
        valid_club_games = df.dropna(subset=[club_col, "game_id"]).copy()
        valid_club_games[club_col] = pd.to_numeric(valid_club_games[club_col], errors="coerce")
        valid_club_games = valid_club_games.dropna(subset=[club_col])
        valid_club_games[club_col] = valid_club_games[club_col].astype(int)
        club_matches_count = (
            valid_club_games.groupby(club_col)["game_id"].nunique().to_dict()
        )

    comp_matches_count: dict[str, int] = {}
    if "competition_id" in df.columns and "game_id" in df.columns:
        valid_comp_games = df.dropna(subset=["competition_id", "game_id"]).copy()
        valid_comp_games["competition_id"] = valid_comp_games["competition_id"].astype(str).str.strip()
        valid_comp_games = valid_comp_games[valid_comp_games["competition_id"] != ""]
        comp_matches_count = (
            valid_comp_games.groupby("competition_id")["game_id"].nunique().to_dict()
        )

    # Векторний розрахунок рейтингу ефективності (NaN для виступів < 10 хв)
    scores = calculate_performance_score_vectorized(
        goals=df["goals"],
        assists=df["assists"],
        minutes_played=df["minutes_played"],
        yellow_cards=df["yellow_cards"],
        red_cards=df["red_cards"],
    )

    dates = _parse_datetime_series(df["date"]) if "date" in df.columns else [None] * len(df)
    app_ids = df["appearance_id"].astype(str).tolist()
    player_ids = df["player_id"].tolist()
    game_ids = [_to_int_or_none(v) for v in df["game_id"]] if "game_id" in df.columns else [None] * len(df)
    club_ids = [_to_int_or_none(v) for v in df[club_col]] if club_col in df.columns else [None] * len(df)
    comp_ids = (
        [_to_str_or_none(v) for v in df["competition_id"]]
        if "competition_id" in df.columns
        else [None] * len(df)
    )

    goals_list = df["goals"].tolist()
    assists_list = df["assists"].tolist()
    minutes_list = df["minutes_played"].tolist()
    yellow_list = df["yellow_cards"].tolist()
    red_list = df["red_cards"].tolist()
    scores_list = [None if np.isnan(s) else float(s) for s in scores]

    appearances_by_player: dict[int, list[dict[str, Any]]] = defaultdict(list)

    print("Групування виступів за player_id...")
    for (
        app_id,
        p_id,
        g_id,
        p_club_id,
        comp_id,
        dt,
        goals,
        assists,
        minutes,
        yellow,
        red,
        score,
    ) in zip(
        app_ids,
        player_ids,
        game_ids,
        club_ids,
        comp_ids,
        dates,
        goals_list,
        assists_list,
        minutes_list,
        yellow_list,
        red_list,
        scores_list,
    ):
        appearances_by_player[p_id].append(
            {
                "appearance_id": app_id,
                "game_id": g_id,
                "player_club_id": p_club_id,
                "competition_id": comp_id,
                "date": dt,
                "goals": goals,
                "assists": assists,
                "minutes_played": minutes,
                "yellow_cards": yellow,
                "red_cards": red,
                "performance_score": score,
            }
        )

    # Звільнення великих проміжних структур для зниження піку RAM
    del (
        df,
        scores,
        dates,
        app_ids,
        player_ids,
        game_ids,
        club_ids,
        comp_ids,
        goals_list,
        assists_list,
        minutes_list,
        yellow_list,
        red_list,
        scores_list,
    )
    gc.collect()

    # Сортування виступів кожного гравця за датою за спаданням
    for apps in appearances_by_player.values():
        apps.sort(
            key=lambda item: (item["date"] is not None, item["date"] or datetime.min),
            reverse=True,
        )

    return (
        dict(appearances_by_player),
        club_matches_count,
        comp_matches_count,
        valid_appearances_count,
    )


def build_player_documents(
    players_path: Path,
    appearances_by_player: dict[int, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[int, int], dict[str, int], set[int]]:
    print("Читання та очищення players.csv...")
    df = pd.read_csv(players_path, low_memory=False)
    df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce")
    df = df.dropna(subset=["player_id"]).copy()
    df["player_id"] = df["player_id"].astype(int)

    dob_list = (
        _parse_datetime_series(df["date_of_birth"])
        if "date_of_birth" in df.columns
        else [None] * len(df)
    )

    # Переведення в object перед заміною NaN -> None, щоб уникнути BSON NaN
    records = df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")

    club_players_count: dict[int, int] = defaultdict(int)
    comp_domestic_players_count: dict[str, int] = defaultdict(int)
    matched_player_ids: set[int] = set()
    documents: list[dict[str, Any]] = []

    for row, dob in zip(records, dob_list):
        player_id = int(row["player_id"])
        matched_player_ids.add(player_id)

        first_name = _to_str_or_none(row.get("first_name"))
        last_name = _to_str_or_none(row.get("last_name"))
        full_name = _to_str_or_none(row.get("name")) or f"{first_name or ''} {last_name or ''}".strip()

        current_club_id = _to_int_or_none(row.get("current_club_id"))
        domestic_comp_id = _to_str_or_none(row.get("current_club_domestic_competition_id"))

        if current_club_id is not None:
            club_players_count[current_club_id] += 1
        if domestic_comp_id is not None:
            comp_domestic_players_count[domestic_comp_id] += 1

        player_appearances = appearances_by_player.get(player_id, [])
        total_goals = sum(a["goals"] for a in player_appearances)
        total_assists = sum(a["assists"] for a in player_appearances)
        total_yellow_cards = sum(a["yellow_cards"] for a in player_appearances)
        total_red_cards = sum(a["red_cards"] for a in player_appearances)

        avg_score = calculate_player_overall_score(
            [a["performance_score"] for a in player_appearances]
        )

        # full_scouting_report у MongoDB навмисно не записуємо
        documents.append(
            {
                "player_id": player_id,
                "first_name": first_name,
                "last_name": last_name,
                "name": full_name,
                "last_season": _to_int_or_none(row.get("last_season")),
                "current_club_id": current_club_id,
                "current_club_name": _to_str_or_none(row.get("current_club_name")),
                "current_club_domestic_competition_id": domestic_comp_id,
                "country_of_birth": _to_str_or_none(row.get("country_of_birth")),
                "city_of_birth": _to_str_or_none(row.get("city_of_birth")),
                "citizenship": _to_str_or_none(row.get("country_of_citizenship")),
                "date_of_birth": dob,
                "position": _to_str_or_none(row.get("position")),
                "sub_position": _to_str_or_none(row.get("sub_position")),
                "foot": _to_str_or_none(row.get("foot")),
                "height_in_cm": _to_int_or_none(row.get("height_in_cm")),
                "market_value_eur": _to_float_or_none(row.get("market_value_in_eur")),
                "highest_market_value_eur": _to_float_or_none(
                    row.get("highest_market_value_in_eur")
                ),
                "total_matches": len(player_appearances),
                "total_goals": total_goals,
                "total_assists": total_assists,
                "total_yellow_cards": total_yellow_cards,
                "total_red_cards": total_red_cards,
                "avg_performance_score": avg_score,
                "appearances": player_appearances,
            }
        )

    del df, records, dob_list
    gc.collect()

    return (
        documents,
        dict(club_players_count),
        dict(comp_domestic_players_count),
        matched_player_ids,
    )


def build_club_documents(
    clubs_path: Path,
    club_players_count: dict[int, int],
    club_matches_count: dict[int, int],
) -> list[dict[str, Any]]:
    print("Читання та очищення clubs.csv...")
    df = pd.read_csv(clubs_path, low_memory=False)
    df["club_id"] = pd.to_numeric(df["club_id"], errors="coerce")
    df = df.dropna(subset=["club_id"]).copy()
    df["club_id"] = df["club_id"].astype(int)

    records = df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")
    documents: list[dict[str, Any]] = []

    for row in records:
        club_id = int(row["club_id"])
        documents.append(
            {
                "club_id": club_id,
                "club_code": _to_str_or_none(row.get("club_code")),
                "club_name": _to_str_or_none(row.get("name")),
                "domestic_competition_id": _to_str_or_none(row.get("domestic_competition_id")),
                "squad_size": _to_int_or_none(row.get("squad_size")),
                "average_age": _to_float_or_none(row.get("average_age")),
                "foreigners_number": _to_int_or_none(row.get("foreigners_number")),
                "foreigners_percentage": _to_float_or_none(row.get("foreigners_percentage")),
                "national_team_players": _to_int_or_none(row.get("national_team_players")),
                "stadium_name": _to_str_or_none(row.get("stadium_name")),
                "stadium_seats": _to_int_or_none(row.get("stadium_seats")),
                "coach_name": _to_str_or_none(row.get("coach_name")),
                "last_season": _to_int_or_none(row.get("last_season")),
                "players_count": club_players_count.get(club_id, 0),
                "matches_count": int(club_matches_count.get(club_id, 0)),
            }
        )

    return documents


def build_competition_documents(
    competitions_path: Path,
    comp_domestic_players_count: dict[str, int],
    comp_matches_count: dict[str, int],
) -> list[dict[str, Any]]:
    print("Читання та очищення competitions.csv...")
    df = pd.read_csv(competitions_path, low_memory=False)
    records = df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")
    documents: list[dict[str, Any]] = []

    for row in records:
        comp_id = _to_str_or_none(row.get("competition_id"))
        if not comp_id:
            continue
        documents.append(
            {
                "competition_id": comp_id,
                "competition_code": _to_str_or_none(row.get("competition_code")),
                "competition_name": _to_str_or_none(row.get("name")),
                "type": _to_str_or_none(row.get("type")),
                "sub_type": _to_str_or_none(row.get("sub_type")),
                "country_id": _to_int_or_none(row.get("country_id")),
                "country_name": _to_str_or_none(row.get("country_name")),
                "domestic_league_code": _to_str_or_none(row.get("domestic_league_code")),
                "confederation": _to_str_or_none(row.get("confederation")),
                "domestic_players_count": comp_domestic_players_count.get(comp_id, 0),
                "matches_count": int(comp_matches_count.get(comp_id, 0)),
            }
        )

    return documents


def verify_loaded_data(
    db: Database,
    valid_appearances_count: int,
    orphan_appearances_count: int,
) -> None:
    """Післязавантажувальна перевірка інваріантів моделі та якості даних."""
    players = get_critical_collection(db, "players")

    # 1. Виступи з performance_score: null vs minutes_played < 10
    app_stats = list(
        players.aggregate(
            [
                {"$unwind": "$appearances"},
                {
                    "$group": {
                        "_id": None,
                        "total_embedded": {"$sum": 1},
                        "null_scores": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$appearances.performance_score", None]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "under_10_min": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$lt": [
                                            "$appearances.minutes_played",
                                            MIN_MINUTES_FOR_RATING,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                        "min_score": {"$min": "$appearances.performance_score"},
                        "max_score": {"$max": "$appearances.performance_score"},
                    }
                },
            ]
        )
    )[0]

    # 2. Перевірка відсутності BSON NaN
    nan_mv = players.count_documents({"market_value_eur": float("nan")})
    nan_avg = players.count_documents({"avg_performance_score": float("nan")})
    nan_app = players.count_documents({"appearances.performance_score": float("nan")})

    # 3. Сума total_matches та частка заповненості market_value_eur
    total_players = players.count_documents({})
    filled_mv = players.count_documents({"market_value_eur": {"$ne": None}})
    mv_pct = (filled_mv / total_players * 100.0) if total_players > 0 else 0.0

    matches_sum_res = list(
        players.aggregate([{"$group": {"_id": None, "sum_matches": {"$sum": "$total_matches"}}}])
    )
    sum_total_matches = matches_sum_res[0]["sum_matches"] if matches_sum_res else 0

    print("\n--- Перевірка якості даних після ETL ---")
    print(
        f"  1. Виступи з performance_score=null: {app_stats['null_scores']} | "
        f"виступи з minutes_played < {MIN_MINUTES_FOR_RATING}: {app_stats['under_10_min']}"
    )
    print(
        f"  2. BSON NaN перевірка: market_value_eur={nan_mv}, "
        f"avg_performance_score={nan_avg}, appearances.performance_score={nan_app}"
    )
    print(
        f"  3. Діапазон не-null performance_score: "
        f"[{app_stats['min_score']}, {app_stats['max_score']}] (очікується в межах [0, 10])"
    )
    print(
        f"  4. Сума total_matches={sum_total_matches} + відкинуті виступи={orphan_appearances_count} "
        f"= {sum_total_matches + orphan_appearances_count} (валідних після дедуплікації: {valid_appearances_count})"
    )
    print(
        f"  5. Заповненість market_value_eur: {filled_mv}/{total_players} ({mv_pct:.2f}%)"
    )

    assert app_stats["null_scores"] == app_stats["under_10_min"], "Кількість null-рейтингів не збігається з виступами < 10 хв!"
    assert nan_mv == 0 and nan_avg == 0 and nan_app == 0, "Виявлено BSON NaN у документах!"
    assert MIN_SCORE <= app_stats["min_score"] <= app_stats["max_score"] <= MAX_SCORE, "Рейтинг вийшов за межі [0, 10]!"
    assert sum_total_matches + orphan_appearances_count == valid_appearances_count, "Невідповідність кількості матчів!"


def run_etl(data_dir: Path, batch_size: int) -> None:
    verify_formula_consistency()

    required_files = {
        "players": data_dir / "players.csv",
        "appearances": data_dir / "appearances.csv",
        "clubs": data_dir / "clubs.csv",
        "competitions": data_dir / "competitions.csv",
    }
    for name, path in required_files.items():
        if not path.exists():
            raise FileNotFoundError(f"Не знайдено файл датасету '{name}': {path}")

    (
        appearances_by_player,
        club_matches_count,
        comp_matches_count,
        valid_appearances_count,
    ) = load_appearances_grouped(required_files["appearances"])

    player_docs, club_players_count, comp_domestic_players_count, matched_player_ids = (
        build_player_documents(
            required_files["players"],
            appearances_by_player,
        )
    )
    club_docs = build_club_documents(
        required_files["clubs"],
        club_players_count,
        club_matches_count,
    )
    competition_docs = build_competition_documents(
        required_files["competitions"],
        comp_domestic_players_count,
        comp_matches_count,
    )

    # Звіт про якість даних (виступи гравців, яких немає в players.csv)
    orphan_player_ids = set(appearances_by_player.keys()) - matched_player_ids
    orphan_appearances_count = sum(
        len(appearances_by_player[pid]) for pid in orphan_player_ids
    )
    print("\n--- Звіт про якість даних ---")
    print(f"  Гравців у players.csv: {len(matched_player_ids)}")
    print(
        f"  Відкинуто виступів без профілю в players.csv: {orphan_appearances_count} "
        f"(для {len(orphan_player_ids)} відсутніх player_id)"
    )

    del appearances_by_player
    gc.collect()

    client = create_mongo_client()
    try:
        db = get_database(client)
        players_col = get_critical_collection(db, "players")
        clubs_col = get_critical_collection(db, "clubs")
        competitions_col = get_critical_collection(db, "competitions")

        # Очищення документів без видалення колекцій, щоб зберегти індекси
        print("\nОчищення попередніх записів (delete_many)...")
        players_col.delete_many({})
        clubs_col.delete_many({})
        competitions_col.delete_many({})

        _insert_in_batches(players_col, player_docs, batch_size, "players")
        _insert_in_batches(clubs_col, club_docs, batch_size, "clubs")
        _insert_in_batches(competitions_col, competition_docs, batch_size, "competitions")

        print("\n--- Підсумок завантаження ETL ---")
        for col_name in ("players", "clubs", "competitions", "search_queries"):
            count = db[col_name].count_documents({})
            print(f"  {col_name}: {count} документів")

        verify_loaded_data(
            db=db,
            valid_appearances_count=valid_appearances_count,
            orphan_appearances_count=orphan_appearances_count,
        )
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ETL-завантаження датасету Transfermarkt у MongoDB")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Шлях до папки з CSV-файлами (за замовчуванням: ./data)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Розмір пакета для insert_many (за замовчуванням: 5000)",
    )
    args = parser.parse_args()
    run_etl(data_dir=args.data_dir, batch_size=args.batch_size)


if __name__ == "__main__":
    main()