# AWB-019 release evidence contract

Agent Workboard 0.3.1b3 is a local-first Preview parser hotfix. It preserves the
single-record selective parser v2 and accepts multi-`session_meta` input only as
a unique, linear requested-HEAD-to-ancestor chain under selective parser v3.
Unknown, broken, reordered, duplicate, cyclic, ambiguous, token-before-tail, or
metadata-after-token shapes fail closed. Prompt, response, tool output, source
content, credentials, session files, and local paths are not release evidence.

The only supported forward pair is the exact public 0.3.1b2 identity to the
exact 0.3.1b3 identity. Upgrade is preflight-first, backup-first, performs no
schema DDL, validates all three installed extensions, and emits one structured
next step. Rollback remains bound to the post-upgrade database hash and refuses
after workflow or usage writes.

This Preview preserves independent review and strict automatic-gate conditions;
automatic review is never authority for publication or another risky action.
Usage validation for this release is pre-cohort only and does not start AWB-010.
This release does not publish to PyPI, migrate consumers, add a runner or daemon,
provide remote control, or claim stable/GA readiness.
