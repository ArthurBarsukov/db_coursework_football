from typing import List
from command_service.dto import PlayerDTO, AppearanceDTO, PlayerDossierDTO, PlayerWithAppearancesDTO
from command_service.ports.output import (
    PlayerProfileRepositoryPort, AppearanceRepositoryPort, GraphRepositoryPort
)

class PlayerProfileService:
    def __init__(self, repository: PlayerProfileRepositoryPort):
        self.repository = repository

    def fetch_player_info(self, player_id: int) -> PlayerDTO:
        return self.repository.get_player_by_id(player_id)

class AppearanceService:
    def __init__(self, repository: AppearanceRepositoryPort):
        self.repository = repository

    def fetch_player_statistics(self, player_id: int) -> List[AppearanceDTO]:
        return self.repository.get_appearances_by_player(player_id)
    
class ScoutingService:
    def __init__(self, graph_repository: GraphRepositoryPort):
        self.graph_repository = graph_repository

    def find_analogous_players(self, player_id: int) -> List[PlayerDTO]:
        return self.graph_repository.find_tactical_analogs(player_id, limit=5)

class PlayerFacade:
    def __init__(
        self, 
        profile_service: PlayerProfileService, 
        appearance_service: AppearanceService,
        scouting_service: ScoutingService
    ):
        self.profile_service = profile_service
        self.appearance_service = appearance_service
        self.scouting_service = scouting_service

    def get_full_player_dossier(self, player_id: int) -> PlayerDossierDTO:
        profile = self.profile_service.fetch_player_info(player_id)
        stats = self.appearance_service.fetch_player_statistics(player_id)
        analogs = self.scouting_service.find_analogous_players(player_id)
        
        return PlayerDossierDTO(
            **profile.__dict__,
            appearances=stats,
            tactical_analogs=analogs
        )