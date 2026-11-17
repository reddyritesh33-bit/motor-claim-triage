# Load test: local_scale3

SLO: complete-run p95 <= 30 s (declared in docs/SLO.md before testing)

| users | completed runs | failed | runs/s | p50 (s) | p95 (s) | max (s) | SLO met |
|---|---|---|---|---|---|---|---|
| 20 | 100 | 0 | 0.81 | 20.0 | 32.0 | 35.2 | NO |
| 40 | 100 | 0 | 0.82 | 48.0 | 60.0 | 66.1 | NO |
| 60 | 308 | 0 | 2.49 | 18.0 | 32.0 | 32.6 | NO |
| 80 | 317 | 0 | 2.48 | 27.0 | 39.0 | 38.8 | NO |
