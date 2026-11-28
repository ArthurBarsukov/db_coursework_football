from typing import List
from command_service.ports.output import GraphRepositoryPort
from command_service.dto import PlayerDTO, ClubDTO

class Neo4jAdapter(GraphRepositoryPort):
    def __init__(self, neo4j_client=None):
        self._neo4j_client = neo4j_client  # TODO: підключення до Neo4j

    def find_tactical_analogs(self, player_id: int, limit: int = 5) -> List[PlayerDTO]:
        raise NotImplementedError
    
    def get_club_with_roster(self, club_id: int) -> ClubDTO:
        raise NotImplementedError