# Load test: local_scale1

SLO: complete-run p95 <= 30 s (declared in docs/SLO.md before testing)

| users | completed runs | failed | runs/s | p50 (s) | p95 (s) | max (s) | SLO met |
|---|---|---|---|---|---|---|---|
| 20 | 107 | 0 | 0.82 | 19.0 | 26.0 | 30.5 | yes |
| 40 | 112 | 0 | 0.90 | 39.0 | 49.0 | 51.6 | NO |
| 60 | 105 | 0 | 0.83 | 50.0 | 74.0 | 77.9 | NO |
| 80 | 114 | 0 | 0.96 | 43.0 | 87.0 | 95.1 | NO |
