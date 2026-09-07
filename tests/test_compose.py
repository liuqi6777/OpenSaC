import pytest

from opensac import SearchHit, dedup, fuse


def hit(url, title=""):
    return SearchHit(url=f"https://example.com/{url}", title=title, snippet="text")


def test_dedup_preserves_first_object_and_exact_urls():
    first = hit("a", "first")
    second = hit("b")
    assert dedup(iter([first, hit("a", "later"), second])) == [first, second]
    assert dedup([first, hit("a")])[0] is first
    assert len(dedup([hit("a?x=1"), hit("a?x=2")])) == 2
    assert dedup([]) == []


def test_custom_identity_and_generator_inputs():
    a, b = {"key": 1, "text": "first"}, {"key": 2, "text": "second"}
    assert dedup([a, b, {"key": 1}], key=lambda item: item["key"]) == [a, b]
    merged = fuse([iter([a, b]), iter([b])], key=lambda item: item["key"])
    assert merged == [b, a]
    assert merged[0] is b
    with pytest.raises(ValueError):
        dedup([a])


def test_fusion_rewards_agreement_without_inflating_duplicate_votes():
    a, b, c = hit("a"), hit("b"), hit("c")
    # Input list position is authoritative; original objects are preserved.
    assert fuse([[a, b], [c, b]]) == [b, a, c]
    assert fuse([[a, a, a, b], [c, b]]) == fuse([[a, b], [c, b]])
    assert fuse([[a, b], [c, b]])[0] is b


def test_weighting_zero_contributions_and_stable_ties():
    a, b = hit("a"), hit("b")
    assert fuse([[a], [b]], weights=[1, 2]) == [b, a]
    assert fuse([[a], [b]], weights=[1, 1]) == [a, b]
    assert fuse([[a], [b]], weights=[0, 1]) == [b]
    assert fuse([[a], [b]], weights=[1e308, 1e308]) == [a, b]
    assert fuse([]) == []
    assert fuse([[], []]) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"weights": []},
        {"weights": [1, 2]},
        {"weights": [0]},
        {"weights": [-1]},
        {"weights": [float("nan")]},
        {"weights": [float("inf")]},
        {"weights": [True]},
        {"k": 0},
        {"k": -1},
        {"k": float("nan")},
        {"k": float("inf")},
    ],
)
def test_invalid_fusion_parameters_fail_explicitly(kwargs):
    with pytest.raises(ValueError):
        fuse([[hit("a")]], **kwargs)
