"""Tests for the pilot-search on-disk job queue (pilot_jobs).

enqueue overwrites the per-instance pending job (newest wins); claim_next atomically moves it to
processing/; complete deletes it; requeue_orphans resumes a crash mid-search. Queue dirs are
redirected to a tmp dir so nothing touches the real cache.
"""
from __future__ import annotations

import json

import pytest

from scripts.managers.factories.daemons import pilot_jobs


@pytest.fixture()
def queue(tmp_path, monkeypatch):
    q = tmp_path / "queue"
    p = tmp_path / "processing"
    monkeypatch.setattr(pilot_jobs, "PILOT_QUEUE_DIR", q)
    monkeypatch.setattr(pilot_jobs, "PILOT_PROCESSING_DIR", p)
    return q, p


def _job(instance="sonarr", n=3):
    return {
        "mode": "interactive", "instance": instance,
        "items": [[i, 900 + i] for i in range(n)],
        "ladder": [[11, 480], [12, 1080]], "meta": {}, "current_indexers": [1],
        "floor_res": 0,
    }


def test_enqueue_then_claim_roundtrips(queue):
    q, p = queue
    path = pilot_jobs.enqueue("sonarr", _job(n=2))
    assert path.exists() and path.name == "sonarr__interactive.json"

    claimed = pilot_jobs.claim_next()
    assert claimed is not None
    proc_path, job = claimed
    assert job["instance"] == "sonarr"
    assert job["items"] == [[0, 900], [1, 901]]
    assert proc_path.parent == p          # moved into processing/
    assert not path.exists()              # removed from the queue (can't be re-claimed)
    assert pilot_jobs.claim_next() is None   # queue now empty


def test_enqueue_overwrites_pending_job_for_same_instance(queue):
    q, _ = queue
    pilot_jobs.enqueue("sonarr", _job(n=2))
    pilot_jobs.enqueue("sonarr", _job(n=5))   # newest wins
    assert len(list(q.glob("*.json"))) == 1
    _proc, job = pilot_jobs.claim_next()
    assert len(job["items"]) == 5


def test_complete_removes_processing_file(queue):
    pilot_jobs.enqueue("sonarr", _job())
    proc_path, _ = pilot_jobs.claim_next()
    assert proc_path.exists()
    pilot_jobs.complete(proc_path)
    assert not proc_path.exists()


def test_requeue_orphans_resumes_interrupted_job(queue):
    q, p = queue
    pilot_jobs.enqueue("sonarr", _job(n=4))
    proc_path, _ = pilot_jobs.claim_next()   # claimed but "crashed" (left in processing/)
    assert proc_path.exists() and not (q / "sonarr__interactive.json").exists()

    moved = pilot_jobs.requeue_orphans()
    assert moved == 1
    assert (q / "sonarr__interactive.json").exists()      # back in the queue → resumes
    assert not proc_path.exists()
    _proc2, job = pilot_jobs.claim_next()
    assert len(job["items"]) == 4


def test_requeue_orphans_drops_orphan_when_newer_pending_exists(queue):
    q, p = queue
    # An orphan (older batch) AND a fresh pending batch for the same instance: the pending one
    # is a superset of due stubs, so the orphan is dropped rather than clobbering it.
    pilot_jobs.enqueue("sonarr", _job(n=2))
    proc_path, _ = pilot_jobs.claim_next()   # orphan in processing/
    pilot_jobs.enqueue("sonarr", _job(n=9))  # newer pending batch
    moved = pilot_jobs.requeue_orphans()
    assert moved == 0
    assert not proc_path.exists()            # orphan dropped
    _proc, job = pilot_jobs.claim_next()
    assert len(job["items"]) == 9            # the newer batch survives


def test_pending_count_tracks_queue_and_processing(queue):
    assert pilot_jobs.pending_count() == 0
    pilot_jobs.enqueue("a", _job("a"))
    pilot_jobs.enqueue("b", _job("b"))
    assert pilot_jobs.pending_count() == 2   # both queued
    pilot_jobs.claim_next()                  # one moves to processing/ — still counted
    assert pilot_jobs.pending_count() == 2


def test_corrupt_queue_file_is_skipped(queue):
    q, _ = queue
    q.mkdir(parents=True, exist_ok=True)
    (q / "sonarr.json").write_text("{ not json")
    assert pilot_jobs.claim_next() is None   # corrupt claim dropped, not raised
