from typing import List
from command_service.ports.output import SearchIndexPort
from command_service.dto import PlayerDTO

class ElasticsearchAdapter(SearchIndexPort):
    def __init__(self, es_client=None):
        self._es_client = es_client  # TODO: підключення до кластера Elasticsearch

    def search_by_text(self, query: str, limit: int = 10) -> List[PlayerDTO]:
        # TODO: реалізувати інвертований пошук за полем full_scouting_report
        raise NotImplementedError