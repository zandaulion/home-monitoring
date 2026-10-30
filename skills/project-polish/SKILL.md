---
name: project-polish
description: Prepare an existing software project for a polished public repository and reproducible self-hosted installation, including synthetic previews, setup validation, CI, and curated releases. Use for requests to bring a project up to publication quality or apply the same finishing standard to another repository.
---

# Project Polish

Deliver both a convincing public presentation and a verified path from a clean
checkout to a usable installation. Fit the work to the actual project; a CLI
needs examples, not a screenshot gallery, and a hosted app may need different
packaging from a two-machine service.

## Establish the baseline

Inspect the current working tree, project instructions, supported platforms,
existing tests, deployment boundaries and user-requested scope. Preserve unrelated
or concurrent work. When working files are changing elsewhere, assemble and test
an immutable source snapshot before publishing; do not silently sweep in private
or unfinished output. Existing authorization persists, but a request to polish a
project does not itself authorize contacting people, deploying production, or
publishing artifacts outside the requested destination.

If the user asks for consistency with other projects, inspect a few relevant
repositories and their actual setup/release workflows. Reuse useful conventions,
not private configuration or an assumed universal stack. Make justified routine
choices and continue without a new approval gate for every reversible edit.

## Build the appropriate finish

- Make the README answer: what it does, what it looks like, how to try it, how to
  install it, what remains private, and what its practical limits are. Move deep
  operations detail into linked guides without losing it.
- For visual apps, capture the real UI using a dedicated synthetic demo. Generate
  invented data and artwork, isolate storage, disable external side effects, and
  keep demo access behavior outside production authentication. A preview must
  not read production configuration even if its environment points there.
- Verify representative phone, tablet and desktop layouts, loading failures,
  browser errors and horizontal overflow. Exercise a meaningful core workflow,
  not merely page load. Record screenshot dimensions and hashes. Label previews
  as illustrative when they do not demonstrate real inference or integrations.
- Make fresh installation executable: role-specific dependencies, private config
  initialization with random secrets and no overwrite, path-correct service
  templates, local diagnostics, restart and private backup/restore instructions.
  Preserve an existing deployment; test setup in temporary storage.
- Put meaningful existing tests and relevant preview/package checks into CI with
  minimal permissions, pinned actions and no production credentials or accounts.
- Add project-appropriate contribution guidance, private vulnerability reporting,
  license and upstream notices. Inspect existing/vendored terms; do not blindly
  impose a license on third-party code, models or data.
- Package exact reviewed source objects rather than recursively zipping a live
  working directory. Include configuration examples, build revision, instructions,
  license/notices and checksums; exclude runtime data and unnecessary dependencies.

For detailed acceptance checks, read [references/acceptance.md](references/acceptance.md).

## Verify before declaring completion

Test the documented path from a clean extraction in a fresh environment. Verify
that generated config is private, existing config is preserved, model downloads
are verified where relevant, and the demo cleans up its temporary data. Keep the
claims precise: a demo or passing tests do not establish real-device connectivity,
recognition accuracy, notification delivery or production deployment success.

Before every GitHub write, inspect the exact outgoing files/diff and commit
metadata for personal records, names, addresses, device/infrastructure identifiers,
private images, credentials and copied production values. Use synthetic examples.
A secret scanner or ignore file is only a guardrail. Inspect binary images and
archive manifests too; do not "sanitize" a real photograph by merely blurring it.
If privacy is uncertain, stop that publication and ask. Where the user requires
non-identifying commits, explicitly control author and committer metadata and
avoid account-default API commits that cannot preserve it.

Publish only within existing authorization. Verify the remote revision and
uploaded download against the locally reviewed objects and hashes, and observe
relevant CI results. Report the deliverables, evidence, limitations and any work
that remains local. Save improvements to this skill when actual use reveals a
reusable decision or failure mode, rather than accumulating speculative rules.
