"""Registry ↔ model consistency + describe() contract (the schema the agent sees)."""

from app.schema_registry import EXACT_OPS, RECORD_TYPES, describe


def test_every_registry_column_exists_on_its_model():
    for rt in RECORD_TYPES.values():
        for name, col in rt.exact.items():
            assert hasattr(rt.model, col.attr), f"{rt.name}.{name}: missing attr {col.attr}"
            assert col.type in EXACT_OPS, f"{rt.name}.{name}: unknown type {col.type}"
        for name, fz in rt.fuzzy.items():
            assert hasattr(rt.model, fz.attr), f"{rt.name}.{name}: missing attr {fz.attr}"
            assert hasattr(rt.model, fz.emb_attr), f"{rt.name}.{name}: missing emb attr {fz.emb_attr}"


def test_fuzzy_embedding_columns_are_distinct_per_type():
    # Two fuzzy columns must never share an embedding column (a shared vector
    # would silently answer for the wrong text).
    for rt in RECORD_TYPES.values():
        embs = [fz.emb_attr for fz in rt.fuzzy.values()]
        assert len(embs) == len(set(embs)), f"{rt.name}: shared embedding column"


def test_provenance_formats_reference_real_attrs():
    import string

    for rt in RECORD_TYPES.values():
        fields = [f for _, f, _, _ in string.Formatter().parse(rt.provenance) if f]
        for field in fields:
            assert hasattr(rt.model, field), f"{rt.name}: provenance field {field} not on model"


def test_describe_contract():
    out = describe(0.3)
    assert set(out["record_types"]) == set(RECORD_TYPES)
    assert out["fuzzy"]["default_threshold"] == 0.3
    for name, spec in out["record_types"].items():
        rt = RECORD_TYPES[name]
        assert set(spec["columns"]) == set(rt.exact) | set(rt.fuzzy)
        for col_name, col_spec in spec["columns"].items():
            assert col_spec["kind"] in ("exact", "fuzzy")
            assert col_spec["description"]


def test_common_columns_present_everywhere():
    for rt in RECORD_TYPES.values():
        assert "owner" in rt.exact, rt.name
        assert "occurred_at" in rt.exact, rt.name
