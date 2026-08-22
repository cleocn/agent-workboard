# AWB-018 release evidence contract

Agent Workboard 0.3.1b2 Preview is a recovery-only successor of the exact
v0.3.1b1 public commit. The release adds only the HUMAN-bound orphan Reviewer
task recovery and an exact backup-first 0.3.1b1→0.3.1b2 no-DDL upgrade path.

The release retains independent Reviewer and strict 3+1+1 review. AUTO_ON_PASS
only automates a gate after PASS with zero open Findings and does not authorize
remote, destructive, publish, deploy, delete, upgrade, or recovery actions.

The pending Codex usage parser change is excluded. AWB-010 is not started;
`estimatedCredits` remain local analytical units rather than an official
ChatGPT/Codex weekly-quota bill. There is no PyPI publication, runner, daemon,
remote control plane, or consumer migration in this Preview.
