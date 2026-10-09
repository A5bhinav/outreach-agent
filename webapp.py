"""Local web app: `python main.py --serve` opens http://127.0.0.1:8765.

Pick a portfolio company, type a request, approve the plan, watch the run, and land in the
review inbox. Profiles, sender details and API keys are editable from the browser.

Standard library only. Binds to 127.0.0.1, accepts only same-origin requests (a custom header
plus Host/Origin checks, so other websites can't trigger paid runs), and runs one job at a time
because the pipeline's model client and counters are process-wide.
"""
import asyncio
import copy
import csv
import datetime
import json
import mimetypes
import os
import re
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

from agents import demo, llm, profiles
from agents.compliance import Ledger, missing_legal
from agents.planner import plan as make_plan

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui" / "app.html"
OUT = ROOT / "outputs"
LEDGER = ROOT / "ledger.csv"
ENV = ROOT / ".env"
COMPANY_FIELDS = ["company_name", "founder_name", "founder_bio", "pitch", "icp_notes", "proof_points",
                  "stage_and_backers", "investor_note", "offer", "roles"]
SENDER_FIELDS = ["voice", "name", "title", "firm", "email", "firm_address"]


# ---------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind: str, **kw):
        self.id = kw.pop("id", None) or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.kind, self.status, self.log, self.error = kind, "running", [], ""
        self.started, self.finished = time.time(), None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.result = None
        self.__dict__.update(kw)

    def view(self, since: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "error": self.error,
                "log": self.log[since:], "log_len": len(self.log), "elapsed": round((self.finished or time.time()) - self.started)}


_lock = threading.Lock()  # one job at a time: llm's client, limits and counters are process-wide
_active: Job | None = None
_jobs: dict[str, Job] = {}


def _busy() -> str | None:
    return f"a {_active.kind} is already in progress" if _active and _active.status == "running" else None


def _start(job: Job, work) -> None:
    """Run `work(job)` (a coroutine factory) on a fresh event loop in a background thread."""
    global _active
    _active = job
    _jobs[job.id] = job

    def target():
        global _active
        restore = None
        llm.reset()
        llm.sink = job.log
        try:
            if getattr(job, "demo", False):
                restore = demo.install(job.request).restore
            job.loop = asyncio.new_event_loop()
            job.task = job.loop.create_task(work(job))
            job.result = job.loop.run_until_complete(job.task)
            job.status = "done"
        except asyncio.CancelledError:
            job.status, job.error = "cancelled", "Stopped. Anything already finished was kept."
        except SystemExit as e:
            job.status, job.error = "error", str(e)
        except BaseException as e:  # noqa: BLE001 - surface every failure to the browser
            job.status, job.error = "error", f"{type(e).__name__}: {getattr(e, 'message', e)}"
            job.log.append(traceback.format_exc(limit=3))
        finally:
            if restore:
                restore()
            llm.sink = None
            job.finished = time.time()
            if job.loop:
                job.loop.close()
            with _lock:
                if _active is job:
                    _active = None

    threading.Thread(target=target, daemon=True).start()


def _profile_for(company: str, is_demo: bool) -> tuple[dict, str]:
    if is_demo:
        return copy.deepcopy(demo.PROFILE), "demo"
    path = profiles.resolve(company=company or None)
    return profiles.load(path), str(path)


def _readiness(startup: dict, is_demo: bool) -> list[str]:
    if is_demo:
        return []
    problems = []
    if "PLACEHOLDER" in yaml.safe_dump(startup):
        problems.append("the company profile or your sender details still have placeholder values")
    if missing := missing_legal(startup):
        problems.append(f"the email footer needs {', '.join(missing)}")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        problems.append("no Anthropic API key is set (Settings)")
    return problems


def _estimate(p: dict, n: int) -> dict:
    heavy = (-(-3 * n // 2) + 2 if p["mode"] == "companies" else max(5, n // 2 + 2)) + 3 * n
    return {"calls": heavy + len(p["queries"]) + n, "heavy_calls": heavy, "searches": 4 * heavy,
            "minutes": [max(5, n), 3 * max(5, n)]}


# ---------------------------------------------------------------- runs on disk

def _run_dir(run_id: str) -> Path:
    d = (OUT / run_id).resolve()
    if d.parent != OUT.resolve() or not re.fullmatch(r"[\w.-]+", run_id):
        raise ValueError("bad run id")
    return d


def _run_summary(d: Path) -> dict | None:
    rj = d / "run.json"
    if not rj.exists():
        return None
    try:
        st = json.loads(rj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = []
    if (d / "results.csv").exists():
        with open(d / "results.csv", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    job = _jobs.get(d.name)
    status = job.status if job else ("done" if rows else "incomplete")
    company = "Example Robotics (demo)" if d.name.startswith("demo-") else ""
    if not company and st.get("profile"):
        try:
            company = str(yaml.safe_load(Path(st["profile"]).read_text(encoding="utf-8")).get("company_name", ""))
        except (OSError, AttributeError, yaml.YAMLError):
            company = Path(st["profile"]).stem
    return {"id": d.name, "request": st.get("request", "").replace("[DEMO, fictional data] ", ""),
            "company": company.replace("PLACEHOLDER:", "").strip(), "mode": (st.get("plan") or {}).get("mode", ""),
            "demo": d.name.startswith("demo-"), "status": status, "targets": len(rows),
            "ready": sum(r.get("status") == "ready" for r in rows),
            "started": datetime.datetime.fromtimestamp(rj.stat().st_ctime).isoformat(timespec="minutes"),
            "has_inbox": (d / "results.html").exists(),
            "live": [{"target": r["target"], "status": r["status"], "channel": r["channel"],
                      "contact": r["contact_name"], "hook": r["fact_used"]} for r in rows]}


# ---------------------------------------------------------------- settings

def _load_env() -> None:
    """Load KEY=VALUE lines from .env (git-ignored) without overriding the real environment."""
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$", line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip().strip('"').strip("'")


def _save_env(updates: dict[str, str]) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    for k, v in updates.items():
        lines = [x for x in lines if not re.match(rf"\s*(?:export\s+)?{k}\s*=", x)] + [f"{k}={v}"]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass


def _mask(v: str | None) -> str:
    return f"{v[:7]}…{v[-4:]}" if v and len(v) > 12 else ("set" if v else "")


def _state() -> dict:
    comps = []
    for slug, path in profiles.companies().items():
        text = path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text) or {}
        comps.append({"slug": slug, "name": str(cfg.get("company_name", slug)).replace("PLACEHOLDER:", "").strip(),
                      "placeholders": "PLACEHOLDER" in text})
    sender_path = profiles.SENDER if profiles.SENDER.exists() else profiles.SENDER_EXAMPLE
    sender = yaml.safe_load(sender_path.read_text(encoding="utf-8")) or {}
    return {"companies": comps,
            "sender": {k: sender.get(k, "") for k in SENDER_FIELDS},
            "sender_ready": profiles.SENDER.exists() and "PLACEHOLDER" not in yaml.safe_dump(sender)
                            and not missing_legal({"sender": sender, "company_name": "x"}),
            "keys": {"anthropic": _mask(os.environ.get("ANTHROPIC_API_KEY")), "apollo": _mask(os.environ.get("APOLLO_API_KEY"))},
            "busy": _busy(), "active": _active.id if _active and _active.status == "running" else None}


def _clean_company(data: dict) -> dict:
    out = {}
    for k in COMPANY_FIELDS:
        v = data.get(k)
        if k == "proof_points":
            v = [str(x).strip() for x in (v or []) if str(x).strip()]
        elif k == "roles":
            v = [{kk: str(r.get(kk, "")).strip() for kk in ("title", "location", "comp", "why_interesting") if str(r.get(kk, "")).strip()}
                 for r in (v or []) if isinstance(r, dict) and str(r.get("title", "")).strip()]
        else:
            v = str(v or "").strip()
        if v:
            out[k] = v
    return out


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Outreach/1"

    def log_message(self, fmt, *args):  # keep the terminal quiet; the browser shows progress
        pass

    # -- helpers
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = json.dumps(body).encode() if ctype == "application/json" else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code: int, msg: str) -> None:
        self._send(code, {"error": msg})

    def _same_origin(self) -> bool:
        host = self.headers.get("Host", "")
        ok_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return host in ok_hosts and (origin is None or urlparse(origin).netloc in ok_hosts)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # -- routing
    def do_GET(self):
        if not self._same_origin():
            return self._err(403, "forbidden")
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                return self._send(200, UI.read_bytes(), "text/html")
            if path == "/api/ping":
                return self._send(200, {"ok": True})
            if path == "/api/state":
                return self._send(200, _state())
            if path == "/api/runs":
                OUT.mkdir(exist_ok=True)
                runs = [s for d in sorted(OUT.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
                        if d.is_dir() and (s := _run_summary(d))]
                return self._send(200, {"runs": runs})
            if m := re.fullmatch(r"/api/runs/([\w.-]+)", path):
                job, since = _jobs.get(m.group(1)), int((urlparse(self.path).query.partition("since=")[2] or 0))
                summ = _run_summary(_run_dir(m.group(1))) or {}
                return self._send(200, {**summ, "job": job.view(since) if job else None})
            if m := re.fullmatch(r"/api/companies/([\w-]+)", path):
                p = profiles.companies().get(m.group(1))
                if not p:
                    return self._err(404, "no such company")
                return self._send(200, {"slug": p.stem, "profile": yaml.safe_load(p.read_text(encoding="utf-8")) or {}})
            if m := re.fullmatch(r"/runs/([\w.-]+)/(.+)", path):
                d = _run_dir(m.group(1))
                f = (d / m.group(2)).resolve()
                if not str(f).startswith(str(d) + os.sep) or not f.is_file():
                    return self._err(404, "not found")
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype)
            return self._err(404, "not found")
        except ValueError as e:
            return self._err(400, str(e))

    def do_POST(self):
        self._write("POST")

    def do_PUT(self):
        self._write("PUT")

    def _write(self, method: str):
        # A custom header can't be sent cross-origin without a CORS preflight, which we never answer.
        if not self._same_origin() or self.headers.get("X-Outreach") != "1":
            return self._err(403, "forbidden")
        path = urlparse(self.path).path
        try:
            data = self._body()
            if path == "/api/plan" and method == "POST":
                return self._plan(data)
            if path == "/api/runs" and method == "POST":
                return self._run(data)
            if m := re.fullmatch(r"/api/runs/([\w.-]+)/cancel", path):
                job = _jobs.get(m.group(1))
                if not job or job.status != "running" or not job.loop or not job.task:
                    return self._err(409, "that run isn't running")
                job.loop.call_soon_threadsafe(job.task.cancel)
                return self._send(200, {"ok": True})
            if path == "/api/ledger" and method == "POST":
                return self._ledger(data)
            if path == "/api/companies" and method == "POST":
                name = str(data.get("name", "")).strip()
                if not name:
                    return self._err(400, "a company name is required")
                p = profiles.new_company(name)
                return self._send(201, {"slug": p.stem})
            if (m := re.fullmatch(r"/api/companies/([\w-]+)", path)) and method == "PUT":
                p = profiles.companies().get(m.group(1))
                if not p:
                    return self._err(404, "no such company")
                prof = _clean_company(data.get("profile", {}))
                if not prof.get("company_name") or not prof.get("pitch"):
                    return self._err(400, "company name and pitch are required")
                p.write_text(yaml.safe_dump(prof, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
                return self._send(200, {"ok": True})
            if path == "/api/sender" and method == "PUT":
                sender = {k: str(data.get(k, "")).strip() for k in SENDER_FIELDS if str(data.get(k, "")).strip()}
                sender["voice"] = sender.get("voice") if sender.get("voice") in ("investor", "founder") else "investor"
                if not sender.get("name"):
                    return self._err(400, "your name is required")
                profiles.SENDER.write_text(yaml.safe_dump(sender, sort_keys=False, allow_unicode=True), encoding="utf-8")
                return self._send(200, {"ok": True})
            if path == "/api/keys" and method == "POST":
                updates = {}
                for field, env in (("anthropic", "ANTHROPIC_API_KEY"), ("apollo", "APOLLO_API_KEY")):
                    v = str(data.get(field, "")).strip()
                    if v:
                        os.environ[env] = v
                        updates[env] = v
                if data.get("remember") and updates:
                    _save_env(updates)
                return self._send(200, {"ok": True, "saved": bool(data.get("remember") and updates)})
            return self._err(404, "not found")
        except profiles.ProfileError as e:
            return self._err(400, str(e))
        except (ValueError, json.JSONDecodeError) as e:
            return self._err(400, str(e))

    # -- actions
    def _plan(self, data: dict):
        request, is_demo = str(data.get("request", "")).strip(), bool(data.get("demo"))
        if not request:
            return self._err(400, "type a request first")
        startup, profile = _profile_for(str(data.get("company", "")), is_demo)
        if problems := _readiness(startup, is_demo):
            return self._err(400, "Before running: " + "; ".join(problems) + ".")
        n = max(1, min(50, int(data.get("n") or 5)))
        with _lock:
            if busy := _busy():
                return self._err(409, busy)
            job = Job("plan", id="plan-" + str(int(time.time() * 1000)), request=request, demo=is_demo)

            async def work(j):
                if not is_demo:
                    await llm.preflight()
                return await make_plan(request, startup)
            _start(job, work)
        while job.status == "running":  # planning takes seconds; answer when it's done
            time.sleep(0.05)
        if job.status != "done":
            return self._err(502, job.error or "planning failed")
        return self._send(200, {"plan": job.result, "estimate": _estimate(job.result, n), "profile": profile})

    def _run(self, data: dict):
        from main import run  # imported lazily: main imports this module for --serve
        request, is_demo, p = str(data.get("request", "")).strip(), bool(data.get("demo")), data.get("plan")
        if not request or not isinstance(p, dict) or not p.get("queries"):
            return self._err(400, "approve a plan first")
        startup, profile = _profile_for(str(data.get("company", "")), is_demo)
        if problems := _readiness(startup, is_demo):
            return self._err(400, "Before running: " + "; ".join(problems) + ".")
        n = max(1, min(50, int(data.get("n") or 5)))
        with _lock:
            if busy := _busy():
                return self._err(409, busy)
            run_id = ("demo-" if is_demo else "") + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            outdir = OUT / run_id
            outdir.mkdir(parents=True, exist_ok=True)
            ledger = Ledger(outdir / "ledger.csv" if is_demo else LEDGER, startup["company_name"])
            job = Job("run", id=run_id, request=request, demo=is_demo)
            label = f"[DEMO, fictional data] {request}" if is_demo else request

            async def work(j):
                return await run(label, startup, n, outdir, ledger, preflight=False, profile=profile, approved_plan=p)
            _start(job, work)
        return self._send(202, {"id": run_id})

    def _ledger(self, data: dict):
        d = _run_dir(str(data.get("run", "")))
        targets = {str(t) for t in data.get("targets", [])}
        led = Ledger(d / "ledger.csv" if d.name.startswith("demo-") else LEDGER, "")
        n = led.mark_sent(str(d), targets) if data.get("sent", True) else led.unmark_sent(str(d), targets)
        return self._send(200, {"updated": n})


def serve(port: int = 8765, open_browser: bool = True) -> None:
    _load_env()
    OUT.mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_port}/"
    print(f"Outreach is running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
