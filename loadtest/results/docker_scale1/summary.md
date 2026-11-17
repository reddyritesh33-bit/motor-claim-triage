# Load test: docker_scale1

SLO: complete-run p95 <= 30 s (declared in docs/SLO.md before testing)

| users | completed runs | failed | runs/s | p50 (s) | p95 (s) | max (s) | SLO met |
|---|---|---|---|---|---|---|---|
| 20 | 106 | 0 | 0.85 | 20.0 | 25.0 | 30.5 | yes |
| 40 | 99 | 0 | 0.79 | 46.0 | 51.0 | 52.7 | NO |
| 60 | 110 | 0 | 0.88 | 50.0 | 73.0 | 76.1 | NO |
