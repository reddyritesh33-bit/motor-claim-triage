"""Summarise Locust CSVs (one per user step) into a Markdown table against the SLO."""
import csv
import glob
import os
import re
import sys

SLO_P95_S = 30.0
out_dir = sys.argv[1]
rows = []
for path in glob.glob(os.path.join(out_dir, "u*_stats.csv")):
    users = int(re.search(r"u(\d+)_stats", path).group(1))
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["Name"] == "run_complete":
                rows.append({
                    "users": users,
                    "runs": int(r["Request Count"]),
                    "failures": int(r["Failure Count"]),
                    "rps": float(r["Requests/s"]),
                    "p50_s": float(r["50%"]) / 1000,
                    "p95_s": float(r["95%"]) / 1000,
                    "max_s": float(r["Max Response Time"]) / 1000,
                })
rows.sort(key=lambda x: x["users"])
print(f"# Load test: {os.path.basename(out_dir.rstrip('/'))}\n")
print(f"SLO: complete-run p95 <= {SLO_P95_S:.0f} s (declared in docs/SLO.md before testing)\n")
print("| users | completed runs | failed | runs/s | p50 (s) | p95 (s) | max (s) | SLO met |")
print("|---|---|---|---|---|---|---|---|")
for r in rows:
    print(f"| {r['users']} | {r['runs']} | {r['failures']} | {r['rps']:.2f} | {r['p50_s']:.1f} | {r['p95_s']:.1f} | "
          f"{r['max_s']:.1f} | {'yes' if r['p95_s'] <= SLO_P95_S and r['failures'] == 0 else 'NO'} |")
