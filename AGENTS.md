# Isleworks workspace

- Canonical project root: `/Users/lhl/Projects/Isleworks`.
- `/Users/lhl/AlongSite` is a separate project. Do not edit it for Isleworks work.
- Follow `docs/自主经济模拟系统_MVP_v0.1.md`; keep implementation progress explicit.
- Use Python 3.11 or newer. Core runtime is standard-library-only.
- Run tests with `PYTHONPATH=src python3.11 -m unittest discover -s tests/isleworks`.
- Fish balances and prices use integer millifish (1000 units = 1 fish).
- Keep economic mutations inside the engine ledger, never in agents or model outputs.
- Never make paid model calls or publish to GitHub without explicit user authority.
- Never put model keys in code, committed configuration, logs, or documentation.
