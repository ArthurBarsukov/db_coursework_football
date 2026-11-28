from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from command_service.application.services import (
    AnalyticsService,
    AppearanceService,
    PlayerFacade,
    PlayerProfileService,
    ReferenceService,
    TelemetryService,
)
from command_service.dto import AppearanceDTO, PlayerFilterCriteria, SaveResult


def _parse_int_field(
    data: dict[str, Any],
    field_name: str,
    *,
    default: int | None = 0,
    allow_none: bool = False,
) -> int | None:
    """Безпечно приводить поле вхідного запиту до int, відхиляючи None за замовчуванням."""
    if field_name not in data:
        return default
    raw = data[field_name]
    if raw is None:
        if allow_none:
            return None
        raise ValueError(f"Поле '{field_name}' не може бути None.")
    if isinstance(raw, bool):
        raise ValueError(f"Поле '{field_name}' має бути цілим числом, а не bool.")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Поле '{field_name}' має бути цілим числом (отримано {raw!r})."
        ) from exc


def _parse_naive_utc_date(raw_date: Any) -> datetime:
    """Приймає datetime або ISO-рядок і повертає наївний UTC datetime без tzinfo."""
    if raw_date is None:
        raise ValueError("Поле 'date' є обов'язковим і не може бути None.")

    if isinstance(raw_date, datetime):
        dt = raw_date
    elif isinstance(raw_date, str):
        text = raw_date.strip()
        if not text:
            raise ValueError("Поле 'date' не може бути порожнім рядком.")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"Некоректний ISO-формат дати у полі 'date': {raw_date!r}."
            ) from exc
    else:
        raise ValueError(
            f"Поле 'date' має бути datetime або ISO-рядком (отримано {type(raw_date).__name__})."
        )

    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class PlayerQueryController:
    """Вхідний контролер сценаріїв читання, фільтрації, аналітики та телеметрії."""

    def __init__(
        self,
        player_facade: PlayerFacade,
        profile_service: PlayerProfileService | None = None,
        reference_service: ReferenceService | None = None,
        analytics_service: AnalyticsService | None = None,
        telemetry_service: TelemetryService | None = None,
    ) -> None:
        self.player_facade = player_facade
        self.profile_service = profile_service
        self.reference_service = reference_service
        self.analytics_service = analytics_service
        self.telemetry_service = telemetry_service

    def get_player(
        self,
        player_id: int,
        appearances_limit: int = 20,
        *,
        consistent_read: bool = False,
        appearances_offset: int = 0,
    ) -> dict[str, Any]:
        try:
            dossier = self.player_facade.get_full_player_dossier(
                player_id=int(player_id),
                appearances_limit=appearances_limit,
                consistent_read=consistent_read,
                appearances_offset=appearances_offset,
            )
            if dossier is None:
                return {
                    "status": "not_found",
                    "message": f"Player with player_id={player_id} not found",
                }
            return {"status": "success", "data": asdict(dossier)}
        except ValueError as exc:
            return {"status": "validation_error", "message": str(exc)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def filter_players(self, criteria: PlayerFilterCriteria) -> dict[str, Any]:
        if self.profile_service is None:
            return {
                "status": "error",
                "message": "PlayerProfileService is not configured in controller",
            }
        try:
            players = self.profile_service.filter_players(criteria)
            return {
                "status": "success",
                "count": len(players),
                "data": [asdict(p) for p in players],
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def list_players(self, criteria: PlayerFilterCriteria) -> dict[str, Any]:
        if self.profile_service is None:
            return {"status": "error", "message": "PlayerProfileService is not configured"}
        try:
            players, has_more = self.profile_service.list_players(criteria)
            return {
                "status": "success", "data": [asdict(p) for p in players],
                "count": len(players),
                "pagination": {"offset": criteria.offset, "limit": criteria.limit, "has_more": has_more},
            }
        except ValueError as exc:
            return {"status": "validation_error", "message": str(exc)}
        except Exception:
            return {"status": "error", "message": "Не вдалося отримати дані."}

    def get_club_roster(self, club_id: int, limit: int = 50) -> dict[str, Any]:
        if self.reference_service is None:
            return {
                "status": "error",
                "message": "ReferenceService is not configured in controller",
            }
        try:
            club, players = self.reference_service.get_club_roster(
                int(club_id), limit=limit
            )
            if club is None:
                return {
                    "status": "not_found",
                    "message": f"Club with club_id={club_id} not found",
                }
            return {
                "status": "success",
                "data": {
                    "club": asdict(club),
                    "players": [asdict(p) for p in players],
                },
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def get_defender_cards_stats(self) -> dict[str, Any]:
        if self.analytics_service is None:
            return {
                "status": "error",
                "message": "AnalyticsService is not configured in controller",
            }
        try:
            stats = self.analytics_service.get_defender_cards_stats()
            return {"status": "success", "data": [asdict(item) for item in stats]}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def get_top_competitions(
        self, limit: int = 10, min_matches: int = 100
    ) -> dict[str, Any]:
        if self.analytics_service is None:
            return {
                "status": "error",
                "message": "AnalyticsService is not configured in controller",
            }
        try:
            stats = self.analytics_service.get_top_competitions_by_goals_per_match(
                limit=limit, min_matches=min_matches
            )
            return {"status": "success", "data": [asdict(item) for item in stats]}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def get_recent_queries(self, limit: int = 20) -> dict[str, Any]:
        if self.telemetry_service is None:
            return {
                "status": "error",
                "message": "TelemetryService is not configured in controller",
            }
        try:
            queries = self.telemetry_service.get_recent_queries(limit=limit)
            return {"status": "success", "data": [asdict(q) for q in queries]}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def get_telemetry_summary(
        self, window_minutes: int | None = None
    ) -> dict[str, Any]:
        if self.telemetry_service is None:
            return {
                "status": "error",
                "message": "TelemetryService is not configured in controller",
            }
        try:
            summary = self.telemetry_service.get_telemetry_summary(
                window_minutes=window_minutes
            )
            return {"status": "success", "data": asdict(summary)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


class AppearanceCommandController:
    """Вхідний контролер командного запису виступів футболіста."""

    def __init__(self, appearance_service: AppearanceService) -> None:
        self.appearance_service = appearance_service

    @staticmethod
    def _build_appearance_dto(request_data: dict[str, Any]) -> AppearanceDTO:
        if not isinstance(request_data, dict):
            raise ValueError("Тіло запиту має бути словником.")

        raw_app_id = request_data.get("appearance_id")
        if raw_app_id is None or not str(raw_app_id).strip():
            raise ValueError(
                "Поле 'appearance_id' є обов'язковим і не може бути порожнім."
            )
        appearance_id = str(raw_app_id).strip()

        player_id = _parse_int_field(
            request_data, "player_id", default=None, allow_none=True
        )
        game_id = _parse_int_field(
            request_data, "game_id", default=None, allow_none=True
        )
        player_club_id = _parse_int_field(
            request_data, "player_club_id", default=None, allow_none=True
        )

        raw_comp_id = request_data.get("competition_id")
        competition_id = (
            str(raw_comp_id).strip()
            if raw_comp_id is not None and str(raw_comp_id).strip()
            else None
        )

        app_date = _parse_naive_utc_date(request_data.get("date"))

        goals = _parse_int_field(request_data, "goals", default=0)
        assists = _parse_int_field(request_data, "assists", default=0)
        minutes_played = _parse_int_field(request_data, "minutes_played", default=0)
        yellow_cards = _parse_int_field(request_data, "yellow_cards", default=0)
        red_cards = _parse_int_field(request_data, "red_cards", default=0)

        assert goals is not None
        assert assists is not None
        assert minutes_played is not None
        assert yellow_cards is not None
        assert red_cards is not None

        return AppearanceDTO(
            appearance_id=appearance_id,
            player_id=player_id,
            game_id=game_id,
            player_club_id=player_club_id,
            competition_id=competition_id,
            date=app_date,
            goals=goals,
            assists=assists,
            minutes_played=minutes_played,
            yellow_cards=yellow_cards,
            red_cards=red_cards,
        )

    def record_match_appearance(self, request_data: dict[str, Any]) -> dict[str, Any]:
        try:
            appearance_dto = self._build_appearance_dto(request_data)
            result = self.appearance_service.record_appearance(appearance_dto)

            if result is SaveResult.SAVED:
                return {
                    "status": "success",
                    "result": result.value,
                    "message": "Appearance recorded successfully",
                }
            if result is SaveResult.DUPLICATE:
                return {
                    "status": "duplicate",
                    "result": result.value,
                    "message": f"Appearance '{appearance_dto.appearance_id}' already exists",
                }
            return {
                "status": "not_found",
                "result": result.value,
                "message": f"Player with player_id={appearance_dto.player_id} not found",
            }
        except ValueError as exc:
            return {"status": "validation_error", "message": str(exc)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}