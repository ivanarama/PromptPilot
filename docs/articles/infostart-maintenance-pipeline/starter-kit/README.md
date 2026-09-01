# Generic GitHub maintenance pipeline starter kit

This directory is intentionally project-neutral. Copy it into a new repository,
then replace the values in `.pipeline/maintenance.example.json` and rename that
file to `.pipeline/maintenance.json`.

The kit implements four independent scheduled stages:

```text
TRIAGE → FIX → REVIEW → human ship decision → MERGE
```

## Files

- `.pipeline/maintenance.example.json` — repository identity, trusted account,
  base branch, project-owned checks and per-run limits.
- `.claude/skills/*/SKILL.md` — canonical stage contracts.
- `.agents/skills/*/SKILL.md` — thin Codex adapters.
- `prompts/stage-prompt.md` — prompt pasted into the PromptPilot task form.
- `pipeline_profiles.example.json` — token-free dashboard profile.
- `scripts/create-labels.ps1` — creates the route labels through GitHub CLI.
- `tools/pipeline_health.py` — optional read-only invariant checker.

## Mandatory customization

1. Replace `OWNER/REPOSITORY`, `OWNER` and `PROJECT_KEY`.
2. Put deterministic build/test/lint commands into
   `.pipeline/maintenance.json`. Commands are trusted repository code; issue
   and PR text must never be turned into shell commands.
3. Review every skill against the repository's branch protection and review
   policy.
4. Commit the contracts to the default branch before creating recurring tasks.
5. Create the route labels and four isolated automation clones.
6. Create all four PromptPilot series paused, test them one by one, then resume.

The kit deliberately never grants an agent authority to set `approved`,
`decision:*` or `ship`. Those labels are human gates.
