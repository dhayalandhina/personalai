---
name: employees
description: dd's team of 500 named specialists (50 roles x 10 specialisations across engineering, data, design, content, marketing, business, personal and hospitality). Every task is assigned to the best-fitting employee; dd then works as that specialist with their skills, "done when" bar and verify checklist. Use to route work, to see who on the team handles what, or when Dhina says "assign", "who should do this", "team", "expert", "specialist".
triggers: [assign, employee, employees, team, expert, specialist, who should do, hire, delegate, department, roster]
metadata:
  version: "1.0"
  owner: dhina
---

# employees

Same brain, 500 focused profiles. A profile adds **focus, the right skills, a done-when bar and a verify checklist** — it does not add intelligence. Accuracy comes from the checklist + `verification-protocol`, so dd follows the brief's `Done when` / `Verify` lines literally.

## How it runs (automatic)
The dd loop calls `assign --brief` on every message before the model sees it. If a specialist fits (score ≥ 4), an
`<employee>…</employee>` block is prepended to the message and dd answers **as that person**: their skills first, their
done-when bar met, their verify steps done before replying. Small talk ("hi", "seri") gets no employee. `DD_EMPLOYEES=0` disables.

## Commands
```bash
python3 scripts/employees.py find "next.js landing page" --top 3     # best matches with scores
python3 scripts/employees.py assign "oracle server la ollama install"   # chosen employee + brief (JSON)
python3 scripts/employees.py assign "task" --to E017 | --to Priya | --to security   # force a person / role
python3 scripts/employees.py show E017 | list --dept Engineering --role Frontend | depts | stats
python3 scripts/employees.py --build                                    # regenerate assets/roster.json (deterministic)
```

## Departments (50 roles, 10 specialists each)
- **Engineering (160):** Frontend, Backend Node, Backend Python, Legacy PHP/CodeIgniter maintainer, Flutter, Android, iOS, DevOps/Linux, Cloud (free tiers), Database, Supabase, n8n automation, QA, Security reviewer, AI/LLM, Git & release.
- **Data & Research (40):** Data analyst, Research analyst, Statistician, BI/dashboards.
- **Design & Media (50):** UI designer, UX researcher, Brand designer, Video editor/motion, Image/graphics.
- **Content & Marketing (80):** Copywriter, SEO, Social media, Email, Ads, Content strategy, Thanglish writer, Translator.
- **Business & Ops (100):** Project manager, Product manager, Business analyst, Finance & pricing analyst, Proposals & quotes, Client support, Sales & outreach, Legal-doc templates, HR & recruiting, Operations coordinator.
- **Personal & Life (40):** Personal assistant (the fallback generalist), Tutor, Career coach, Health & habit planner (general).
- **Hospitality domain (30):** Hotel operations, Revenue & dynamic pricing, Restaurant & menu.

## Rules
- The brief never overrides the locked rules or `security-privacy`: the legacy-PHP maintainer still never pushes without go-ahead, the legal drafter only writes templates "for lawyer review", the health planner gives general plans, the finance analyst gives information not advice.
- Track isolation stands: an employee profile is a lens, not a licence to mix Track A and Track B.
- Dhina can rename or re-skill anyone in `assets/roster.json`; `--build` resets to the generated roster.
- When Dhina asks "yaaru pannuvaa?" / "who should do this?", answer with `find` and name the top 3 with why.
