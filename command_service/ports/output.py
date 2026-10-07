from typing import Protocol, List
from command_service.dto import (
    PlayerDTO, AppearanceDTO, ClubDTO, SearchQueryDTO
)

class PlayerProfileRepositoryPort(Protocol):
    def get_player_by_id(self, player_id: int) -> PlayerDTO:
        ...

    def search_players_by_query(self, query: str) -> List[PlayerDTO]:
        ...

class AppearanceRepositoryPort(Protocol):
    def get_appearances_by_player(self, player_id: int) -> List[AppearanceDTO]:
        ...

    def save_appearance(self, appearance: AppearanceDTO) -> None:
        ...

class GraphRepositoryPort(Protocol):
    def find_tactical_analogs(self, player_id: int, limit: int = 5) -> List[PlayerDTO]:
        ...
    
    def get_club_with_roster(self, club_id: int) -> ClubDTO:
        ...

class TelemetryRepositoryPort(Protocol):
    def log_query_metrics(self, metrics: SearchQueryDTO) -> None:
        ...

# --- Додаємо нові порти, які вимагають адаптери ---

class CacheStorePort(Protocol):
    def get_cached_dossier(self, player_id: int) -> dict | None:
        ...
    def set_cached_dossier(self, player_id: int, data: dict, ttl_seconds: int = 300) -> None:
        ...

class SearchIndexPort(Protocol):
    def search_by_text(self, query: str, limit: int = 10) -> List[PlayerDTO]:
        ...

class EventPublisherPort(Protocol):
    def publish_appearance_event(self, topic: str, appearance_data: AppearanceDTO) -> None:
        ...