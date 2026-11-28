from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from dataclasses import asdict, fields, replace
from datetime import datetime

from command_service.domain import calculate_performance_score
from command_service.dto import (
    AppearanceDTO,
    ClubDTO,
    CompetitionDTO,
    CompetitionStatsDTO,
    DefenderCardsStatsDTO,
    PlayerDossierDTO,
    PlayerDTO,
    PlayerFilterCriteria,
    PlayerWithAppearancesDTO,
    SaveResult,
    SearchQueryDTO,
    TelemetrySummaryDTO,
)
from command_service.ports.output import (
    AnalyticsRepositoryPort,
    AppearanceRepositoryPort,
    GraphRepositoryPort,
    PlayerProfileRepositoryPort,
    ReferenceRepositoryPort,
    TelemetryRepositoryPort,
)

logger = logging.getLogger(__name__)

MAX_REALISTIC_MINUTES = 130


def _safe_log_telemetry(
    telemetry: TelemetryRepositoryPort | None,
    metrics: SearchQueryDTO,
) -> None:
    """Записує метрики запиту, логуючи помилку без переривання основної операції."""
    if telemetry is None:
        return
    try:
        telemetry.log_query_metrics(metrics)
    except Exception as exc:
        logger.warning(
            "Не вдалося записати телеметрію для query_type=%r: %s",
            metrics.query_type,
            exc,
        )


class PlayerProfileService:
    """Прикладний сервіс читання та багатокритеріальної фільтрації профілів гравців."""

    def __init__(
        self,
        repository: PlayerProfileRepositoryPort,
        telemetry_repository: TelemetryRepositoryPort | None = None,
        *,
        enable_telemetry: bool = True,
    ) -> None:
        self.repository = repository
        self.telemetry_repository = telemetry_repository
        self.enable_telemetry = enable_telemetry

    def fetch_player_info(
        self, player_id: int, *, consistent_read: bool = False
    ) -> PlayerDTO | None:
        return self.repository.get_player_by_id(
            player_id, consistent_read=consistent_read
        )

    def fetch_player_with_appearances(
        self,
        player_id: int,
        limit: int = 20,
        *,
        consistent_read: bool = False,
        offset: int = 0,
    ) -> PlayerWithAppearancesDTO | None:
        return self.repository.get_player_with_appearances(
            player_id, limit=limit, consistent_read=consistent_read, offset=offset
        )

    def filter_players(self, criteria: PlayerFilterCriteria) -> list[PlayerDTO]:
        self.validate_filter(criteria)
        t0 = time.perf_counter()
        results = self.repository.filter_players(criteria)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        if self.enable_telemetry:
            active_params = {
                k: v for k, v in asdict(criteria).items() if v is not None
            }
            _safe_log_telemetry(
                self.telemetry_repository,
                SearchQueryDTO(
                    query_type="filter_players",
                    parameters=active_params,
                    execution_time_ms=elapsed_ms,
                    results_count=len(results),
                    cache_hit=False,
                ),
            )
        return results

    @staticmethod
    def validate_filter(criteria: PlayerFilterCriteria) -> None:
        if not 1 <= criteria.limit <= 200 or not 0 <= criteria.offset <= 100_000:
            raise ValueError("Некоректні параметри пагінації.")
        if criteria.sort_by not in {"market_value_eur", "avg_performance_score", "total_goals", "total_matches"}:
            raise ValueError("Невідоме поле сортування.")
        if criteria.sort_order not in {-1, 1}:
            raise ValueError("Напрям сортування має бути -1 або 1.")
        for key in ("name", "position", "sub_position", "foot", "citizenship", "competition_id"):
            value = getattr(criteria, key)
            if value is not None and (not isinstance(value, str) or len(value) > 100):
                raise ValueError(f"Некоректне поле {key}.")
        for key in ("min_market_value_eur", "max_market_value_eur", "min_avg_score", "min_match_minutes", "min_match_goals", "min_match_score"):
            value = getattr(criteria, key)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"Поле {key} має бути невід'ємним скінченним числом.")
        if criteria.current_club_id is not None and criteria.current_club_id < 1:
            raise ValueError("ID клубу має бути додатним.")
        if criteria.min_market_value_eur is not None and criteria.max_market_value_eur is not None and criteria.min_market_value_eur > criteria.max_market_value_eur:
            raise ValueError("Мінімальна вартість перевищує максимальну.")
        for key in ("min_avg_score", "min_match_score"):
            if getattr(criteria, key) is not None and getattr(criteria, key) > 10:
                raise ValueError("Рейтинг має бути в діапазоні 0–10.")

    def list_players(self, criteria: PlayerFilterCriteria) -> tuple[list[PlayerDTO], bool]:
        """Read one extra result to detect the next page without a second count query."""
        self.validate_filter(criteria)
        if criteria.limit > 100:
            raise ValueError("На сторінці може бути не більше 100 записів.")
        players = self.filter_players(replace(criteria, limit=criteria.limit + 1))
        return players[:criteria.limit], len(players) > criteria.limit


class AppearanceService:
    """Прикладний сервіс роботи з виступами футболіста (читання та командний запис)."""

    def __init__(self, repository: AppearanceRepositoryPort) -> None:
        self.repository = repository

    def fetch_player_statistics(
        self,
        player_id: int,
        limit: int = 50,
        *,
        consistent_read: bool = False,
    ) -> list[AppearanceDTO]:
        return self.repository.get_appearances_by_player(
            player_id, limit=limit, consistent_read=consistent_read
        )

    @staticmethod
    def _validate_appearance(appearance: AppearanceDTO) -> None:
        if not isinstance(appearance.date, datetime):
            raise ValueError("Поле 'date' має бути об'єктом datetime.")

        if appearance.minutes_played < 0 or appearance.minutes_played > MAX_REALISTIC_MINUTES:
            raise ValueError(
                f"Некоректна тривалість виступу: {appearance.minutes_played} хв "
                f"(допустимий діапазон 0..{MAX_REALISTIC_MINUTES})."
            )

        for field_name, value in (
            ("goals", appearance.goals),
            ("assists", appearance.assists),
            ("yellow_cards", appearance.yellow_cards),
            ("red_cards", appearance.red_cards),
        ):
            if value < 0:
                raise ValueError(
                    f"Поле '{field_name}' не може бути від'ємним (отримано {value})."
                )

    def record_appearance(self, appearance: AppearanceDTO) -> SaveResult:
        if appearance.player_id is None:
            return SaveResult.PLAYER_NOT_FOUND

        self._validate_appearance(appearance)

        calculated_score = calculate_performance_score(
            goals=appearance.goals,
            assists=appearance.assists,
            minutes_played=appearance.minutes_played,
            yellow_cards=appearance.yellow_cards,
            red_cards=appearance.red_cards,
        )
        prepared_appearance = replace(appearance, performance_score=calculated_score)
        return self.repository.save_appearance(prepared_appearance)


class ScoutingService:
    """Сервіс пошуку тактичних аналогів через графовий репозиторій."""

    def __init__(self, graph_repository: GraphRepositoryPort) -> None:
        self.graph_repository = graph_repository

    def find_analogous_players(
        self, player_id: int, limit: int = 5
    ) -> list[PlayerDTO]:
        return self.graph_repository.find_tactical_analogs(player_id, limit=limit)


class ReferenceService:
    """Прикладний сервіс довідників клубів, їхніх складів і турнірів."""

    def __init__(self, reference_repository: ReferenceRepositoryPort) -> None:
        self.reference_repository = reference_repository

    def get_club_by_id(self, club_id: int) -> ClubDTO | None:
        return self.reference_repository.get_club_by_id(club_id)

    def get_club_roster(
        self, club_id: int, limit: int = 50
    ) -> tuple[ClubDTO | None, list[PlayerDTO]]:
        club = self.reference_repository.get_club_by_id(club_id)
        if club is None:
            return None, []
        players = self.reference_repository.get_players_by_club(club_id, limit=limit)
        return club, players

    def get_competitions_by_ids(
        self, competition_ids: Sequence[str]
    ) -> dict[str, CompetitionDTO]:
        return self.reference_repository.get_competitions_by_ids(competition_ids)


class AnalyticsService:
    """Сервіс агрегаційної аналітики (категорія 6 запитів)."""

    def __init__(
        self,
        analytics_repository: AnalyticsRepositoryPort,
        telemetry_repository: TelemetryRepositoryPort | None = None,
        *,
        enable_telemetry: bool = True,
    ) -> None:
        self.analytics_repository = analytics_repository
        self.telemetry_repository = telemetry_repository
        self.enable_telemetry = enable_telemetry

    def get_defender_cards_stats(self) -> list[DefenderCardsStatsDTO]:
        t0 = time.perf_counter()
        stats = self.analytics_repository.get_defender_cards_stats()
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        if self.enable_telemetry:
            _safe_log_telemetry(
                self.telemetry_repository,
                SearchQueryDTO(
                    query_type="defender_cards_stats",
                    parameters={"position": "Defender"},
                    execution_time_ms=elapsed_ms,
                    results_count=len(stats),
                    cache_hit=False,
                ),
            )
        return stats

    def get_top_competitions_by_goals_per_match(
        self, limit: int = 10, min_matches: int = 100
    ) -> list[CompetitionStatsDTO]:
        t0 = time.perf_counter()
        stats = self.analytics_repository.get_top_competitions_by_goals_per_match(
            limit=limit, min_matches=min_matches
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        if self.enable_telemetry:
            _safe_log_telemetry(
                self.telemetry_repository,
                SearchQueryDTO(
                    query_type="top_competitions_goals_per_match",
                    parameters={"limit": limit, "min_matches": min_matches},
                    execution_time_ms=elapsed_ms,
                    results_count=len(stats),
                    cache_hit=False,
                ),
            )
        return stats


class TelemetryService:
    """Сервіс перегляду та зведення телеметрії запитів (категорія 7)."""

    def __init__(self, telemetry_repository: TelemetryRepositoryPort) -> None:
        self.telemetry_repository = telemetry_repository

    def get_recent_queries(self, limit: int = 20) -> list[SearchQueryDTO]:
        return self.telemetry_repository.get_recent_queries(limit=limit)

    def get_telemetry_summary(
        self, window_minutes: int | None = None
    ) -> TelemetrySummaryDTO:
        return self.telemetry_repository.get_telemetry_summary(
            window_minutes=window_minutes
        )


class PlayerFacade:
    """Фасад агрегації повного скаутського досьє гравця з деградацією при збоях Neo4j."""

    def __init__(
        self,
        profile_service: PlayerProfileService,
        appearance_service: AppearanceService,
        scouting_service: ScoutingService,
        reference_repository: ReferenceRepositoryPort,
        telemetry_repository: TelemetryRepositoryPort | None = None,
        *,
        enable_telemetry: bool = True,
    ) -> None:
        self.profile_service = profile_service
        self.appearance_service = appearance_service
        self.scouting_service = scouting_service
        self.reference_repository = reference_repository
        self.telemetry_repository = telemetry_repository
        self.enable_telemetry = enable_telemetry

    def get_full_player_dossier(
        self,
        player_id: int,
        appearances_limit: int = 20,
        *,
        consistent_read: bool = False,
        appearances_offset: int = 0,
    ) -> PlayerDossierDTO | None:
        t0 = time.perf_counter()

        player_with_apps = self.profile_service.fetch_player_with_appearances(
            player_id,
            limit=appearances_limit,
            consistent_read=consistent_read,
            offset=appearances_offset,
        )
        if player_with_apps is None:
            if self.enable_telemetry:
                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                _safe_log_telemetry(
                    self.telemetry_repository,
                    SearchQueryDTO(
                        query_type="player_dossier",
                        parameters={
                            "player_id": player_id,
                            "appearances_limit": appearances_limit,
                            "consistent_read": consistent_read,
                        },
                        execution_time_ms=elapsed_ms,
                        results_count=0,
                        cache_hit=False,
                    ),
                )
            return None

        warnings: list[str] = []
        club: ClubDTO | None = None
        competitions: dict[str, CompetitionDTO] = {}

        if player_with_apps.current_club_id is not None:
            club = self.reference_repository.get_club_by_id(
                player_with_apps.current_club_id
            )

        comp_ids: set[str] = {
            app.competition_id
            for app in player_with_apps.appearances
            if app.competition_id
        }
        if player_with_apps.current_club_domestic_competition_id:
            comp_ids.add(player_with_apps.current_club_domestic_competition_id)

        if comp_ids:
            competitions = self.reference_repository.get_competitions_by_ids(
                list(comp_ids)
            )

        try:
            analogs = self.scouting_service.find_analogous_players(player_id)
        except Exception:
            analogs = []
            warnings.append("tactical_analogs_unavailable")

        base_data = {
            f.name: getattr(player_with_apps, f.name)
            for f in fields(PlayerWithAppearancesDTO)
        }
        dossier = PlayerDossierDTO(
            **base_data,
            club=club,
            competitions=competitions,
            tactical_analogs=analogs,
            warnings=warnings,
        )

        if self.enable_telemetry:
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            _safe_log_telemetry(
                self.telemetry_repository,
                SearchQueryDTO(
                    query_type="player_dossier",
                    parameters={
                        "player_id": player_id,
                        "appearances_limit": appearances_limit,
                        "consistent_read": consistent_read,
                    },
                    execution_time_ms=elapsed_ms,
                    results_count=1,
                    cache_hit=False,
                ),
            )
        return dossier