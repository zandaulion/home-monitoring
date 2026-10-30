# Security policy

Report vulnerabilities privately through **Security → Report a vulnerability**
when private reporting is available. Otherwise use a contact route on the
maintainer's GitHub profile to arrange a private channel before sharing details.
Do not publish exploit details or private records in an issue.

Include the affected revision, impact, and a minimal synthetic reproduction.
Test installations and accounts you control. Camera snapshots, face embeddings,
portraits, live invitations, device cookies, push subscriptions, credentials,
operational logs and deployment addresses must stay private. Remove these from
any diagnostic material before sharing it.

The loopback application trusts identity from its private proxy boundary.
Public requests must pass through the documented hardened listener; the public
hostname must never reach collector or administrator APIs. The local demo has
synthetic automatic access and must never be exposed as a production service.

This policy does not promise an audit, support period or response deadline.
