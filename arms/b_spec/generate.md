# Arm B: generation script

Run from the repo root:

```bash
bash arms/b_spec/generate.sh N
```

The script runs every step identically:

1. Installs Spec Kit at the pinned tag (`v1.1.0`)
2. Builds the kit with `make_kit.py N` and runs `specify init`
3. Runs the five SDD skills (`constitution`, `specify`, `plan`, `tasks`, `implement`) via `claude --print --output-format json`, saving each step's full output to `logs/step-<name>.json` in the kit
4. The implement step also allows `uv run pytest` so Spec Kit can run the kit's tests as part of implementation
5. Writes `metadata.json` in the kit root with: Claude Code version, model, Spec Kit version, start and end times, and tokens and cost per step

Import the result into the repo:

```bash
python arms/b_spec/import_gen.py N
```
