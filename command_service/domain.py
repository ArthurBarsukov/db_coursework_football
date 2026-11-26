from __future__ import annotations
from typing import Any, Sequence

# Константи формули розрахунку performance_score
BASE_SCORE: float = 5.0
GOALS_WEIGHT: float = 1.5
ASSISTS_WEIGHT: float = 1.0
MINUTES_WEIGHT: float = 0.5
MINUTES_PER_GAME: float = 90.0
YELLOW_CARD_PENALTY: float = 0.5
RED_CARD_PENALTY: float = 2.0
MIN_SCORE: float = 0.0
MAX_SCORE: float = 10.0
SCORE_DECIMALS: int = 2
MIN_MINUTES_FOR_RATING: int = 10


def calculate_performance_score(
    goals: int = 0,
    assists: int = 0,
    minutes_played: int = 0,
    yellow_cards: int = 0,
    red_cards: int = 0,
) -> float | None:
    """Скалярний розрахунок рейтингу виступу (None, якщо minutes_played < 10)."""
    if minutes_played < MIN_MINUTES_FOR_RATING:
        return None

    raw_score = (
        BASE_SCORE
        + goals * GOALS_WEIGHT
        + assists * ASSISTS_WEIGHT
        + MINUTES_WEIGHT * (minutes_played / MINUTES_PER_GAME)
        - yellow_cards * YELLOW_CARD_PENALTY
        - red_cards * RED_CARD_PENALTY
    )
    clamped_score = min(MAX_SCORE, max(MIN_SCORE, float(raw_score)))
    return round(clamped_score, SCORE_DECIMALS)


def calculate_player_overall_score(
    appearance_scores: Sequence[float | None],
) -> float | None:
    """Середній рейтинг гравця за оціненими виступами (ігнорує None)."""
    rated = [s for s in appearance_scores if s is not None]
    if not rated:
        return None
    return round(float(sum(rated) / len(rated)), SCORE_DECIMALS)


def build_full_scouting_report(
    player_id: int,
    full_name: str,
    position: str | None,
    market_value_eur: float | int | None,
    appearances: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Заглушка: динамічне формування повного скаутського звіту (реалізується на етапі Elasticsearch)."""
    raise NotImplementedError(
        "build_full_scouting_report ще не реалізовано (заплановано для етапу Elasticsearch)."
    )