# RWX Buildathon · Blast Radius

This entry selects SQLGlot tests from whole-patch impact evidence and schedules them under the scorer's cost model. It records **135.653 simulated seconds with 0 misses** on the 30 public regressions. On the 14 independently frozen mixed patches, the three-run local median including CPU is **170.711 seconds**, down from 303.137 seconds in a clean-build comparison.

## Test selection

`impact.py` verifies unified diffs against the pinned source, compares the old and new Python ASTs, and looks up baseline execution for changed function bodies. The index combines the original pytest contexts with coverage attributed to spawned process-pool jobs. It also records fixture readers, raw Python source readers, declaration use sites and related dialects. Serializer and deserializer references in `__reduce__` connect coverage across process boundaries. Unfamiliar patch formats, paths and changes to imports or declaration shape use the full suite.

Dictionary key changes and set membership changes in declarations take the full-suite fallback because dependent classes may require an inherited entry while importing. The source audit distinguishes file opens made by the Python loader from direct source reads. It reclassifies an older, untagged read only when both full-manifest and isolated observations identify the same test and path as an import. Confirmed direct readers are always kept.

The configuration-read index is enabled for `TSQL.WEEK_OFFSET`. It records the owning class and test for each measured read, including reads in spawned pool tasks. An isolated integer value edit can use those readers plus raw source readers and tests whose coverage is missing. Unattributed or unresolved reads prevent the builder from enabling the affected target.

For a value change to an existing numeric escape key, the index tracks actual scanner lookups through inheritance and constructor calls. The selector retains raw source readers, code observers, and tests with missing coverage.

For a local expression edit inside a stable statement, the selector looks for tests that enter the entire statement or condition. This includes the beginning of a short-circuit condition even when its modified operand was skipped on the baseline. It keeps cheap function-level candidates and uses whole-function coverage where execution was unattributed, serialization is involved, or the finer evidence is missing.

The optional predicate index measures whether native equality and identity agree on operands seen by the pinned test suite. It only uses sites where the probe recorded agreement and corroborated subprocess execution where applicable. The shortcut requires the entire patch to preserve its operands and change only the comparison operators in one file. Raw source readers and tests with missing coverage still run.

For a single ordered comparison edit, the arithmetic probe records tests that can observe a change at the equality boundary. Its floating-point extension applies to exact built-in numbers at sites where the integer and numeric probes agree on execution contexts and the extra evidence can remove a long test.

`impact_index.json.gz` is generated from the unpatched SQLGlot checkout and test manifest. The runtime maps evidence to test numbers using pytest node IDs. The image also contains the pinned SQLGlot source, tests and fixtures. It uses isolated, bounded subprocesses for selected cold-import comparisons or proof-backed replay when the scheduled savings justify the cost. Public answers are used by the separate local scorer.

## Scheduling

`scheduler.py` reads the recorded durations in integer milliseconds. For each eligible worker count, the longest test and the rounded-up average load give a lower bound on the busiest worker's load. A longest-first assignment gives a feasible upper bound. When those bounds leave room to improve the score, the scheduler explores one representative of each equivalent worker state. The entry point caps that search per distinct test set and keeps the best feasible assignment it has found.

The selector then fills spare capacity with tests that exercised the edited files. These tests occupy existing workers only up to the current busiest load, preserving the simulated cost. `--explain` reports the number added as `filled_spare_capacity`.

If the cached full-suite schedule costs no more than the selected schedule, the selector uses the full suite.

The full-suite comparison uses four workers and reaches the lower bound of 36.817 seconds per patch. Run it with `--all-tests`.

## The problem

Your image gets a batch of code changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change, print which tests to run and how to split them across up to 8 workers.

| | |
| --- | --- |
| `/data/commits/<id>.patch` | One unified diff against the pinned commit. Each stands alone: none builds on another. |
| `/data/tests.json` | Every test, numbered, with its file and the seconds it took us to run. |
| stdout | One JSON line a commit: `{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}` |

Each inner list is one worker's test numbers. `"workers": []` runs nothing, which is the right answer for a change that breaks nothing.

## Scoring

Lower is better. [SCORING.md](SCORING.md) has the exact rules; in short:

- A commit costs the recorded seconds of its busiest worker, plus 2 seconds for every worker.
- A commit where you left out a test that fails adds 300 seconds.
- The CPU seconds your program uses are added. Running sqlglot's tests while you decide is allowed, and you pay for the time.
- Your image runs 3 times and the middle total counts.

"Fails" means pytest reports a failure, an error or a timeout for the test, or the test's file cannot be imported. A test with subtests is one test: one failing subtest fails it.

## Try it

You need Docker, make and Python 3.9 or newer.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
```

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

To run the selector directly with Python in PowerShell 7:

```powershell
python selector.py --data-dir sample --explain > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

This uses the same sample scorer. `make sample` also checks the Linux container under the scorer's resource limits.

`--explain` reports the selection rules and candidate count on stderr. The index rebuild steps below use the local `analysis/` workspace. First collect baseline coverage using the Reproduction section of `analysis/analysis_report.md`. Repeat that coverage command with `--process-contexts`, a separate scratch directory, and `--output /analysis/.work/coverage-with-workers.json.gz`. Use a fresh scratch directory for the comparison probe so results from previous runs cannot enter the index.

```powershell
docker run --rm --platform linux/amd64 --network none --cpus 4 --memory 8g `
    --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/analysis/predicate_probe:/src `
    --env BLAST_RADIUS_PREDICATE_SOURCE=/src `
    --env BLAST_RADIUS_PREDICATE_RAW=/analysis/.work/predicate-verified-raw `
    --mount "type=bind,source=$analysisRoot,target=/analysis" `
    --mount "type=bind,source=$sourceRoot,target=/src,readonly" `
    --mount "type=bind,source=$sampleRoot,target=/sample,readonly" `
    blast-radius-analysis-coverage python /analysis/collect_predicates.py `
    --catalog /sample/tests.json --output /analysis/.work/predicate-verified.json
```

Merge the baseline runs from the repository root. Leaving out `--predicate-observations` builds the coverage-only comparison index.

```powershell
python analysis/build_impact_index.py --source analysis/.work/sqlglot `
    --coverage analysis/.work/coverage-with-workers.json.gz `
    --parent-coverage analysis/coverage.json.gz `
    --predicate-observations analysis/.work/predicate-verified.json --output impact_index.json.gz
```

For the source provenance correction, run the coverage collector with `--source-audit` and write to `analysis/.work/manifest-source-audit.json.gz`. Run it in a fresh container with `--source-audit --test-number 27` to produce `analysis/.work/cold-source-audit.json.gz`. Use separate scratch directories and add `--source-audit analysis/.work/manifest-source-audit.json.gz --cold-source-audit analysis/.work/cold-source-audit.json.gz` to the index builder command.

To collect the owner-specific configuration reads, use a fresh scratch directory.

```powershell
docker run --rm --platform linux/amd64 --network none --cpus 4 --memory 8g `
    --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/analysis/predicate_probe:/src `
    --env BLAST_RADIUS_CONFIG_SOURCE=/src --env BLAST_RADIUS_CONFIG_FIELDS=WEEK_OFFSET `
    --env BLAST_RADIUS_CONFIG_RAW=/analysis/.work/week-offset-raw `
    --mount "type=bind,source=$analysisRoot,target=/analysis" `
    --mount "type=bind,source=$sourceRoot,target=/src,readonly" `
    --mount "type=bind,source=$sampleRoot,target=/sample,readonly" `
    blast-radius-analysis-coverage python /analysis/collect_config_reads.py `
    --catalog /sample/tests.json --output /analysis/.work/week-offset-observations.json
```

Pass `--configuration-reads analysis/.work/week-offset-observations.json --configuration-target sqlglot.dialects.tsql:TSQL:WEEK_OFFSET` to the index builder. The independent mutations are `tsql-week-offset-zero` and `oracle-week-offset-positive` in `analysis/validate_impact.py`.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

## Submit

Push to a public GitHub repository with the `Dockerfile` at its top, then enter the repository and the commit hash on the track page. The final scores the commit you pick. If you pick none, your best practice submission counts.

## Rules

- Submit your own team's repository. A submission with the same repository, commit or archive as another team's is disqualified, the later one.
- Your build may download only from public package registries: PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot’s own repository at the pinned commit. Nothing from an address your team controls. The final rebuilds every image after the cutoff, so anything else could change a submission after the deadline.
- No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
- Only the final counts. The practice board carries no prize.

## How your image runs

- The build has the network, 4 cores, 8 GB of memory and 10 minutes. It may clone sqlglot at the pinned commit, install it and run its tests to make coverage or import maps.
- Your repository may hold 20 MB. Git submodules and Git LFS files are not fetched: clone sqlglot in the `Dockerfile` and commit maps as plain files.
- The run has no network, 2 cores, 4 GB of memory and 3 minutes for the whole batch (4.5 in the final, which has about one and a half times as many commits).
- The disk is read-only. `/tmp` holds 64 MB and is empty at the start of every run.
- The container runs as user `nobody`, with `HOME=/tmp` and at most 256 processes.
- stdout may hold 2 MB. More rejects the run.

## Things that will catch you

- Logs go to stderr. Any line on stdout that is not an answer rejects the run.
- `score.py` names a broken rule by its code, as in `Rejected: not_json on line 1`. The table in SCORING.md says what each code means.
- Files your program needs must be readable by `nobody`. A map written by `root` during the build with mode 600 is not.
- pytest 9 prints a test as `PASSED` beside `SUBFAILED` lines when one of its subtests fails. That test failed.
- sqlglot skips its executor tests unless `SKIP_INTEGRATION=0` is set. We labeled with it set, so those tests can fail.
- `requirements.lock` is every package in the environment we labeled in, on the image the `Dockerfile` starts from. To run sqlglot's tests the way we did, `pip install -r requirements.lock` and put the sqlglot checkout on `PYTHONPATH`; sqlglot's own `pip install -e .[dev]` picks newer versions.
- The scorer is linux/amd64. On an Apple Silicon Mac, build and run with `--platform linux/amd64`, as the Makefile does, or your timings and wheels will differ.
- A change that only adds a comment is not promised to be harmless.
