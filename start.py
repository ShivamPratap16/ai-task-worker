"""One command to run the demo: the Acme sandbox (:8001) and the worker dashboard (:8000).

    python start.py [--chaos realistic|off|hostile]
"""
import argparse
import subprocess
import sys
import time
import webbrowser

ap = argparse.ArgumentParser()
ap.add_argument("--chaos", default="realistic", choices=["off", "realistic", "hostile"])
ap.add_argument("--no-browser", action="store_true")
ap.add_argument("--host", default="127.0.0.1", help="dashboard bind address; 0.0.0.0 for a hosted demo")
ap.add_argument("--port", type=int, default=8000, help="dashboard port")
args = ap.parse_args()

procs = [
    subprocess.Popen([sys.executable, "-m", "sandbox", "--chaos", args.chaos]),
    subprocess.Popen([sys.executable, "-m", "worker", "serve", "--host", args.host, "--port", str(args.port)]),
]
print(f"Sandbox:   http://127.0.0.1:8001  (chaos: {args.chaos})")
print(f"Dashboard: http://{args.host}:{args.port}")
print("Ctrl+C to stop.")
time.sleep(2)
if not args.no_browser:
    webbrowser.open(f"http://127.0.0.1:{args.port}")
try:
    while all(p.poll() is None for p in procs):
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
