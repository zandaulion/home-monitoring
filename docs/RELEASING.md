# Releasing a reproducible source bundle

A release is a source kit for a fresh installation, not an installer with private
credentials, a model-weight distribution, or a backup of a live deployment.

Pushing a version tag from `main` starts `.github/workflows/release.yml`. It runs
every check, builds the bundle, verifies its hash, installs it into a clean
environment with `scripts/verify_release.py`, and creates a **draft** release. The
automation cannot judge privacy or real-world behaviour, so a person finishes it.

1. Run the checks, regenerate the synthetic screenshots, and commit to `main`.
2. Review the exact outgoing diff, preview artwork, screenshot manifest and
   author/committer metadata. Check any local production configuration and data
   for accidental copied values without printing secrets into logs. Review
   `git show --format=fuller --stat HEAD` and `python scripts/check_publish.py
   --ref HEAD`.
3. Tag and push the reviewed commit:

   ```sh
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. When the Release workflow finishes, open the draft on GitHub, write its notes,
   and compare the attached checksum with a local build of the same commit:

   ```sh
   python scripts/build_release.py --version v0.2.0
   cat dist/SHA256SUMS
   ```

5. Publish the draft only when publishing is within the user's authorized task.
   Published releases are public immediately.

The builder reads committed Git blobs, never the working directory. Private and
runtime directories are rejected by the guard. A bundle includes `BUILD-INFO.json`
with its source revision, all application code and templates, and the generated
showcase. ZIP timestamps and permissions are normalized; identical committed
source and version produce identical bytes. Uncommitted edits are not included.

## Installing a download

Download `home-monitoring-v0.1.0.zip` and `SHA256SUMS` from the same release, run
`sha256sum --check SHA256SUMS` beside them, and extract. Follow `README.md` for the
isolated preview or `deploy/README.md` for the two-machine installation. Models
are downloaded and verified separately. Use your own private accounts, secrets,
camera inventory, HTTPS routes and backup destination.

For an existing installation, make a consistent private backup first and follow
the upgrade instructions. The demo intentionally uses fake recognition and
illustrative scenes; a successful preview does not validate your camera's
snapshot endpoint, networking, real-model accuracy, or phone push delivery.
