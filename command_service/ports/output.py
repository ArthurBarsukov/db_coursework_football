from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

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
)


class PlayerProfileRepositoryPort(Protocol):
    """Порт читання та багатокритеріальної фільтрації профілів гравців."""

    def get_player_by_id(
        self, player_id: int, *, consistent_read: bool = False
    ) -> PlayerDTO | None:
        ...

    def get_player_with_appearances(
        self, player_id: int, limit: int = 20, *, consistent_read: bool = False, offset: int = 0
    ) -> PlayerWithAppearancesDTO | None:
        ...

    def filter_players(self, criteria: PlayerFilterCriteria) -> list[PlayerDTO]:
        ...


class AppearanceRepositoryPort(Protocol):
    """Порт читання та атомарного запису виступів футболіста."""

    def get_appearances_by_player(
        self, player_id: int, limit: int = 50, *, consistent_read: bool = False
    ) -> list[AppearanceDTO]:
        ...

    def save_appearance(self, appearance: AppearanceDTO) -> SaveResult:
        ...


class ReferenceRepositoryPort(Protocol):
    """Порт доступу до довідників клубів, їхніх складів і турнірів."""

    def get_club_by_id(self, club_id: int) -> ClubDTO | None:
        ...

    def get_players_by_club(
        self, club_id: int, limit: int = 50
    ) -> list[PlayerDTO]:
        ...

    def get_competitions_by_ids(
        self, competition_ids: Sequence[str]
    ) -> dict[str, CompetitionDTO]:
        ...


class AnalyticsRepositoryPort(Protocol):
    """Порт агрегаційної аналітики."""

    def get_defender_cards_stats(self) -> list[DefenderCardsStatsDTO]:
        ...

    def get_top_competitions_by_goals_per_match(
        self, limit: int = 10, min_matches: int = 100
    ) -> list[CompetitionStatsDTO]:
        ...


class TelemetryRepositoryPort(Protocol):
    """Порт збереження та аналізу телеметрії запитів."""

    def log_query_metrics(self, metrics: SearchQueryDTO) -> None:
        ...

    def get_recent_queries(self, limit: int = 20) -> list[SearchQueryDTO]:
        ...

    def get_telemetry_summary(
        self, window_minutes: int | None = None
    ) -> TelemetrySummaryDTO:
        ...


class GraphRepositoryPort(Protocol):
    """Порт графової БД (Neo4j) для тактичних аналогів та зв'язків складу."""

    def find_tactical_analogs(
        self, player_id: int, limit: int = 5
    ) -> list[PlayerDTO]:
        ...

    def get_club_with_roster(self, club_id: int) -> ClubDTO | None:
        ...


class CacheStorePort(Protocol):
    """Порт кешування скаутських досьє (Redis)."""

    def get_cached_dossier(self, player_id: int, *, appearances_limit: int = 20, appearances_offset: int = 0) -> dict | None:
        ...

    def set_cached_dossier(
        self, player_id: int, data: dict, ttl_seconds: int = 300,
        *, appearances_limit: int = 20, appearances_offset: int = 0
    ) -> None:
        ...


class SearchIndexPort(Protocol):
    """Порт повнотекстового нечіткого пошуку (Elasticsearch)."""

    def search_by_text(self, query: str, limit: int = 10) -> list[PlayerDTO]:
        ...


class EventPublisherPort(Protocol):
    """Порт публікації подій у брокер повідомлень (Kafka)."""

    def publish_appearance_event(
        self, topic: str, appearance_data: AppearanceDTO
    ) -> None:
        ...
