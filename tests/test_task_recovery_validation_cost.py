"""A recovery pass validates a DAG in proportion to artifacts, not paths."""
from collections import Counter

from test_task_work_units import accept, create, unit


def diamond(tmp_path):
    specs = [unit("a"), unit("b", dependencies=("a",)), unit("c", dependencies=("a",)),
             unit("d", dependencies=("b", "c")), unit("e", dependencies=("b", "c")),
             unit("f", dependencies=("d", "e")), unit("g", dependencies=("d", "e")),
             unit("h", dependencies=("f", "g"))]
    store = create(tmp_path, specs)
    for spec in specs:
        accept(store, store.claim(spec.unit_id, "worker-" + spec.unit_id))
    return store


def test_recovery_does_not_rehash_the_same_shared_dependency_per_path(tmp_path, monkeypatch):
    store = diamond(tmp_path)
    reads, plans = Counter(), []
    original_blob, original_meta = store._read_blob, store._meta
    def blob(ref, schema):
        reads[ref["sha256"]] += 1
        return original_blob(ref, schema)
    def meta(connection, key):
        if key == "plan":
            plans.append(key)
        return original_meta(connection, key)
    monkeypatch.setattr(store, "_read_blob", blob)
    monkeypatch.setattr(store, "_meta", meta)
    assert store.resume_plan()["reusable"] == ["a", "b", "c", "d", "e", "f", "g", "h"]
    assert len(reads) == 8
    assert sum(reads.values()) <= 2 * len(reads), "Recovery work must remain linear in distinct result artifacts"
    assert len(plans) <= 2, "One recovery pass must not repeatedly parse the full graph"


def test_one_join_reuses_its_shared_dependency_verification_within_that_read(tmp_path, monkeypatch):
    store = diamond(tmp_path)
    reads = Counter()
    original = store._read_blob
    def blob(ref, schema):
        reads[ref["sha256"]] += 1
        return original(ref, schema)
    monkeypatch.setattr(store, "_read_blob", blob)
    assert store.read_result("h")["value"] == {"ok": True}
    assert len(reads) == 8
    assert sum(reads.values()) <= 2 * len(reads)
