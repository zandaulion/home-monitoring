# Acceptance checks

Use the parts relevant to the project. These are observable outcomes, not a
requirement to add every feature or document to every repository.

| Area | Useful evidence |
| --- | --- |
| Public presentation | README explains purpose and supported setup; local links resolve; synthetic previews render the actual UI and carry a generation manifest. |
| Safe preview | A deliberately invalid production-config environment variable does not affect the demo; its data directory is temporary and removed; external requests and production integrations are blocked. |
| Interaction | At least one core flow works in a real browser at representative widths; no page errors, failed core requests or horizontal overflow. |
| Setup | Fresh extraction installs with documented dependencies; role-specific config generation produces private files and refuses to overwrite existing configuration. |
| Diagnostics | Placeholder, invalid-permission and missing-asset cases return actionable errors without printing secrets. Connectivity and identity checks remain separate from static config checks. |
| Data recovery | Backup scope covers related database/media/key material, states how consistency is obtained, and explains what restoring does and does not preserve. |
| CI | Tests run without live accounts or model downloads unless explicitly part of the test; permissions and execution time are bounded. |
| Dependency maintenance | The default branch contains valid updater configuration for actual dependency manifests and CI actions; referenced constraints/lockfiles are covered. Check repository alert/security-update settings separately. Updates open reviewable PRs under the existing merge policy. |
| Repository metadata | The public description explains the product; a focused topic set matches its capabilities and stack. No private hostnames, accounts or infrastructure identifiers appear. |
| Distribution | Archive contains only the reviewed revision; a private untracked decoy cannot enter it; same inputs generate the same package; checksum verifies after extraction/download. |
| Publication | Exact outgoing metadata and content have been reviewed; remote commit/tree and release hashes match; no hidden working-directory additions were swept in. |

Typical implementation choices: a loopback-only temporary demo, a configuration
initializer and `doctor` command, source packaging from Git blobs, a ZIP hash file,
and a small screenshot command that launches its own demo and browser. Prefer the
project's existing equivalents when available. Do not add an installer, container
stack, screenshot pipeline or public demo host unless it serves the user's scope.

A repository guard can reject runtime paths, credentials matching known patterns,
unexpected binary formats and screenshot-manifest mismatches. It must say what
it checks and must not claim to prove the absence of all sensitive information.
Keep private-value comparisons local and avoid printing the values being matched.
