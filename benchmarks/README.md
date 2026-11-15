# Benchmarks

## Mutants (fictional, included)

```
python benchmarks/mutants.py --json results.json
```

Applies 23 mechanical mutation operators to the demo's 12 gold queries, keeps the silently wrong mutants and shrinks each. It is assistant-authored synthetic data, useful as a regression check for the shrinker, not as evidence about real errors.

## BIRD dev with real model errors (data not included)

BIRD is a public text-to-SQL benchmark (CC BY-SA 4.0). The data is not committed here.

1. Download `https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip` (346 MB). Unzip it, then unzip `dev_20240627/dev_databases.zip` into the same folder. You should end up with `dev_20240627/dev.json` and `dev_databases/<db>/<db>.sqlite` (about 1.5 GB).
2. Download the official ChatGPT baseline predictions from the BIRD repository: `https://raw.githubusercontent.com/AlibabaResearch/DAMO-ConvAI/main/bird/llm/exp_result/turbo_output/predict_dev.json` (about 300 KB, 1,534 entries; 54 are `0`, meaning no SQL was given).
3. Run:
   ```
   python benchmarks/bird.py --bird-dir BIRD_FOLDER --pred predict_dev.json --sample 100 --seed 777 --workers 6 --out results.json --details details.json
   ```

Step 1 scores all 1,534 predictions with goldcheck's comparison and reports agreement with BIRD's own set comparison. Step 2 shrinks a seeded random sample of the silently wrong ones, one Python process per question with a hard wall-clock limit, and runs clause blame on each. `--details` writes questions, SQL and counterexample rows for manual review. Keep that file local, because it contains BIRD data.

Committed results:
- `bird-turbo-heldout-2026-11-16.json`: seed 777, never used while developing the shrinker.
