import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from tests.testlib import check_output, gen_job, run

alloc_exec = "nomad alloc exec -i=false -t=false -job"


@dataclass
class NomadTempdir:
    jobid: str

    def __enter__(self):
        return check_output(
            f"{alloc_exec} {self.jobid} sh -xeuc 'echo $NOMAD_TASK_DIR'"
        ).strip()

    def __exit__(self, type, value, traceback):
        return run(f"{alloc_exec} {self.jobid} sh -xeuc 'rm -rv $NOMAD_TASK_DIR'")


def run_temp_job():
    jobjson = gen_job(script="exec sleep 60")
    job = jobjson["Job"]
    jobname = job["ID"]
    try:
        run("nomad-watch --json start -", input=json.dumps(jobjson))
        with NomadTempdir(jobname) as nomaddir:
            with tempfile.TemporaryDirectory() as hostdir:
                yield jobname, nomaddir, hostdir
    finally:
        run(f"nomad job stop --purge {jobname}", check=False)


def test_nomad_cp_dir():
    for jobname, nomaddir, hostdir in run_temp_job():
        run(
            f"{alloc_exec} {jobname} sh -xeuc 'cd {nomaddir} && mkdir -p dir && touch dir/1 dir/2'"
        )
        run(f"nomad-cp -vv -job {jobname}:{nomaddir}/dir {hostdir}/dir")
        run(f"nomad-cp -vv -job {hostdir}/dir {jobname}:{nomaddir}/dir2")
        run(f"nomad-cp -vv -job {jobname}:{nomaddir}/dir2 {hostdir}/dir2")
        run(f"diff -r {hostdir}/dir {hostdir}/dir2")


def test_nomad_cp_file():
    for jobname, nomaddir, hostdir in run_temp_job():
        txt = f"{time.time()}"
        with Path(f"{hostdir}/file").open("w") as f:
            f.write(txt)
        run(f"nomad-cp -vv -job {hostdir}/file {jobname}:{nomaddir}/file")
        run(f"nomad-cp -vv -job {jobname}:{nomaddir}/file {hostdir}/file2")
        with Path(f"{hostdir}/file2").open() as f:
            assert f.read() == txt
