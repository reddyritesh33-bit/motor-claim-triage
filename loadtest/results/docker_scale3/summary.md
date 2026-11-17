# Load test: docker_scale3

SLO: complete-run p95 <= 30 s (declared in docs/SLO.md before testing)

| users | completed runs | failed | runs/s | p50 (s) | p95 (s) | max (s) | SLO met |
|---|---|---|---|---|---|---|---|
| 20 | 120 | 0 | 0.96 | 8.7 | 13.0 | 14.3 | yes |
| 40 | 239 | 0 | 1.92 | 12.0 | 18.0 | 29.0 | yes |
| 60 | 325 | 0 | 2.61 | 18.0 | 30.0 | 30.1 | yes |
| 80 | 322 | 0 | 2.56 | 27.0 | 40.0 | 40.6 | NO |
