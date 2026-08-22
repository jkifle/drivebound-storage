from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def migration_graph() -> ScriptDirectory:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    return ScriptDirectory.from_config(config)


def test_migration_graph_has_one_unique_linear_head() -> None:
    graph = migration_graph()
    revisions = list(graph.walk_revisions())

    assert graph.get_heads() == ["0017"]
    assert len({revision.revision for revision in revisions}) == len(revisions)

    expected_down_revisions = {
        f"{revision:04d}": (f"{revision - 1:04d}" if revision > 1 else None)
        for revision in range(1, 18)
    }
    assert {
        revision.revision: revision.down_revision
        for revision in revisions
    } == expected_down_revisions
