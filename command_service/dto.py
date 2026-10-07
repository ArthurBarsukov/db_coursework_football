from dataclasses import dataclass
from typing import List
import uuid

@dataclass
class AppearanceDTO:
    appearance_id: str
    player_id: int
    player_club_id: int
    competition_id: str
    date: str
    minutes_played: int
    goals: int
    assists: int
    yellow_cards: int
    red_cards: int
    performance_score: float

@dataclass
class PlayerDTO:
    player_id: int
    name: str
    position: str
    sub_position: str
    foot: str
    citizenship: str
    current_club_name: str
    market_value_eur: float
    total_goals: int
    total_assists: int
    total_yellow_cards: int
    total_red_cards: int
    avg_performance_score: float

@dataclass
class PlayerWithAppearancesDTO(PlayerDTO):
    appearances: List[AppearanceDTO]

@dataclass
class PlayerDossierDTO(PlayerWithAppearancesDTO):
    tactical_analogs: List[PlayerDTO]
    
@dataclass
class ClubDTO:
    club_id: int
    club_name: str
    players_count: int

@dataclass
class CompetitionDTO:
    competition_id: str
    competition_name: str
    matches_count: int

@dataclass
class SearchQueryDTO:
    query_id: uuid.UUID
    raw_query: str
    normalized_query: str
    cache_hit: bool
    latency_ms: int