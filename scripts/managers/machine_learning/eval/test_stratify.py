"""Hand-verified unit tests for eval/stratify.py."""
from scripts.managers.machine_learning.eval import stratify as st


def test_popularity_counts():
    events = [{"item": "a"}, {"item": "a"}, {"item": "b"}]
    c = st.popularity_counts(events)
    assert c["a"] == 2 and c["b"] == 1


def test_popularity_segments_mass_based():
    # counts a=6,b=3,c=1 (total 10); head_cut=2, tail_cut=8
    #   a: cum 0  <2  → head ; cum→6
    #   b: cum 6  (≥2, <8) → torso ; cum→9
    #   c: cum 9  (≥8) → tail
    seg = st.popularity_segments({"a": 6, "b": 3, "c": 1}, head_frac=0.2, tail_frac=0.2)
    assert seg == {"a": "head", "b": "torso", "c": "tail"}


def test_popularity_segments_empty():
    assert st.popularity_segments({}) == {}
    assert st.popularity_segments({"x": 0}) == {}   # total 0 → empty


def test_segment_of_default():
    seg = {"a": "head"}
    assert st.segment_of("a", seg) == "head"
    assert st.segment_of("unseen", seg) == "tail"          # default cold → tail
    assert st.segment_of("unseen", seg, default="torso") == "torso"


def test_relevant_by_segment():
    seg = {"a": "head", "b": "torso", "c": "tail"}
    parts = st.relevant_by_segment({"a", "b", "c", "z"}, seg)  # z unseen → tail
    assert parts["head"] == {"a"}
    assert parts["torso"] == {"b"}
    assert parts["tail"] == {"c", "z"}
