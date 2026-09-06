from sqlalchemy import create_engine, text

from tgmon import bootstrap


def test_existing_wiki_table_gains_review_columns_without_losing_rows(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(bootstrap, "engine", engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE knowledge_page (id INTEGER PRIMARY KEY, title TEXT)"))
        connection.execute(text("INSERT INTO knowledge_page VALUES (1, 'Original Wiki Page')"))
    bootstrap._ensure_columns()
    bootstrap._ensure_columns()
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(knowledge_page)"))}
        assert {"aliases", "missing_count", "review_reason", "reviewed_at"} <= columns
        assert connection.execute(text("SELECT title FROM knowledge_page WHERE id = 1")).scalar_one() == "Original Wiki Page"
    engine.dispose()
