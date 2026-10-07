from command_service.dto import PlayerDTO, AppearanceDTO
from command_service.application.services import (
    PlayerProfileService, AppearanceService, ScoutingService, PlayerFacade
)
from command_service.adapters.inbound.controllers import (
    PlayerQueryController, AppearanceCommandController
)

# Імпортуємо всі наші вихідні адаптери (Outbound)
from command_service.adapters.outbound.mongo_adapter import MongoAdapter
from command_service.adapters.outbound.neo4j_adapter import Neo4jAdapter
from command_service.adapters.outbound.redis_adapter import RedisAdapter
from command_service.adapters.outbound.elastic_adapter import ElasticsearchAdapter
from command_service.adapters.outbound.kafka_adapter import KafkaPublisherAdapter

print("--- 1. Ініціалізація Інфраструктури (Dependency Injection) ---")
# Створюємо об'єкти підключень до баз даних, кешу та черг
mongo_db = MongoAdapter()
neo4j_db = Neo4jAdapter()
redis_cache = RedisAdapter()
elastic_search = ElasticsearchAdapter()
kafka_queue = KafkaPublisherAdapter()

print("--- 2. Збірка Доменного ядра ---")
# Передаємо бази даних у Сервіси
profile_service = PlayerProfileService(repository=mongo_db)
appearance_service = AppearanceService(repository=mongo_db)
scouting_service = ScoutingService(graph_repository=neo4j_db)

# Збираємо Фасад
facade = PlayerFacade(profile_service, appearance_service, scouting_service)

print("--- 3. Ініціалізація Вхідних Контролерів ---")
# (У реальному проєкті redis_cache передавався б у QueryController для кешування, 
# а kafka_queue - у CommandController для асинхронного запису)
query_controller = PlayerQueryController(player_facade=facade)
command_controller = AppearanceCommandController(appearance_service=appearance_service)

print("--- 4. Тестовий запуск системи ---")
response = query_controller.get_player(player_id=1)
print(response)