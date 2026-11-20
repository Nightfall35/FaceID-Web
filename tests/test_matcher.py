import numpy as np
import pytest

from app.matcher import Gallery, confidence_from_score, l2n

rng = np.random.default_rng(0)


def rand_unit(n=1, d=512):
    return l2n(rng.normal(size=(n, d)))


def noisy(v, sigma):
    """Return a unit vector near v (cosine roughly 1/sqrt(1+sigma^2*d))."""
    return l2n(v + rng.normal(scale=sigma, size=v.shape))


@pytest.fixture
def people():
    return {"alice": rand_unit()[0], "bob": rand_unit()[0], "carol": rand_unit()[0]}


def make_gallery(people, per_person=3, sigma=0.03):
    entries, pid = [], 1
    ids = {}
    for name, base in people.items():
        ids[name] = pid
        for _ in range(per_person):
            entries.append((pid, name, noisy(base, sigma)))
        pid += 1
    return Gallery(entries), ids


def test_l2n_unit_norm():
    v = l2n(rng.normal(size=(5, 512)))
    assert np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)


def test_identifies_each_person(people):
    g, ids = make_gallery(people)
    for name, base in people.items():
        m = g.identify(noisy(base, 0.03)[None], threshold=0.4)[0]
        assert m.person_id == ids[name]
        assert m.name == name
        assert m.score > 0.5
        assert m.confidence > 0.5


def test_unknown_face_rejected(people):
    g, _ = make_gallery(people)
    m = g.identify(rand_unit(), threshold=0.4)[0]
    assert m.person_id is None
    assert m.to_dict()["name"] == "Unknown"
    assert m.to_dict()["known"] is False


def test_threshold_controls_acceptance(people):
    g, ids = make_gallery(people)
    q = noisy(people["alice"], 0.06)[None]  # moderate similarity
    s = g.identify(q, threshold=0.0)[0].score
    assert g.identify(q, threshold=s - 0.01)[0].person_id == ids["alice"]
    assert g.identify(q, threshold=s + 0.01)[0].person_id is None


def test_centroid_combined_with_nn(people):
    g, ids = make_gallery(people, per_person=4)
    comb, nn, cen = g.person_scores(noisy(people["bob"], 0.03)[None])
    j = g.person_ids.index(ids["bob"])
    assert np.isclose(comb[0, j], 0.6 * nn[0, j] + 0.4 * cen[0, j], atol=1e-5)


def test_single_photo_uses_nn_only():
    base = rand_unit()[0]
    g = Gallery([(7, "solo", base)])
    q = noisy(base, 0.04)[None]
    comb, nn, _ = g.person_scores(q)
    assert np.isclose(comb[0, 0], nn[0, 0])


def test_unique_assignment_in_one_image(people):
    """Two faces both resembling Alice: only the better one is labeled Alice."""
    g, ids = make_gallery(people)
    good, worse = noisy(people["alice"], 0.02), noisy(people["alice"], 0.04)
    res = g.identify(np.stack([worse, good]), threshold=0.3, unique=True)
    assert res[1].person_id == ids["alice"]
    assert res[0].person_id != ids["alice"]
    res2 = g.identify(np.stack([worse, good]), threshold=0.3, unique=False)
    assert res2[0].person_id == res2[1].person_id == ids["alice"]


def test_multiple_faces_multiple_people(people):
    g, ids = make_gallery(people)
    q = np.stack([noisy(people["carol"], 0.03), rand_unit()[0], noisy(people["bob"], 0.03)])
    res = g.identify(q, threshold=0.4)
    assert [r.person_id for r in res] == [ids["carol"], None, ids["bob"]]


def test_ambiguity_flag():
    a = rand_unit()[0]
    b = l2n(a + 0.02 * rand_unit()[0])  # nearly identical identities
    g = Gallery([(1, "a", a), (2, "b", b)])
    m = g.identify(a[None], threshold=0.4)[0]
    assert m.person_id == 1
    assert m.ambiguous


def test_empty_gallery():
    g = Gallery([])
    m = g.identify(rand_unit(), threshold=0.4)[0]
    assert m.person_id is None
    assert len(g) == 0


def test_confidence_monotonic():
    assert confidence_from_score(0.4, 0.4) == pytest.approx(0.5)
    assert confidence_from_score(0.6, 0.4) > 0.9
    assert confidence_from_score(0.2, 0.4) < 0.1
