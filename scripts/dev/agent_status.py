"""Per-replica agent status from local worker metrics ports (dev helper).
    python scripts/dev/agent_status.py [port ...]   (defaults: triage 9101, coverage 9111-9113)"""
import re
import sys
import urllib.request

ports = [int(p) for p in sys.argv[1:]] or [9101, 9111, 9112, 9113]
for p in ports:
    try:
        m = urllib.request.urlopen(f"http://localhost:{p}/metrics", timeout=3).read().decode()
    except Exception as exc:
        print(p, "unreachable", exc)
        continue
    tasks = dict(re.findall(r'^agent_tasks_total\{agent="\w+",outcome="(\w+)"\} (\S+)', m, re.M))
    st = dict(re.findall(r'^agent_stage_latency_seconds_(sum|count)\{agent="\w+"\} (\S+)', m, re.M))
    inflight = re.findall(r'^agent_inflight_jobs\{agent="\w+"\} (\S+)', m, re.M)
    mean = float(st.get("sum", 0)) / max(1.0, float(st.get("count", 0)))
    print(f"port {p}: tasks={tasks} stages={st.get('count')} mean_stage_s={mean:.2f} inflight={inflight}")
