from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_naive() -> datetime:
    """Повертає поточний UTC-час без tzinfo для сумісності з датами ETL у BSON."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SaveResult(str, Enum):
    """Результат атомарного збереження нового виступу гравця."""

    SAVED = "SAVED"
    DUPLICATE = "DUPLICATE"
    PLAYER_NOT_FOUND = "PLAYER_NOT_FOUND"


@dataclass(kw_only=True)
class AppearanceDTO:
    """DTO окремого виступу футболіста (вбудований документ у колекції players)."""

    appearance_id: str
    player_id: int | None = None
    game_id: int | None = None
    player_club_id: int | None = None
    competition_id: str | None = None
    date: datetime
    goals: int = 0
    assists: int = 0
    minutes_played: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    performance_score: float | None = None


@dataclass(kw_only=True)
class PlayerDTO:
    """DTO профілю футболіста з кар'єрними агрегатами."""

    player_id: int
    name: str
    first_name: str | None = None
    last_name: str | None = None
    last_season: int | None = None
    current_club_id: int | None = None
    current_club_name: str | None = None
    current_club_domestic_competition_id: str | None = None
    country_of_birth: str | None = None
    city_of_birth: str | None = None
    citizenship: str | None = None
    date_of_birth: datetime | None = None
    position: str | None = None
    sub_position: str | None = None
    foot: str | None = None
    height_in_cm: int | None = None
    market_value_eur: float | None = None
    highest_market_value_eur: float | None = None
    total_matches: int = 0
    total_goals: int = 0
    total_assists: int = 0
    total_yellow_cards: int = 0
    total_red_cards: int = 0
    avg_performance_score: float | None = None


@dataclass(kw_only=True)
class PlayerWithAppearancesDTO(PlayerDTO):
    """DTO гравця разом зі зрізом вбудованих виступів."""

    appearances: list[AppearanceDTO] = field(default_factory=list)


@dataclass(kw_only=True)
class PlayerDossierDTO(PlayerWithAppearancesDTO):
    """Повне скаутське досьє гравця (категорія 3)."""

    club: ClubDTO | None = None
    competitions: dict[str, CompetitionDTO] = field(default_factory=dict)
    tactical_analogs: list[PlayerDTO] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class ClubDTO:
    """DTO футбольного клубу з довідника clubs."""

    club_id: int
    club_code: str | None = None
    club_name: str | None = None
    domestic_competition_id: str | None = None
    squad_size: int | None = None
    average_age: float | None = None
    foreigners_number: int | None = None
    foreigners_percentage: float | None = None
    national_team_players: int | None = None
    stadium_name: str | None = None
    stadium_seats: int | None = None
    coach_name: str | None = None
    last_season: int | None = None
    players_count: int = 0
    matches_count: int = 0


@dataclass(kw_only=True)
class CompetitionDTO:
    """DTO футбольного турніру з довідника competitions."""

    competition_id: str
    competition_code: str | None = None
    competition_name: str | None = None
    type: str | None = None
    sub_type: str | None = None
    country_id: int | None = None
    country_name: str | None = None
    domestic_league_code: str | None = None
    confederation: str | None = None
    domestic_players_count: int = 0
    matches_count: int = 0


@dataclass(kw_only=True)
class PlayerFilterCriteria:
    """Критерії багатокритеріальної фільтрації гравців (категорія 2)."""

    name: str | None = None
    offset: int = 0
    position: str | None = None
    sub_position: str | None = None
    foot: str | None = None
    citizenship: str | None = None
    current_club_id: int | None = None
    min_market_value_eur: float | None = None
    max_market_value_eur: float | None = None
    min_avg_score: float | None = None
    # Умови для $elemMatch по вбудованому масиву appearances
    competition_id: str | None = None
    min_match_minutes: int | None = None
    min_match_goals: int | None = None
    min_match_score: float | None = None
    sort_by: str = "market_value_eur"
    sort_order: int = -1
    limit: int = 20


@dataclass(kw_only=True)
class DefenderCardsStatsDTO:
    """Агрегована статистика карток захисників за амплуа (категорія 6)."""

    sub_position: str | None = None
    players_count: int
    total_matches: int
    total_yellow_cards: int
    total_red_cards: int
    total_cards: int
    yellow_cards_per_match: float
    red_cards_per_match: float
    cards_per_match: float


@dataclass(kw_only=True)
class CompetitionStatsDTO:
    """Агрегована статистика виступів у розрізі турнірів (категорія 6)."""

    competition_id: str
    competition_name: str | None = None
    country_name: str | None = None
    appearances_count: int = 0
    matches_count: int = 0
    total_goals: int = 0
    total_assists: int = 0
    total_minutes: int = 0
    goals_per_match: float | None = None
    avg_performance_score: float | None = None


@dataclass(kw_only=True)
class SearchQueryDTO:
    """Запис телеметрії пошукового або аналітичного запиту (категорія 7)."""

    query_type: str
    raw_query: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    execution_time_ms: float
    results_count: int
    cache_hit: bool = False
    timestamp: datetime = field(default_factory=utc_now_naive)


@dataclass(kw_only=True)
class TelemetrySummaryDTO:
    """Зведена статистика продуктивності запитів із колекції search_queries."""

    total_queries: int = 0
    avg_execution_time_ms: float = 0.0
    max_execution_time_ms: float = 0.0
    cache_hits: int = 0
    cache_hit_rate_pct: float = 0.0
    by_query_type: list[dict[str, Any]] = field(default_factory=list)