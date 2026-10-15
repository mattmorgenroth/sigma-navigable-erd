#!/usr/bin/env python3
"""Run the full tool against the fictional Acme Retail workbook, with no network and no credentials.

  python3 examples/offline_demo.py            # writes examples/sample-output/
  python3 examples/offline_demo.py /some/dir  # writes somewhere else

It exercises the same code path as `run.py trace`: resolve the workbook from its URL, trace every column through
the data models to the warehouse tables, run the recommendation engine, render the page and save the results.
"""
import glob
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "skills", "sigma-navigable-erd", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))
sys.path.insert(0, HERE)

import blame  # noqa: E402
import fake_sigma  # noqa: E402
import run  # noqa: E402


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "sample-output")
    fake_sigma.install(blame)
    work = tempfile.mkdtemp(prefix="navigable-erd-demo-")
    sys.argv = ["run.py", "trace", fake_sigma.WB_URL, "--out", work,
                "--env-file", os.path.join(work, "no-such-file.env")]
    run.main()
    run_dir = sorted(glob.glob(os.path.join(work, "*", "*", "2*")))[-1]
    os.makedirs(dest, exist_ok=True)
    for name in ("index.html", "standalone.html", "recommendations.md", "blame.json"):
        shutil.copy(os.path.join(run_dir, name), os.path.join(dest, name))
    shutil.rmtree(work, ignore_errors=True)
    print("Sample written to", dest)


if __name__ == "__main__":
    main()
