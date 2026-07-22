"""持久化 JobStore 测试。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.job_store import JobStore, recover_stale_jobs


def test_job_persistence():
    with tempfile.TemporaryDirectory() as d:
        store = JobStore(jobs_dir=os.path.join(d, "jobs"))
        job = store.create(
            video_name="v.mp4",
            face_name="f.jpg",
            duration=60,
            manifest={"video_path": "/tmp/v.mp4"},
        )
        store.update(job.id, status="running", message="测试中", segments_total=10, segments_done=3)
        store.append_log(job.id, "line1")

        store2 = JobStore(jobs_dir=os.path.join(d, "jobs"))
        loaded = store2.get(job.id)
        assert loaded is not None
        assert loaded.status == "running"
        assert loaded.segments_total == 10
        assert loaded.segments_done == 3
        assert "line1" in store2.read_log_tail(job.id)
        print("persistence OK")


def test_recover_stale():
    with tempfile.TemporaryDirectory() as d:
        store = JobStore(jobs_dir=os.path.join(d, "jobs"))
        job = store.create(
            video_name="v.mp4", face_name="f.jpg", duration=30, manifest={}
        )
        store.update(job.id, status="running", worker_pid=999999999)
        n = recover_stale_jobs(store)
        assert n == 1
        assert store.get(job.id).status == "interrupted"
        print("recover stale OK")


if __name__ == "__main__":
    test_job_persistence()
    test_recover_stale()
    print("\nALL JOB STORE TESTS PASSED")
