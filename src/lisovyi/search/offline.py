"""Пошук просто з теки архіву, без БД: для тестів і вимірювальних скриптів."""

from pathlib import Path

from lisovyi.ingest.classify import MAP_TITLE, classify
from lisovyi.ingest.forest_map import parse_forest_map
from lisovyi.ingest.parsers import TranscriptStore, extract_text
from lisovyi.search.index import Bm25Index, SearchDocument
from lisovyi.search.places import PlaceResolver


def load_archive(archive_dir: Path, transcripts_dir: Path) -> tuple[Bm25Index, PlaceResolver]:
    """Індекс і резолвер з файлів архіву; document_id — порядковий номер файлу."""
    transcripts = TranscriptStore.load(transcripts_dir)
    paths = sorted(path for path in archive_dir.iterdir() if path.is_file())
    texts = {path: extract_text(path.read_bytes(), path.name, transcripts).text for path in paths}
    maps = [path for path in paths if path.stem == MAP_TITLE]
    if len(maps) != 1:
        raise ValueError(f"expected exactly one {MAP_TITLE!r} file in {archive_dir}")
    forest_map = parse_forest_map(texts[maps[0]])
    documents = []
    for document_id, path in enumerate(paths, start=1):
        classification = classify(path.stem, texts[path], list(forest_map))
        if classification is None:
            raise ValueError(f"{path.name}: cannot determine the document's scope")
        documents.append(
            SearchDocument(
                document_id=document_id,
                filename=path.name,
                title=path.stem,
                kind=classification.kind,
                scope=classification.scope,
                topic=classification.topic,
                text=texts[path],
            )
        )
    resolver = PlaceResolver(
        {glove: [part.value for part in parts] for glove, parts in forest_map.items()}
    )
    return Bm25Index.build(documents), resolver
