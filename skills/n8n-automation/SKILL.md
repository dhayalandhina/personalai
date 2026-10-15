---
name: n8n-automation
description: Build and operate workflows on Dhina's self-hosted n8n via its REST API — list/inspect/create/activate workflows, trigger webhooks, read executions and errors, scaffold common automations (lead intake, WhatsApp/Telegram alerts, scraping, scheduled reports). Use when Dhina mentions n8n, an automation/workflow, a webhook, or "automate this".
triggers: [n8n, workflow, automation, automate, webhook, trigger, execution failed, node, zapier alternative]
metadata:
  version: "1.0"
  owner: dhina
---

# n8n-automation

Config in `$DD_HOME/.env`: `N8N_URL` (e.g. `http://localhost:5678` or `https://n8n.yourdomain`), `N8N_API_KEY` (n8n → Settings → API). Self-hosted = free.

## Commands
```bash
python3 scripts/n8n.py list [--active]                         # id, name, active, updated
python3 scripts/n8n.py get ID [--nodes]                         # workflow JSON (summary or full)
python3 scripts/n8n.py create workflow.json [--activate]        # from a file (n8n export format)
python3 scripts/n8n.py update ID workflow.json
python3 scripts/n8n.py activate ID | deactivate ID
python3 scripts/n8n.py executions [--workflow ID] [--status error] [--max 20]
python3 scripts/n8n.py execution EXEC_ID                        # error message + failing node
python3 scripts/n8n.py webhook PATH --json '{"k":"v"}' [--test] # fire a webhook trigger
python3 scripts/n8n.py scaffold TEMPLATE --out wf.json --name "..."   # templates: webhook-to-telegram, cron-report, form-lead-capture, rss-digest
```

## Building a workflow (method)
1. `scaffold` the closest template → edit nodes in the JSON (node `type`, `parameters`, `connections`). Keep credentials as references by name; **never paste secrets into workflow JSON**.
2. `create --activate` only after Dhina sees the summary (name, trigger, nodes, what it sends where). Workflows that send messages/emails to other people or write to client systems are created **inactive** and activated by him.
3. Verify: `executions --workflow ID --max 3` after a test trigger; fix the failing node by reading `execution EXEC_ID`.
4. Document in the reply: trigger → steps → output, one line each. Save a copy of the final JSON next to the project.

## Rules
- Track B automations only on Dhina's n8n. Track A (employer) automations live on the employer's instance with its own URL/key — never mix `.env` values.
- Error loops: a workflow failing every run → deactivate, report, don't let it retry forever.
- Rate limits / external APIs → prefer free tiers and add a Wait node; no paid API nodes unless he named them.
