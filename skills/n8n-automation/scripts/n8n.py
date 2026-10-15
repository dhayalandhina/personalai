#!/usr/bin/env python3
"""n8n.py — n8n public REST API client + workflow scaffolds (stdlib only).

  list [--active] | get ID [--nodes] | create FILE.json [--activate] | update ID FILE.json
  activate ID | deactivate ID | executions [--workflow ID] [--status error|success] [--max N] | execution ID
  webhook PATH [--json '{...}'] [--test]
  scaffold TEMPLATE --out FILE --name NAME     templates: webhook-to-telegram, cron-report, form-lead-capture, rss-digest
  --selftest
Env: N8N_URL, N8N_API_KEY (also read from $DD_HOME/.env)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


def load_env() -> None:
    p = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def cfg() -> tuple[str, str]:
    load_env()
    url, key = os.environ.get("N8N_URL", "").rstrip("/"), os.environ.get("N8N_API_KEY", "")
    if not url or not key:
        raise RuntimeError("N8N_URL / N8N_API_KEY missing in $DD_HOME/.env")
    return url, key


def api(method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict | list:
    url, key = cfg()
    full = f"{url}/api/v1{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(full, data=body, method=method,
                                 headers={"X-N8N-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def clean_for_create(wf: dict) -> dict:
    """n8n's create endpoint rejects read-only fields."""
    keep = {"name", "nodes", "connections", "settings", "staticData"}
    out = {k: v for k, v in wf.items() if k in keep}
    out.setdefault("settings", {"executionOrder": "v1"})
    return out


def summarize(wf: dict, nodes: bool = False) -> dict:
    s = {"id": wf.get("id"), "name": wf.get("name"), "active": wf.get("active"), "updated": (wf.get("updatedAt") or "")[:19],
         "node_count": len(wf.get("nodes", [])), "node_types": sorted({n.get("type", "") for n in wf.get("nodes", [])})}
    if nodes:
        s["nodes"] = wf.get("nodes")
        s["connections"] = wf.get("connections")
    return s


TEMPLATES = {
    "webhook-to-telegram": {
        "nodes": [
            {"parameters": {"httpMethod": "POST", "path": "dd-intake", "responseMode": "onReceived"}, "name": "Webhook",
             "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0]},
            {"parameters": {"chatId": "={{ $env.TELEGRAM_OWNER_ID }}", "text": "=New: {{ JSON.stringify($json.body) }}", "additionalFields": {}},
             "name": "Telegram", "type": "n8n-nodes-base.telegram", "typeVersion": 1.2, "position": [300, 0],
             "credentials": {"telegramApi": {"name": "dd bot"}}}],
        "connections": {"Webhook": {"main": [[{"node": "Telegram", "type": "main", "index": 0}]]}}},
    "cron-report": {
        "nodes": [
            {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "30 7 * * *"}]}}, "name": "Schedule",
             "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0]},
            {"parameters": {"url": "http://localhost:8000/report", "options": {}}, "name": "HTTP Request",
             "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [300, 0]},
            {"parameters": {"chatId": "={{ $env.TELEGRAM_OWNER_ID }}", "text": "=Report: {{ JSON.stringify($json) }}", "additionalFields": {}},
             "name": "Telegram", "type": "n8n-nodes-base.telegram", "typeVersion": 1.2, "position": [600, 0],
             "credentials": {"telegramApi": {"name": "dd bot"}}}],
        "connections": {"Schedule": {"main": [[{"node": "HTTP Request", "type": "main", "index": 0}]]},
                        "HTTP Request": {"main": [[{"node": "Telegram", "type": "main", "index": 0}]]}}},
    "form-lead-capture": {
        "nodes": [
            {"parameters": {"formTitle": "Contact", "formFields": {"values": [{"fieldLabel": "Name"}, {"fieldLabel": "Phone"}, {"fieldLabel": "Email"}, {"fieldLabel": "Need"}]}},
             "name": "Form", "type": "n8n-nodes-base.formTrigger", "typeVersion": 2.2, "position": [0, 0]},
            {"parameters": {"operation": "append", "documentId": {"__rl": True, "mode": "list", "value": ""}, "sheetName": {"__rl": True, "mode": "list", "value": ""}},
             "name": "Google Sheets", "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5, "position": [300, 0]},
            {"parameters": {"chatId": "={{ $env.TELEGRAM_OWNER_ID }}", "text": "=Lead: {{ $json.Name }} {{ $json.Phone }} — {{ $json.Need }}", "additionalFields": {}},
             "name": "Telegram", "type": "n8n-nodes-base.telegram", "typeVersion": 1.2, "position": [600, 0],
             "credentials": {"telegramApi": {"name": "dd bot"}}}],
        "connections": {"Form": {"main": [[{"node": "Google Sheets", "type": "main", "index": 0}]]},
                        "Google Sheets": {"main": [[{"node": "Telegram", "type": "main", "index": 0}]]}}},
    "rss-digest": {
        "nodes": [
            {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * *"}]}}, "name": "Schedule",
             "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0]},
            {"parameters": {"url": "https://hnrss.org/frontpage?points=100"}, "name": "RSS", "type": "n8n-nodes-base.rssFeedRead",
             "typeVersion": 1.1, "position": [300, 0]},
            {"parameters": {"chatId": "={{ $env.TELEGRAM_OWNER_ID }}", "text": "={{ $json.title }} — {{ $json.link }}", "additionalFields": {}},
             "name": "Telegram", "type": "n8n-nodes-base.telegram", "typeVersion": 1.2, "position": [600, 0],
             "credentials": {"telegramApi": {"name": "dd bot"}}}],
        "connections": {"Schedule": {"main": [[{"node": "RSS", "type": "main", "index": 0}]]},
                        "RSS": {"main": [[{"node": "Telegram", "type": "main", "index": 0}]]}}},
}


def scaffold(template: str, name: str) -> dict:
    if template not in TEMPLATES:
        raise ValueError(f"unknown template; choose from {', '.join(TEMPLATES)}")
    t = json.loads(json.dumps(TEMPLATES[template]))
    return {"name": name, "nodes": t["nodes"], "connections": t["connections"], "settings": {"executionOrder": "v1"}}


def selftest() -> int:
    for k in TEMPLATES:
        wf = scaffold(k, f"test {k}")
        assert wf["nodes"] and set(wf["connections"]) <= {n["name"] for n in wf["nodes"]}, k
        c = clean_for_create({**wf, "id": "x", "active": True, "createdAt": "..."})
        assert "id" not in c and "active" not in c and c["name"] == wf["name"]
    os.environ["DD_HOME"] = tempfile.mkdtemp()
    os.environ.pop("N8N_URL", None)
    try:
        cfg()
        raise AssertionError("cfg must fail without env")
    except RuntimeError:
        pass
    print(f"n8n selftest ok ({len(TEMPLATES)} templates; live API needs N8N_URL/N8N_API_KEY)")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    try:
        if cmd == "list":
            data = api("GET", "/workflows", params={"active": "true"} if "--active" in argv else None)
            res = [summarize(w) for w in data.get("data", [])]
        elif cmd == "get":
            res = summarize(api("GET", f"/workflows/{argv[1]}"), "--nodes" in argv)
        elif cmd == "create":
            wf = clean_for_create(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
            created = api("POST", "/workflows", wf)
            if "--activate" in argv:
                api("POST", f"/workflows/{created['id']}/activate")
            res = summarize(api("GET", f"/workflows/{created['id']}"))
        elif cmd == "update":
            wf = clean_for_create(json.loads(Path(argv[2]).read_text(encoding="utf-8")))
            res = summarize(api("PUT", f"/workflows/{argv[1]}", wf))
        elif cmd in ("activate", "deactivate"):
            res = summarize(api("POST", f"/workflows/{argv[1]}/{cmd}"))
        elif cmd == "executions":
            params = {"limit": arg(argv, "--max", "20")}
            if arg(argv, "--workflow"):
                params["workflowId"] = arg(argv, "--workflow")
            if arg(argv, "--status"):
                params["status"] = arg(argv, "--status")
            res = [{"id": e.get("id"), "workflowId": e.get("workflowId"), "status": e.get("status"), "started": (e.get("startedAt") or "")[:19],
                    "mode": e.get("mode")} for e in api("GET", "/executions", params=params).get("data", [])]
        elif cmd == "execution":
            e = api("GET", f"/executions/{argv[1]}", params={"includeData": "true"})
            run = (e.get("data") or {}).get("resultData", {})
            res = {"id": e.get("id"), "status": e.get("status"), "error": (run.get("error") or {}).get("message"),
                   "last_node": run.get("lastNodeExecuted"), "started": (e.get("startedAt") or "")[:19]}
        elif cmd == "webhook":
            url, _ = cfg()
            path = argv[1].strip("/")
            full = f"{url}/{'webhook-test' if '--test' in argv else 'webhook'}/{path}"
            body = (arg(argv, "--json") or "{}").encode()
            req = urllib.request.Request(full, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                res = {"ok": True, "status": r.status, "response": r.read().decode()[:500]}
        elif cmd == "scaffold":
            wf = scaffold(argv[1], arg(argv, "--name", f"dd {argv[1]}"))
            out = arg(argv, "--out", f"{argv[1]}.json")
            Path(out).write_text(json.dumps(wf, indent=1), encoding="utf-8")
            res = {"ok": True, "file": out, "nodes": [n["name"] for n in wf["nodes"]]}
        else:
            print(__doc__)
            return 2
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
