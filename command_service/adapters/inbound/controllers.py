from command_service.dto import AppearanceDTO
from command_service.application.services import AppearanceService, PlayerFacade

class PlayerQueryController:
    def __init__(self, player_facade: PlayerFacade):
        self.player_facade = player_facade

    def get_player(self, player_id: int) -> dict:
        try:
            dossier = self.player_facade.get_full_player_dossier(player_id)
            return {"status": "success", "data": dossier.__dict__}
        except Exception as e:
            return {"status": "error", "message": str(e)}

class AppearanceCommandController:
    def __init__(self, appearance_service: AppearanceService):
        self.appearance_service = appearance_service

    def record_match_appearance(self, request_data: dict) -> dict:
        try:
            appearance_dto = AppearanceDTO(**request_data)
            self.appearance_service.repository.save_appearance(appearance_dto)
            return {"status": "success", "message": "Appearance recorded successfully"}
        except Exception as e:
            return {"status": "error", "message": str(e)}