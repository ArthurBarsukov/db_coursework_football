from typing import List
from command_service.ports.output import PlayerProfileRepositoryPort, AppearanceRepositoryPort
from command_service.dto import PlayerDTO, AppearanceDTO

# Явне успадкування від портів, як в еталоні
class MongoAdapter(PlayerProfileRepositoryPort, AppearanceRepositoryPort):
    def __init__(self, mongo_client=None):
        self._mongo_client = mongo_client  # TODO: підключення до MongoDB

    def get_player_by_id(self, player_id: int) -> PlayerDTO:
        raise NotImplementedError

    def search_players_by_query(self, query: str) -> List[PlayerDTO]:
        raise NotImplementedError

    def get_appearances_by_player(self, player_id: int) -> List[AppearanceDTO]:
        raise NotImplementedError

    def save_appearance(self, appearance: AppearanceDTO) -> None:
        raise NotImplementedError