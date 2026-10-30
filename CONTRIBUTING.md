# Contributing

Use Python 3.12 and install `requirements-dev.txt` in a virtual environment.
The shared `constraints.txt` pins the resolved Python 3.12 dependency set;
role-specific requirement files install only the packages that role needs. When
updating dependencies, resolve in a fresh environment and rerun tests and browser
checks before refreshing constraints.

Run `python -m pytest -q`, `node tests/push-worker.mjs`, and
`node tests/activity-timeline.mjs`. Tests use synthetic images and temporary data;
they do not require camera access or model downloads. FastAPI tests need local
socket access. Use `python scripts/screenshots.py` for browser smoke checks at
phone, tablet and desktop sizes; its demo storage is isolated from production.
Run `python tests/timeline-browser.py` for the timeline selection, pagination,
filters, modal, empty-day and narrow touch-layout regression checks.

Keep the collector light: it depends only on Pillow and the standard library.
Do not couple it to server inference packages. Preserve loopback-only server
binding, proxy identity boundaries, network-only private API responses, generic
push payloads, and bounded event storage. Keep demo authentication inside the
separate demo wrapper; never add a production authentication bypass for screenshots.

Before publishing, inspect the exact staged diff and author/committer metadata.
Run `python scripts/check_publish.py --staged`; after committing, use
`python scripts/check_publish.py --ref HEAD`. The checker catches known credential
patterns and prohibited artifact paths; it cannot prove that arbitrary content
contains no private data. Review text, images, archives and metadata manually.
Use neutral maintainer metadata when publishing this repository from a private
working environment, and never include production config, captures or logs.

Screenshots must come from `scripts/screenshots.py`, with its hash manifest.
Do not replace them with real camera views, even blurred ones. To change artwork,
edit `scripts/demo.py` and regenerate. Document the behavior you changed and add
focused tests where authentication, storage, migration, packaging or demo
isolation could regress. Do not add secrets to bug reports or CI fixtures.

See [release instructions](docs/RELEASING.md) for curated source archives and
[SECURITY.md](SECURITY.md) for vulnerability reporting.
