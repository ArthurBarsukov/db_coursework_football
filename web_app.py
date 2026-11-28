"""Stage 12: HTTP adapter over the existing controller/service/repository layers."""
from __future__ import annotations

import atexit
from dataclasses import replace
from datetime import date, datetime

from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

from command_service.adapters.inbound.controllers import PlayerQueryController
from command_service.adapters.outbound.mongo_adapter import MongoAdapter
from command_service.adapters.outbound.neo4j_adapter import Neo4jAdapter
from command_service.application.services import (
    AppearanceService, PlayerFacade, PlayerProfileService, ScoutingService,
)
from command_service.config import MongoSettings, create_mongo_client, get_database
from command_service.dto import PlayerFilterCriteria


class ISOJSONProvider(DefaultJSONProvider):
    @staticmethod
    def default(value):
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return DefaultJSONProvider.default(value)


def build_controller(repository):
    # The UI reads football data only; query telemetry is disabled to avoid writes.
    profiles = PlayerProfileService(repository, enable_telemetry=False)
    facade = PlayerFacade(
        profiles, AppearanceService(repository), ScoutingService(Neo4jAdapter()),
        repository, enable_telemetry=False,
    )
    return PlayerQueryController(facade, profile_service=profiles)


def create_app(repository=None):
    app = Flask(__name__)
    app.json = ISOJSONProvider(app)
    app.json.ensure_ascii = False
    if repository is None:
        settings = replace(MongoSettings.from_env(), server_selection_timeout_ms=3000)
        client = create_mongo_client(settings)
        atexit.register(client.close)
        repository = MongoAdapter(get_database(client, settings))
    controller = build_controller(repository)

    def respond(result):
        status = result.get("status")
        if status == "error":
            # Existing controllers may include driver exception text. Do not expose it.
            return jsonify(status="error", message="База даних тимчасово недоступна. Перевірте MongoDB і повторіть запит."), 503
        code = {"success": 200, "not_found": 404, "validation_error": 400}.get(status, 500)
        return jsonify(result), code

    def integer(key, default, minimum, maximum):
        raw = request.args.get(key)
        try:
            value = default if raw is None else int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"Поле {key} має бути цілим числом.") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"Поле {key}: допустимий діапазон {minimum}–{maximum}.")
        return value

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/players")
    def players():
        allowed = set(PlayerFilterCriteria.__dataclass_fields__)
        try:
            if set(request.args) - allowed or any(len(request.args.getlist(k)) != 1 for k in request.args):
                raise ValueError("Невідомі або повторені параметри запиту.")
            values = {}
            int_fields = {"current_club_id", "min_match_minutes", "min_match_goals"}
            float_fields = {"min_market_value_eur", "max_market_value_eur", "min_avg_score", "min_match_score"}
            for key in allowed - {"offset", "limit", "sort_order"}:
                raw = request.args.get(key, "").strip()
                if raw:
                    try:
                        values[key] = int(raw) if key in int_fields else float(raw) if key in float_fields else raw
                    except ValueError:
                        raise ValueError(f"Некоректне числове поле {key}.") from None
            criteria = PlayerFilterCriteria(
                **values, offset=integer("offset", 0, 0, 100_000),
                limit=integer("limit", 20, 1, 100), sort_order=integer("sort_order", -1, -1, 1),
            )
            return respond(controller.list_players(criteria))
        except ValueError as exc:
            return jsonify(status="validation_error", message=str(exc)), 400

    @app.get("/api/players/<int:player_id>")
    def player(player_id):
        try:
            if player_id < 1 or set(request.args) - {"appearances_limit", "appearances_offset"} or any(len(request.args.getlist(k)) > 1 for k in request.args):
                raise ValueError("Некоректні параметри досьє.")
            limit = integer("appearances_limit", 20, 1, 100)
            offset = integer("appearances_offset", 0, 0, 100_000)
            result = controller.get_player(player_id, appearances_limit=limit, appearances_offset=offset)
            if result.get("status") == "success":
                total = result["data"]["total_matches"]
                result["pagination"] = {"offset": offset, "limit": limit, "total": total, "has_more": offset + limit < total}
            return respond(result)
        except ValueError as exc:
            return jsonify(status="validation_error", message=str(exc)), 400

    @app.errorhandler(404)
    def missing(_error):
        if request.path.startswith("/api/"):
            return jsonify(status="not_found", message="Ресурс не знайдено."), 404
        return "Сторінку не знайдено", 404

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
