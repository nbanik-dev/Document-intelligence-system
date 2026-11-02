from app.search import chunk_text, embed
import numpy as np


def sim(a, b):
    return float(np.dot(embed(a), embed(b)))


def test_embedding_is_normalised_and_deterministic():
    v = np.asarray(embed("invoice from acme"))
    assert abs(np.linalg.norm(v) - 1) < 1e-5 and embed("x y") == embed("x y")


def test_related_text_scores_higher_than_unrelated():
    base = "Invoice from Acme Industrial Supplies for steel bolts"
    assert sim("bill from acme supplies", base) > sim("holiday photos of the beach", base)
    assert sim("vendor acme", base) > 0.1  # synonym vendor->supplier


def test_chunking_covers_text_with_overlap():
    t = " ".join(f"word{i}" for i in range(1000))
    ch = chunk_text(t, size=500, overlap=50)
    assert len(ch) > 5 and all(len(c) <= 500 for c in ch) and ch[0].startswith("word0") and "word999" in ch[-1]
    assert chunk_text("") == []
