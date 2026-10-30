# Releasing a reproducible source bundle

A release is a source kit for a fresh installation, not an installer with private
credentials, a model-weight distribution, or a backup of a live deployment.

1. Run the Python and JavaScript tests and regenerate the synthetic screenshots.
2. Review the exact outgoing diff, preview artwork, screenshot manifest and
   author/committer metadata. Check any local production configuration and data
   for accidental copied values without printing secrets into logs.
3. Stage the intended files explicitly, run `python scripts/check_publish.py
   --staged`, and commit. Review `git show --format=fuller --stat HEAD` and
   `python scripts/check_publish.py --ref HEAD`.
4. Build from that exact commit:

   ```sh
   python scripts/build_release.py --version v0.1.0
   cd dist
   sha256sum --check SHA256SUMS
   ```

5. Extract the ZIP into a clean temporary directory. Follow the root README's
   demo instructions using a fresh virtual environment. Test generated server
   and collector config in isolation; keep real credentials out of the bundle.
6. Publish the source commit/tag, curated ZIP, checksum and release notes only
   when publishing is within the user's authorized task. Verify the remote
   commit and downloaded artifact against the reviewed source and local hash.

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
