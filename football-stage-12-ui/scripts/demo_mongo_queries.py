from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from command_service.adapters.inbound.controllers import (
    AppearanceCommandController,
    PlayerQueryController,
)
from command_service.adapters.outbound.mongo_adapter import MongoAdapter
from command_service.adapters.outbound.neo4j_adapter import Neo4jAdapter
from command_service.application.services import (
    AnalyticsService,
    AppearanceService,
    PlayerFacade,
    PlayerProfileService,
    ReferenceService,
    ScoutingService,
    TelemetryService,
)
from command_service.config import (
    create_mongo_client,
    get_critical_collection,
    get_database,
    get_read_collection,
    get_telemetry_collection,
)
from command_service.dto import PlayerFilterCriteria, TelemetrySummaryDTO

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE_PATH = PROJECT_ROOT / "demo_results.txt"

WARMUP_RUNS = 1
BENCHMARK_RUNS = 15


class TeeStream:
    """Дублює вивід одночасно у стандартний потік консолі та у текстовий файл."""

    def __init__(self, console: TextIO, file_stream: TextIO) -> None:
        self._console = console
        self._file = file_stream

    def write(self, data: str) -> int:
        self._console.write(data)
        self._file.write(data)
        return len(data)

    def flush(self) -> None:
        self._console.flush()
        self._file.flush()


def benchmark_callable(
    fn: Callable[[], Any],
    warmup: int = WARMUP_RUNS,
    repeats: int = BENCHMARK_RUNS,
) -> tuple[float, float, float, Any]:
    """Виконує прогрів і серію повторів, повертаючи (min_ms, median_ms, max_ms, last_result)."""
    last_result = None
    for _ in range(warmup):
        last_result = fn()

    timings_ms: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        last_result = fn()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        timings_ms.append(elapsed_ms)

    return (
        round(min(timings_ms), 3),
        round(statistics.median(timings_ms), 3),
        round(max(timings_ms), 3),
        last_result,
    )


def _extract_plan_stages(plan_node: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """Рекурсивно обходить дерево плану виконання MongoDB і збирає стадії та назви індексів."""
    if not isinstance(plan_node, dict):
        return [], []

    if "queryPlan" in plan_node and isinstance(plan_node["queryPlan"], dict):
        return _extract_plan_stages(plan_node["queryPlan"])

    stages: list[str] = []
    indexes: list[str] = []

    stage_name = plan_node.get("stage")
    if stage_name:
        stages.append(str(stage_name))

    index_name = plan_node.get("indexName")
    if index_name and str(index_name) not in indexes:
        indexes.append(str(index_name))

    if "inputStage" in plan_node:
        sub_stages, sub_indexes = _extract_plan_stages(plan_node["inputStage"])
        stages.extend(sub_stages)
        for idx in sub_indexes:
            if idx not in indexes:
                indexes.append(idx)

    for child_key in ("inputStages", "shards"):
        for child in plan_node.get(child_key, []):
            sub_stages, sub_indexes = _extract_plan_stages(child)
            stages.extend(sub_stages)
            for idx in sub_indexes:
                if idx not in indexes:
                    indexes.append(idx)

    return stages, indexes


def summarize_explain(explain_doc: dict[str, Any]) -> dict[str, Any]:
    """Витягує ключові метрики з результату команди explain (verbosity='executionStats')."""
    exec_stats = explain_doc.get("executionStats", {})
    query_planner = explain_doc.get("queryPlanner", {})

    winning_plan = query_planner.get("winningPlan", {})
    stages, indexes = _extract_plan_stages(winning_plan)
    if not stages and "executionStages" in exec_stats:
        stages, indexes = _extract_plan_stages(exec_stats["executionStages"])

    server_info = explain_doc.get("serverInfo", {})
    server_label = (
        f"{server_info.get('host', '?')}:{server_info.get('port', '?')}"
        if server_info
        else "unknown"
    )

    return {
        "stages": " -> ".join(stages) if stages else "N/A",
        "indexes": ", ".join(indexes) if indexes else "NONE (COLLSCAN)",
        "has_in_memory_sort": "SORT" in stages,
        "nReturned": exec_stats.get("nReturned", 0),
        "totalKeysExamined": exec_stats.get("totalKeysExamined", 0),
        "totalDocsExamined": exec_stats.get("totalDocsExamined", 0),
        "executionTimeMillis": exec_stats.get("executionTimeMillis", 0),
        "server": server_label,
    }


def explain_find_command(
    col: Collection,
    filt: dict[str, Any],
    proj: dict[str, Any] | None = None,
    sort_spec: list[tuple[str, int]] | None = None,
    limit_val: int | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Виконує команду explain('executionStats') із read_preference цільової колекції."""
    cmd: dict[str, Any] = {
        "find": col.name,
        "filter": filt,
        "projection": proj or {"_id": 0, "appearances": 0},
    }
    if sort_spec:
        cmd["sort"] = dict(sort_spec)
    if limit_val is not None:
        cmd["limit"] = limit_val
    if hint is not None:
        cmd["hint"] = hint

    result = col.database.command(
        {"explain": cmd, "verbosity": "executionStats"},
        read_preference=col.read_preference,
    )
    return summarize_explain(result)


def _format_rows_count(res: Any) -> str:
    """Повертає кількість повернутих елементів або '-' для агрегованого зведення."""
    if res is None:
        return "0"
    if isinstance(res, TelemetrySummaryDTO):
        return "-"
    if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], list):
        return str(len(res[1]))
    if isinstance(res, list):
        return str(len(res))
    return "1"


def run_telemetry_scenario(db: Database) -> None:
    print("=" * 102)
    print("1. КАТЕГОРІЯ 7: ТЕЛЕМЕТРІЯ ТА МОНІТОРИНГ ПРОДУКТИВНОСТІ ЗАПИТІВ")
    print("=" * 102)

    adapter = MongoAdapter(db)
    neo4j_stub = Neo4jAdapter()

    profile_svc = PlayerProfileService(
        repository=adapter,
        telemetry_repository=adapter,
        enable_telemetry=True,
    )
    appearance_svc = AppearanceService(repository=adapter)
    scouting_svc = ScoutingService(graph_repository=neo4j_stub)
    analytics_svc = AnalyticsService(
        analytics_repository=adapter,
        telemetry_repository=adapter,
        enable_telemetry=True,
    )
    telemetry_svc = TelemetryService(telemetry_repository=adapter)
    facade = PlayerFacade(
        profile_service=profile_svc,
        appearance_service=appearance_svc,
        scouting_service=scouting_svc,
        reference_repository=adapter,
        telemetry_repository=adapter,
        enable_telemetry=True,
    )

    facade.get_full_player_dossier(player_id=10, appearances_limit=10)
    profile_svc.filter_players(
        PlayerFilterCriteria(
            position="Attack",
            sub_position="Centre-Forward",
            min_market_value_eur=10_000_000,
            limit=10,
        )
    )
    analytics_svc.get_defender_cards_stats()
    analytics_svc.get_top_competitions_by_goals_per_match(limit=5, min_matches=100)

    recent = telemetry_svc.get_recent_queries(limit=5)
    print(f"Останні {len(recent)} записів у search_queries (сортування timestamp DESC):")
    for q in recent:
        print(
            f"  - [{q.timestamp.isoformat()}] type={q.query_type:<32} | "
            f"time={q.execution_time_ms:>6.2f} мс | results={q.results_count} | cache_hit={q.cache_hit}"
        )

    summary_window = telemetry_svc.get_telemetry_summary(window_minutes=5)
    summary_all = telemetry_svc.get_telemetry_summary(window_minutes=None)

    print("\nЗведення телеметрії за останні 5 хвилин (window_minutes=5):")
    print(
        f"  total_queries={summary_window.total_queries}, "
        f"avg_ms={summary_window.avg_execution_time_ms}, "
        f"max_ms={summary_window.max_execution_time_ms}, "
        f"cache_hit_rate={summary_window.cache_hit_rate_pct}%"
    )
    for item in summary_window.by_query_type:
        print(f"    * {item}")

    print("\nЗведення телеметрії за весь час (window_minutes=None):")
    print(
        f"  total_queries={summary_all.total_queries}, "
        f"avg_ms={summary_all.avg_execution_time_ms}, "
        f"max_ms={summary_all.max_execution_time_ms}, "
        f"cache_hit_rate={summary_all.cache_hit_rate_pct}%"
    )


def run_benchmarks(db: Database) -> None:
    print("\n" + "=" * 102)
    print(
        f"2. ЗАМІРИ ЧАСУ ВИКОНАННЯ ЗАПИТІВ (1 прогрів + {BENCHMARK_RUNS} повторів, enable_telemetry=False)"
    )
    print("=" * 102)

    adapter = MongoAdapter(db)
    neo4j_stub = Neo4jAdapter()
    players_read = get_read_collection(db, "players")

    profile_svc = PlayerProfileService(
        repository=adapter,
        telemetry_repository=adapter,
        enable_telemetry=False,
    )
    appearance_svc = AppearanceService(repository=adapter)
    scouting_svc = ScoutingService(graph_repository=neo4j_stub)
    reference_svc = ReferenceService(reference_repository=adapter)
    analytics_svc = AnalyticsService(
        analytics_repository=adapter,
        telemetry_repository=adapter,
        enable_telemetry=False,
    )
    telemetry_svc = TelemetryService(telemetry_repository=adapter)
    facade = PlayerFacade(
        profile_service=profile_svc,
        appearance_service=appearance_svc,
        scouting_service=scouting_svc,
        reference_repository=adapter,
        telemetry_repository=adapter,
        enable_telemetry=False,
    )

    esr_criteria = PlayerFilterCriteria(
        position="Attack",
        sub_position="Centre-Forward",
        min_market_value_eur=5_000_000,
        max_market_value_eur=30_000_000,
        sort_by="market_value_eur",
        sort_order=-1,
        limit=20,
    )
    elem_match_criteria = PlayerFilterCriteria(
        position="Midfield",
        competition_id="L1",
        min_match_minutes=60,
        min_match_goals=1,
        limit=20,
    )
    elem_match_raw_filter = {
        "position": "Midfield",
        "appearances": {
            "$elemMatch": {
                "competition_id": "L1",
                "minutes_played": {"$gte": 60},
                "goals": {"$gte": 1},
            }
        },
    }
    elem_match_proj = {"_id": 0, "appearances": 0}
    elem_match_sort = [("market_value_eur", -1)]

    scenarios: list[tuple[str, Callable[[], Any]]] = [
        (
            "Кат. 3: Досьє гравця (player_id=10, limit=20)",
            lambda: facade.get_full_player_dossier(player_id=10, appearances_limit=20),
        ),
        (
            "Кат. 2: Фільтр ESR (Attack + Centre-Forward + вартість)",
            lambda: profile_svc.filter_players(esr_criteria),
        ),
        (
            "Кат. 2: Фільтр $elemMatch через сервіс + DTO",
            lambda: profile_svc.filter_players(elem_match_criteria),
        ),
        (
            "Кат. 2: $elemMatch raw (автовибір планувальника)",
            lambda: list(
                players_read.find(elem_match_raw_filter, elem_match_proj)
                .sort(elem_match_sort)
                .limit(20)
            ),
        ),
        (
            "Кат. 2: $elemMatch raw + hint(appearances_elem_match)",
            lambda: list(
                players_read.find(elem_match_raw_filter, elem_match_proj)
                .hint("idx_players_appearances_elem_match")
                .sort(elem_match_sort)
                .limit(20)
            ),
        ),
        (
            "Кат. 2: $elemMatch raw + hint(players_pos_subpos_value)",
            lambda: list(
                players_read.find(elem_match_raw_filter, elem_match_proj)
                .hint("idx_players_pos_subpos_value")
                .sort(elem_match_sort)
                .limit(20)
            ),
        ),
        (
            "Кат. 3: Склад клубу (club_id=398, Lazio)",
            lambda: reference_svc.get_club_roster(club_id=398, limit=50),
        ),
        (
            "Кат. 6: Агрегація карток захисників за амплуа",
            lambda: analytics_svc.get_defender_cards_stats(),
        ),
        (
            "Кат. 6: Топ-10 турнірів за голами/матч (min_matches=100)",
            lambda: analytics_svc.get_top_competitions_by_goals_per_match(
                limit=10, min_matches=100
            ),
        ),
        (
            "Кат. 7: Останні 20 запитів телеметрії",
            lambda: telemetry_svc.get_recent_queries(limit=20),
        ),
        (
            "Кат. 7: Зведення телеметрії (вікно 5 хв)",
            lambda: telemetry_svc.get_telemetry_summary(window_minutes=5),
        ),
    ]

    header = (
        f"{'Сценарій запиту':<64} | {'Rows':>5} | {'Min (ms)':>9} | {'Med (ms)':>9} | {'Max (ms)':>9}"
    )
    print(header)
    print("-" * len(header))

    defender_stats_sample = None
    top_comps_sample = None

    for title, fn in scenarios:
        min_ms, med_ms, max_ms, res = benchmark_callable(fn)
        rows_label = _format_rows_count(res)
        print(
            f"{title:<64} | {rows_label:>5} | {min_ms:>9.3f} | {med_ms:>9.3f} | {max_ms:>9.3f}"
        )
        if "карток захисників" in title:
            defender_stats_sample = res
        elif "Топ-10 турнірів" in title:
            top_comps_sample = res

    if defender_stats_sample:
        print("\n[Результат Кат. 6 — Картки захисників за амплуа]")
        for row in defender_stats_sample:
            sub_pos_label = row.sub_position or "Unknown"
            print(
                f"  - {sub_pos_label:<16}: гравців={row.players_count}, "
                f"матчів={row.total_matches}, жовтих/матч={row.yellow_cards_per_match:.4f}, "
                f"червоних/матч={row.red_cards_per_match:.4f}, усього/матч={row.cards_per_match:.4f}"
            )

    if top_comps_sample:
        print("\n[Результат Кат. 6 — Топ-5 результативних турнірів (matches >= 100)]")
        for row in top_comps_sample[:5]:
            print(
                f"  - {row.competition_id:<5} ({row.competition_name or 'N/A'}, {row.country_name or 'N/A'}): "
                f"матчів={row.matches_count}, голів={row.total_goals}, "
                f"голів/матч={row.goals_per_match}, сер. рейтинг={row.avg_performance_score}"
            )


def run_explain_plans(db: Database) -> None:
    print("\n" + "=" * 102)
    print("3. АНАЛІЗ ПЛАНІВ ВИКОНАННЯ ЗАПИТІВ (command explain, verbosity='executionStats')")
    print("=" * 102)

    players_read = get_read_collection(db, "players")
    players_critical = get_critical_collection(db, "players")
    telemetry_col = get_telemetry_collection(db)

    elem_match_filter = {
        "position": "Midfield",
        "appearances": {
            "$elemMatch": {
                "competition_id": "L1",
                "minutes_played": {"$gte": 60},
                "goals": {"$gte": 1},
            }
        },
    }

    explain_cases: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (
            "3.1a ESR повний: position + sub_position + market_value_eur + SORT(value)",
            lambda: explain_find_command(
                players_read,
                {
                    "position": "Attack",
                    "sub_position": "Centre-Forward",
                    "market_value_eur": {"$ne": None, "$gte": 5_000_000, "$lte": 30_000_000},
                },
                sort_spec=[("market_value_eur", -1)],
                limit_val=20,
            ),
        ),
        (
            "3.1b ESR неповний: лише position + SORT(market_value_eur) -> очікується SORT в RAM",
            lambda: explain_find_command(
                players_read,
                {
                    "position": "Attack",
                    "market_value_eur": {"$ne": None, "$gte": 5_000_000, "$lte": 30_000_000},
                },
                sort_spec=[("market_value_eur", -1)],
                limit_val=20,
            ),
        ),
        (
            "3.2a Фільтрація $elemMatch (автовибір планувальника після додавання competition_id)",
            lambda: explain_find_command(
                players_read,
                elem_match_filter,
                sort_spec=[("market_value_eur", -1)],
                limit_val=20,
            ),
        ),
        (
            "3.2b Фільтрація $elemMatch з hint('idx_players_appearances_elem_match')",
            lambda: explain_find_command(
                players_read,
                elem_match_filter,
                sort_spec=[("market_value_eur", -1)],
                limit_val=20,
                hint="idx_players_appearances_elem_match",
            ),
        ),
        (
            "3.2c Фільтрація $elemMatch з hint('idx_players_pos_subpos_value') [базова лінія]",
            lambda: explain_find_command(
                players_read,
                elem_match_filter,
                sort_spec=[("market_value_eur", -1)],
                limit_val=20,
                hint="idx_players_pos_subpos_value",
            ),
        ),
        (
            "3.3a Склад клубу: current_club_id=398 + SORT(market_value_eur)",
            lambda: explain_find_command(
                players_read,
                {"current_club_id": 398},
                sort_spec=[("market_value_eur", -1)],
                limit_val=50,
            ),
        ),
        (
            "3.3b Точковий пошук досьє за унікальним player_id=10 ($slice: 20)",
            lambda: explain_find_command(
                players_read,
                {"player_id": 10},
                proj={"_id": 0, "appearances": {"$slice": 20}},
                limit_val=1,
            ),
        ),
        (
            "3.4 Фільтр атомарного запису виступу (player_id + $ne appearance_id)",
            lambda: explain_find_command(
                players_critical,
                {
                    "player_id": 10,
                    "appearances.appearance_id": {"$ne": "EXPLAIN_PROBE_NON_EXISTENT"},
                },
                proj={"_id": 1},
                limit_val=1,
            ),
        ),
        (
            "3.5 Телеметрія: останні 20 запитів за timestamp DESC",
            lambda: explain_find_command(
                telemetry_col,
                {},
                proj={"_id": 0},
                sort_spec=[("timestamp", -1)],
                limit_val=20,
            ),
        ),
    ]

    for label, runner in explain_cases:
        info = runner()
        print(f"\n[{label}]")
        print(f"  Стадії плану      : {info['stages']}")
        print(f"  Індекс            : {info['indexes']}")
        print(f"  SORT у пам'яті    : {'ТАК' if info['has_in_memory_sort'] else 'НІ'}")
        print(
            f"  Метрики           : nReturned={info['nReturned']}, "
            f"totalKeysExamined={info['totalKeysExamined']}, "
            f"totalDocsExamined={info['totalDocsExamined']}, "
            f"executionTimeMillis={info['executionTimeMillis']} мс"
        )


def run_atomic_write_scenario(db: Database) -> None:
    print("\n" + "=" * 102)
    print("4. СЦЕНАРІЙ АТОМАРНОГО ЗАПИСУ ВИСТУПУ (З ВІДНОВЛЕННЯМ СТАНУ БД)")
    print("=" * 102)

    players_critical = get_critical_collection(db, "players")
    adapter = MongoAdapter(db)
    neo4j_stub = Neo4jAdapter()

    profile_svc = PlayerProfileService(repository=adapter, enable_telemetry=False)
    appearance_svc = AppearanceService(repository=adapter)
    scouting_svc = ScoutingService(graph_repository=neo4j_stub)
    facade = PlayerFacade(
        profile_service=profile_svc,
        appearance_service=appearance_svc,
        scouting_service=scouting_svc,
        reference_repository=adapter,
        enable_telemetry=False,
    )

    query_ctrl = PlayerQueryController(player_facade=facade)
    command_ctrl = AppearanceCommandController(appearance_service=appearance_svc)

    target_player_id = 10  # Miroslav Klose
    snapshot = players_critical.find_one({"player_id": target_player_id})
    if snapshot is None:
        print(f"[ПОМИЛКА] Гравця player_id={target_player_id} не знайдено в БД.")
        return

    unique_suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    demo_app_id = f"DEMO_STAGE4_{unique_suffix}"

    try:
        before = query_ctrl.get_player(
            target_player_id, appearances_limit=1, consistent_read=True
        )["data"]
        print(
            f"До запису (player_id={target_player_id}): "
            f"total_matches={before['total_matches']}, total_goals={before['total_goals']}, "
            f"avg_performance_score={before['avg_performance_score']}"
        )

        payload = {
            "appearance_id": demo_app_id,
            "player_id": target_player_id,
            "game_id": 9999401,
            "player_club_id": 398,
            "competition_id": "IT1",
            "date": "2026-05-25T20:45:00",
            "goals": 2,
            "assists": 1,
            "minutes_played": 90,
            "yellow_cards": 0,
            "red_cards": 0,
        }

        # 4.1 Перший запис -> SAVED
        res_saved = command_ctrl.record_match_appearance(payload)
        after_saved = query_ctrl.get_player(
            target_player_id, appearances_limit=1, consistent_read=True
        )["data"]
        first_app_id = (
            after_saved["appearances"][0]["appearance_id"]
            if after_saved.get("appearances")
            else None
        )
        print(f"  1) Новий виступ ({demo_app_id}) -> {res_saved}")
        print(
            f"     Стан після SAVED: total_matches={after_saved['total_matches']}, "
            f"total_goals={after_saved['total_goals']}, "
            f"avg_performance_score={after_saved['avg_performance_score']}, "
            f"перший у списку={first_app_id}"
        )

        # 4.2 Повторний запис того самого виступу -> DUPLICATE
        res_dup = command_ctrl.record_match_appearance(payload)
        after_dup = query_ctrl.get_player(
            target_player_id, appearances_limit=1, consistent_read=True
        )["data"]
        print(f"  2) Повторний запис ({demo_app_id}) -> {res_dup}")
        print(
            f"     Стан після DUPLICATE (лічильники незмінні): "
            f"total_matches={after_dup['total_matches']}, total_goals={after_dup['total_goals']}"
        )

        # 4.3 Неіснуючий гравець -> PLAYER_NOT_FOUND
        res_not_found = command_ctrl.record_match_appearance(
            {
                **payload,
                "appearance_id": f"{demo_app_id}_MISSING",
                "player_id": 999999999,
            }
        )
        print(f"  3) Неіснуючий player_id=999999999 -> {res_not_found}")

        # 4.4 Помилка валідації (minutes_played=200 > 130) -> validation_error
        res_invalid = command_ctrl.record_match_appearance(
            {
                **payload,
                "appearance_id": f"{demo_app_id}_INVALID",
                "minutes_played": 200,
            }
        )
        print(f"  4) Некоректні хвилини (minutes_played=200) -> {res_invalid}")

    finally:
        players_critical.replace_one({"_id": snapshot["_id"]}, snapshot)
        restored = players_critical.find_one(
            {"player_id": target_player_id},
            {"_id": 0, "total_matches": 1, "total_goals": 1, "avg_performance_score": 1},
        )
        print(f"  [Відновлення знімка] Документ гравця повернуто у вихідний стан: {restored}")


def run_read_preference_comparison(db: Database) -> None:
    print("\n" + "=" * 102)
    print("5. ПОРІВНЯННЯ РЕЖИМІВ ЧИТАННЯ REPLICA SET (PRIMARY VS SECONDARY_PREFERRED)")
    print("=" * 102)

    col_primary = get_critical_collection(db, "players")
    col_secondary = get_read_collection(db, "players")

    query = {"player_id": 10}
    proj = {"_id": 0, "appearances": {"$slice": 20}}

    min_p, med_p, max_p, _ = benchmark_callable(
        lambda: col_primary.find_one(query, proj),
        warmup=2,
        repeats=20,
    )
    min_s, med_s, max_s, _ = benchmark_callable(
        lambda: col_secondary.find_one(query, proj),
        warmup=2,
        repeats=20,
    )

    exp_primary = explain_find_command(
        col_primary,
        query,
        proj=proj,
        limit_val=1,
    )
    exp_secondary = explain_find_command(
        col_secondary,
        query,
        proj=proj,
        limit_val=1,
    )

    print(
        f"  PRIMARY + readConcern='majority'            : "
        f"min={min_p:.3f} мс | med={med_p:.3f} мс | max={max_p:.3f} мс | вузол={exp_primary['server']}"
    )
    print(
        f"  SECONDARY_PREFERRED + readConcern='local'   : "
        f"min={min_s:.3f} мс | med={med_s:.3f} мс | max={max_s:.3f} мс | вузол={exp_secondary['server']}"
    )
    same_node = exp_primary["server"] == exp_secondary["server"]
    print(
        f"  Маршрутизація вузлів                        : "
        f"{'однаковий вузол (потрапило на PRIMARY)' if same_node else 'різні вузли репліка-сету (PRIMARY vs SECONDARY)'}"
    )


def main() -> None:
    original_stdout = sys.stdout
    client: MongoClient | None = None
    with RESULTS_FILE_PATH.open("w", encoding="utf-8") as report_file:
        sys.stdout = TeeStream(original_stdout, report_file)
        try:
            client = create_mongo_client()
            db = get_database(client)
            deleted_telemetry = get_telemetry_collection(db).delete_many({}).deleted_count
            print(
                f"Демонстрація та бенчмарк MongoDB (БД: '{db.name}', "
                f"очищено старих записів телеметрії: {deleted_telemetry}, "
                f"час запуску UTC: {datetime.now(timezone.utc).isoformat()})"
            )
            run_telemetry_scenario(db)
            run_benchmarks(db)
            run_explain_plans(db)
            run_atomic_write_scenario(db)
            run_read_preference_comparison(db)
            print("\n" + "=" * 102)
            print(f"Результати збережено у файл: {RESULTS_FILE_PATH}")
            print("=" * 102)
        finally:
            sys.stdout = original_stdout
            if client is not None:
                client.close()


if __name__ == "__main__":
    main()