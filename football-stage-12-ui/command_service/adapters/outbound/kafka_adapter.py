from command_service.ports.output import EventPublisherPort
from command_service.dto import AppearanceDTO

class KafkaPublisherAdapter(EventPublisherPort):
    def __init__(self, kafka_producer=None):
        self._producer = kafka_producer  # TODO: підключення до Apache Kafka

    def publish_appearance_event(self, topic: str, appearance_data: AppearanceDTO) -> None:
        # TODO: відправити протокол матчу в топік для асинхронного збереження
        raise NotImplementedError
