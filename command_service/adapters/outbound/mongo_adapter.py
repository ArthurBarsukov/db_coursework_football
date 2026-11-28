from __future__ import annotations

import re

from collections.abc import Sequence
from dataclasses import MISSING, asdict, fields
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pymongo.database import Database

from command_service.config import (
    get_critical_collection,
    get_read_collection,
    get_telemetry_collection,
)
from command_service.dto import (
    AppearanceDTO,
    ClubDTO,
    CompetitionDTO,
    CompetitionStatsDTO,
    DefenderCardsStatsDTO,
    PlayerDTO,
    PlayerFilterCriteria,
    PlayerWithAppearancesDTO,
    SaveResult,
    SearchQueryDTO,
    TelemetrySummaryDTO,
    utc_now_naive,
)
from command_service.ports.output import (
    AnalyticsRepositoryPort,
    AppearanceRepositoryPort,
    PlayerProfileRepositoryPort,
    ReferenceRepositoryPort,
    TelemetryRepositoryPort,
)

T = TypeVar("T")

ALLOWED_SORT_FIELDS = frozenset(
    {
        "market_value_eur",
        "avg_performance_score",
        "total_goals",
        "total_matches",
    }
)


def _to_naive_utc(dt: datetime) -> datetime:
    """Нормалізує datetime до наївного UTC без tzinfo для узгодженості з BSON Date."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class MongoAdapter(
    PlayerProfileRepositoryPort,
    AppearanceRepositoryPort,
    ReferenceRepositoryPort,
    AnalyticsRepositoryPort,
    TelemetryRepositoryPort,
):
    """Адаптер MongoDB для читання, фільтрації, атомарного запису, аналітики та телеметрії."""

    def __init__(self, db: Database) -> None:
        # Критична колекція: w='majority', j=True, PRIMARY + readConcern='majority'
        self._players_critical = get_critical_collection(db, "players")
        # Колекції для читання, фільтрації та агрегацій: secondaryPreferred
        self._players_read = get_read_collection(db, "players")
        self._clubs_read = get_read_collection(db, "clubs")
        self._competitions_read = get_read_collection(db, "competitions")
        # Колекція швидкої телеметрії: w=1
        self._telemetry = get_telemetry_collection(db)

    # ------------------------------------------------------------------
    # Внутрішні мапери документів MongoDB -> DTO
    # ------------------------------------------------------------------

    @staticmethod
    def _map_to_dto(cls: type[T], doc: dict[str, Any]) -> T:
        """Мапить словник документа у dataclass DTO, відкидаючи зайві поля (напр. _id)."""
        cleaned: dict[str, Any] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name not in doc:
                continue
            val = doc[f.name]
            if val is None and f.default is not MISSING and f.default is not None:
                cleaned[f.name] = f.default
            else:
                cleaned[f.name] = val
        return cls(**cleaned)

    @classmethod
    def _to_player_dto(cls, doc: dict[str, Any]) -> PlayerDTO:
        normalized = dict(doc)
        if normalized.get("name") is None:
            normalized["name"] = ""
        return cls._map_to_dto(PlayerDTO, normalized)

    @classmethod
    def _to_appearance_dto(
        cls, doc: dict[str, Any], player_id: int | None = None
    ) -> AppearanceDTO:
        normalized = dict(doc)
        if normalized.get("player_id") is None and player_id is not None:
            normalized["player_id"] = player_id
        return cls._map_to_dto(AppearanceDTO, normalized)

    @classmethod
    def _to_player_with_appearances_dto(
        cls, doc: dict[str, Any]
    ) -> PlayerWithAppearancesDTO:
        normalized = dict(doc)
        if normalized.get("name") is None:
            normalized["name"] = ""
        p_id = normalized.get("player_id")
        raw_apps = normalized.pop("appearances", None) or []
        appearances = [cls._to_appearance_dto(app, player_id=p_id) for app in raw_apps]
        base_dto = cls._map_to_dto(PlayerWithAppearancesDTO, normalized)
        base_dto.appearances = appearances
        return base_dto

    # ------------------------------------------------------------------
    # Читання профілів та виступів
    # ------------------------------------------------------------------

    def get_player_by_id(
        self, player_id: int, *, consistent_read: bool = False
    ) -> PlayerDTO | None:
        collection = self._players_critical if consistent_read else self._players_read
        doc = collection.find_one(
            {"player_id": int(player_id)},
            {"_id": 0, "appearances": 0},
        )
        if doc is None:
            return None
        return self._to_player_dto(doc)

    def get_player_with_appearances(
        self,
        player_id: int,
        limit: int = 20,
        *,
        consistent_read: bool = False,
        offset: int = 0,
    ) -> PlayerWithAppearancesDTO | None:
        safe_limit = min(max(1, int(limit)), 500)
        collection = self._players_critical if consistent_read else self._players_read
        doc = collection.find_one(
            {"player_id": int(player_id)},
            {"_id": 0, "appearances": {"$slice": [max(0, int(offset)), safe_limit]}},
        )
        if doc is None:
            return None
        return self._to_player_with_appearances_dto(doc)

    def get_appearances_by_player(
        self,
        player_id: int,
        limit: int = 50,
        *,
        consistent_read: bool = False,
    ) -> list[AppearanceDTO]:
        safe_limit = min(max(1, int(limit)), 500)
        collection = self._players_critical if consistent_read else self._players_read
        doc = collection.find_one(
            {"player_id": int(player_id)},
            {"_id": 0, "player_id": 1, "appearances": {"$slice": safe_limit}},
        )
        if doc is None:
            return []
        raw_apps = doc.get("appearances") or []
        return [
            self._to_appearance_dto(app, player_id=int(player_id)) for app in raw_apps
        ]

    # ------------------------------------------------------------------
    # Багатокритеріальна фільтрація (Категорія 2)
    # ------------------------------------------------------------------

    def filter_players(self, criteria: PlayerFilterCriteria) -> list[PlayerDTO]:
        query: dict[str, Any] = {}

        if criteria.name:
            query["name"] = {"$regex": re.escape(criteria.name.strip()), "$options": "i"}

        if criteria.position is not None:
            query["position"] = criteria.position
        if criteria.sub_position is not None:
            query["sub_position"] = criteria.sub_position
        if criteria.foot is not None:
            query["foot"] = criteria.foot
        if criteria.citizenship is not None:
            query["citizenship"] = criteria.citizenship
        if criteria.current_club_id is not None:
            query["current_club_id"] = int(criteria.current_club_id)

        # Діапазон ринкової вартості лише для заповнених (не-null) значень
        if (
            criteria.min_market_value_eur is not None
            or criteria.max_market_value_eur is not None
        ):
            mv_filter: dict[str, Any] = {"$ne": None}
            if criteria.min_market_value_eur is not None:
                mv_filter["$gte"] = float(criteria.min_market_value_eur)
            if criteria.max_market_value_eur is not None:
                mv_filter["$lte"] = float(criteria.max_market_value_eur)
            query["market_value_eur"] = mv_filter

        if criteria.min_avg_score is not None:
            query["avg_performance_score"] = {
                "$ne": None,
                "$gte": float(criteria.min_avg_score),
            }

        # Фільтрація вбудованого масиву виступів за одним збігом ($elemMatch)
        elem_match: dict[str, Any] = {}
        if criteria.competition_id is not None:
            elem_match["competition_id"] = criteria.competition_id
        if criteria.min_match_minutes is not None:
            elem_match["minutes_played"] = {"$gte": int(criteria.min_match_minutes)}
        if criteria.min_match_goals is not None:
            elem_match["goals"] = {"$gte": int(criteria.min_match_goals)}
        if criteria.min_match_score is not None:
            elem_match["performance_score"] = {
                "$ne": None,
                "$gte": float(criteria.min_match_score),
            }
        if elem_match:
            query["appearances"] = {"$elemMatch": elem_match}

        sort_field = (
            criteria.sort_by
            if criteria.sort_by in ALLOWED_SORT_FIELDS
            else "market_value_eur"
        )
        sort_direction = 1 if criteria.sort_order == 1 else -1
        safe_limit = min(max(1, int(criteria.limit)), 200)

        cursor = (
            self._players_read.find(query, {"_id": 0, "appearances": 0})
            .sort([(sort_field, sort_direction), ("player_id", 1)])
            .skip(max(0, int(criteria.offset)))
            .limit(safe_limit)
        )
        return [self._to_player_dto(doc) for doc in cursor]

    # ------------------------------------------------------------------
    # Довідники клубів і турнірів
    # ------------------------------------------------------------------

    def get_club_by_id(self, club_id: int) -> ClubDTO | None:
        doc = self._clubs_read.find_one({"club_id": int(club_id)}, {"_id": 0})
        if doc is None:
            return None
        return self._map_to_dto(ClubDTO, doc)

    def get_players_by_club(self, club_id: int, limit: int = 50) -> list[PlayerDTO]:
        safe_limit = min(max(1, int(limit)), 200)
        cursor = (
            self._players_read.find(
                {"current_club_id": int(club_id)},
                {"_id": 0, "appearances": 0},
            )
            .sort([("market_value_eur", -1)])
            .limit(safe_limit)
        )
        return [self._to_player_dto(doc) for doc in cursor]

    def get_competitions_by_ids(
        self, competition_ids: Sequence[str]
    ) -> dict[str, CompetitionDTO]:
        unique_ids = list({cid for cid in competition_ids if cid})
        if not unique_ids:
            return {}
        cursor = self._competitions_read.find(
            {"competition_id": {"$in": unique_ids}},
            {"_id": 0},
        )
        result: dict[str, CompetitionDTO] = {}
        for doc in cursor:
            dto = self._map_to_dto(CompetitionDTO, doc)
            result[dto.competition_id] = dto
        return result

    # ------------------------------------------------------------------
    # Атомарний запис виступу (Команда запису виступу)
    # ------------------------------------------------------------------

    def save_appearance(self, appearance: AppearanceDTO) -> SaveResult:
        if appearance.player_id is None:
            return SaveResult.PLAYER_NOT_FOUND

        player_id = int(appearance.player_id)
        app_date = _to_naive_utc(appearance.date)

        embedded_appearance = {
            "appearance_id": appearance.appearance_id,
            "game_id": appearance.game_id,
            "player_club_id": appearance.player_club_id,
            "competition_id": appearance.competition_id,
            "date": app_date,
            "goals": int(appearance.goals),
            "assists": int(appearance.assists),
            "minutes_played": int(appearance.minutes_played),
            "yellow_cards": int(appearance.yellow_cards),
            "red_cards": int(appearance.red_cards),
            "performance_score": appearance.performance_score,
        }

        filter_query = {
            "player_id": player_id,
            "appearances.appearance_id": {"$ne": appearance.appearance_id},
        }

        update_pipeline = [
            {
                "$set": {
                    "appearances": {
                        "$sortArray": {
                            "input": {
                                "$concatArrays": [
                                    {"$ifNull": ["$appearances", []]},
                                    [{"$literal": embedded_appearance}],
                                ]
                            },
                            "sortBy": {"date": -1},
                        }
                    },
                    "total_matches": {"$add": [{"$ifNull": ["$total_matches", 0]}, 1]},
                    "total_goals": {
                        "$add": [
                            {"$ifNull": ["$total_goals", 0]},
                            int(appearance.goals),
                        ]
                    },
                    "total_assists": {
                        "$add": [
                            {"$ifNull": ["$total_assists", 0]},
                            int(appearance.assists),
                        ]
                    },
                    "total_yellow_cards": {
                        "$add": [
                            {"$ifNull": ["$total_yellow_cards", 0]},
                            int(appearance.yellow_cards),
                        ]
                    },
                    "total_red_cards": {
                        "$add": [
                            {"$ifNull": ["$total_red_cards", 0]},
                            int(appearance.red_cards),
                        ]
                    },
                }
            },
            {
                "$set": {
                    "avg_performance_score": {
                        "$round": [{"$avg": "$appearances.performance_score"}, 2]
                    }
                }
            },
        ]

        result = self._players_critical.update_one(
            filter_query,
            update_pipeline,
            upsert=False,
        )

        if result.matched_count == 1:
            return SaveResult.SAVED

        exists = self._players_critical.count_documents(
            {"player_id": player_id},
            limit=1,
        )
        return SaveResult.DUPLICATE if exists > 0 else SaveResult.PLAYER_NOT_FOUND

    # ------------------------------------------------------------------
    # Аналітика (Категорія 6)
    # ------------------------------------------------------------------

    def get_defender_cards_stats(self) -> list[DefenderCardsStatsDTO]:
        pipeline = [
            {
                "$match": {
                    "position": "Defender",
                    "total_matches": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": {"$ifNull": ["$sub_position", "Unknown"]},
                    "players_count": {"$sum": 1},
                    "total_matches": {"$sum": "$total_matches"},
                    "total_yellow_cards": {"$sum": "$total_yellow_cards"},
                    "total_red_cards": {"$sum": "$total_red_cards"},
                }
            },
            {
                "$addFields": {
                    "total_cards": {
                        "$add": ["$total_yellow_cards", "$total_red_cards"]
                    },
                    "yellow_cards_per_match": {
                        "$round": [
                            {"$divide": ["$total_yellow_cards", "$total_matches"]},
                            4,
                        ]
                    },
                    "red_cards_per_match": {
                        "$round": [
                            {"$divide": ["$total_red_cards", "$total_matches"]},
                            4,
                        ]
                    },
                    "cards_per_match": {
                        "$round": [
                            {
                                "$divide": [
                                    {
                                        "$add": [
                                            "$total_yellow_cards",
                                            "$total_red_cards",
                                        ]
                                    },
                                    "$total_matches",
                                ]
                            },
                            4,
                        ]
                    },
                }
            },
            {"$sort": {"cards_per_match": -1}},
        ]

        rows = list(self._players_read.aggregate(pipeline))
        return [
            DefenderCardsStatsDTO(
                sub_position=row["_id"],
                players_count=int(row["players_count"]),
                total_matches=int(row["total_matches"]),
                total_yellow_cards=int(row["total_yellow_cards"]),
                total_red_cards=int(row["total_red_cards"]),
                total_cards=int(row["total_cards"]),
                yellow_cards_per_match=float(row["yellow_cards_per_match"]),
                red_cards_per_match=float(row["red_cards_per_match"]),
                cards_per_match=float(row["cards_per_match"]),
            )
            for row in rows
        ]

    def get_top_competitions_by_goals_per_match(
        self, limit: int = 10, min_matches: int = 100
    ) -> list[CompetitionStatsDTO]:
        safe_limit = min(max(1, int(limit)), 100)
        safe_min_matches = max(1, int(min_matches))

        pipeline = [
            {"$unwind": "$appearances"},
            {"$match": {"appearances.competition_id": {"$ne": None}}},
            {
                "$group": {
                    "_id": "$appearances.competition_id",
                    "appearances_count": {"$sum": 1},
                    "total_goals": {"$sum": "$appearances.goals"},
                    "total_assists": {"$sum": "$appearances.assists"},
                    "total_minutes": {"$sum": "$appearances.minutes_played"},
                    "avg_performance_score": {"$avg": "$appearances.performance_score"},
                }
            },
            {
                "$lookup": {
                    "from": "competitions",
                    "localField": "_id",
                    "foreignField": "competition_id",
                    "as": "comp",
                }
            },
            {"$unwind": "$comp"},
            {"$match": {"comp.matches_count": {"$gte": safe_min_matches}}},
            {
                "$addFields": {
                    "goals_per_match": {
                        "$round": [
                            {"$divide": ["$total_goals", "$comp.matches_count"]},
                            3,
                        ]
                    },
                    "avg_performance_score": {"$round": ["$avg_performance_score", 2]},
                }
            },
            {"$sort": {"goals_per_match": -1}},
            {"$limit": safe_limit},
        ]

        rows = list(self._players_read.aggregate(pipeline))
        return [
            CompetitionStatsDTO(
                competition_id=row["_id"],
                competition_name=row["comp"].get("competition_name"),
                country_name=row["comp"].get("country_name"),
                appearances_count=int(row["appearances_count"]),
                matches_count=int(row["comp"]["matches_count"]),
                total_goals=int(row["total_goals"]),
                total_assists=int(row["total_assists"]),
                total_minutes=int(row["total_minutes"]),
                goals_per_match=(
                    float(row["goals_per_match"])
                    if row.get("goals_per_match") is not None
                    else None
                ),
                avg_performance_score=(
                    float(row["avg_performance_score"])
                    if row.get("avg_performance_score") is not None
                    else None
                ),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Телеметрія запитів (Категорія 7)
    # ------------------------------------------------------------------

    def log_query_metrics(self, metrics: SearchQueryDTO) -> None:
        doc = asdict(metrics)
        doc["timestamp"] = _to_naive_utc(metrics.timestamp)
        self._telemetry.insert_one(doc)

    def get_recent_queries(self, limit: int = 20) -> list[SearchQueryDTO]:
        safe_limit = min(max(1, int(limit)), 200)
        cursor = (
            self._telemetry.find({}, {"_id": 0})
            .sort([("timestamp", -1)])
            .limit(safe_limit)
        )
        return [self._map_to_dto(SearchQueryDTO, doc) for doc in cursor]

    def get_telemetry_summary(
        self, window_minutes: int | None = None
    ) -> TelemetrySummaryDTO:
        stages: list[dict[str, Any]] = []
        if window_minutes is not None:
            since = utc_now_naive() - timedelta(minutes=int(window_minutes))
            stages.append({"$match": {"timestamp": {"$gte": since}}})

        stages.append(
            {
                "$facet": {
                    "overall": [
                        {
                            "$group": {
                                "_id": None,
                                "total": {"$sum": 1},
                                "avg_ms": {"$avg": "$execution_time_ms"},
                                "max_ms": {"$max": "$execution_time_ms"},
                                "hits": {"$sum": {"$cond": ["$cache_hit", 1, 0]}},
                            }
                        }
                    ],
                    "by_type": [
                        {
                            "$group": {
                                "_id": "$query_type",
                                "count": {"$sum": 1},
                                "avg_ms": {"$avg": "$execution_time_ms"},
                                "max_ms": {"$max": "$execution_time_ms"},
                                "cache_hits": {
                                    "$sum": {"$cond": ["$cache_hit", 1, 0]}
                                },
                            }
                        },
                        {"$sort": {"count": -1}},
                    ],
                }
            }
        )

        facet_res = list(self._telemetry.aggregate(stages))[0]
        overall_list = facet_res.get("overall") or []
        if not overall_list:
            return TelemetrySummaryDTO()

        overall = overall_list[0]
        total = int(overall["total"])
        hits = int(overall["hits"])
        hit_rate = round((hits / total * 100.0), 2) if total > 0 else 0.0

        by_query_type = [
            {
                "query_type": item["_id"],
                "count": int(item["count"]),
                "avg_execution_time_ms": round(float(item["avg_ms"] or 0.0), 2),
                "max_execution_time_ms": round(float(item["max_ms"] or 0.0), 2),
                "cache_hits": int(item["cache_hits"]),
            }
            for item in (facet_res.get("by_type") or [])
        ]

        return TelemetrySummaryDTO(
            total_queries=total,
            avg_execution_time_ms=round(float(overall["avg_ms"] or 0.0), 2),
            max_execution_time_ms=round(float(overall["max_ms"] or 0.0), 2),
            cache_hits=hits,
            cache_hit_rate_pct=hit_rate,
            by_query_type=by_query_type,
        )