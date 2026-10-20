#!/usr/bin/env python3
"""employees.py — dd's team: 500 named specialists (50 roles x 10 specialisations), each with a focus,
the dd skills they use, a "done when" bar and a verify checklist. dd assigns every task to the best one and
works as that specialist. Same brain underneath — the profile adds focus + a checklist, not IQ.

  employees.py find "task text" [--top 3]        best matches (JSON)
  employees.py assign "task text" [--to NAME|ID] [--brief|--json]
                                                  --brief prints the <employee> block the dd loop prepends
                                                  (empty when nothing fits well, e.g. "hi"); --json = full record
  employees.py show ID|NAME | list [--dept D] [--role R] | depts | stats
  employees.py --build                            regenerate assets/roster.json (deterministic)
  employees.py --selftest
Roster: assets/roster.json (edit names/strengths there; --build overwrites). Env: DD_EMPLOYEES=0 disables briefs.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

SKILL_DIR = Path(os.environ.get("DD_SKILL_DIR", Path(__file__).resolve().parent.parent))
ROSTER = SKILL_DIR / "assets" / "roster.json"
SKILLS_ROOT = SKILL_DIR.parent
MIN_SCORE = 4          # below this a task gets no employee brief (small talk, one-liners)
LEVELS = ["Principal", "Principal", "Principal", "Staff", "Staff", "Staff", "Staff", "Senior", "Senior", "Senior"]
GENERALIST_ROLE = "Personal Assistant"

FIRST = ("Arjun Priya Karthik Divya Vikram Meera Suresh Anitha Rahul Kavya Hari Lakshmi Naveen Sneha Ganesh Pooja Ravi Deepa "
         "Manoj Nithya Santhosh Swetha Vijay Revathi Ashwin Gayathri Mohan Janani Praveen Keerthi Sanjay Harini Raj Shruthi "
         "Aravind Varsha Kumar Madhu Balaji Ramya Dinesh Sowmya Gokul Aishwarya Siva Pavithra Saravanan Abinaya Kannan Yamini "
         "Bharath Vidya Surya Nandhini Ajay Preethi Vignesh Sangeetha Rajesh Archana Alex Maya Daniel Sara Leo Nina Omar Elena "
         "Ken Ivy Noah Zara Ethan Lina Mia Aarav Ishaan Riya Tara Rohan Neha Dev Amrita Kiran Farah Yusuf Hana Jonas Luca Sofia "
         "Emma Liam Anand Bhavana Chandru Devi Ezhil Geetha Ilango Jeeva Kalai Mani Nila Oviya Parthi Rekha Sakthi Thamizh "
         "Uma Valli Yazhini Zoya").split()
LAST = ("Krishnan Raman Iyer Nair Pillai Reddy Rao Menon Sharma Singh Patel Khan Das Bose Fernandes Mathew Thomas Joseph "
        "Varghese Chen Kim Garcia Silva Rossi Novak Okafor Haddad Tanaka Lopez Murugan Selvam Durai Velu Annamalai").split()

# dept -> [(role, skills, keywords, done_when, verify, [spec x10])]
ENG_DONE = ["code runs; lint/tests pass", "diff minimal, matches existing style"]
ENG_VERIFY = ["run coding/checks.sh or the tests", "re-read the diff once before answering"]
OPS_DONE = ["service up and enabled; a check command is given"]
OPS_VERIFY = ["run the health/status command", "note the rollback step"]
DATA_DONE = ["numbers recomputed two ways; table or chart delivered"]
DATA_VERIFY = ["recompute from raw data", "state the data range and today's date"]
RES_DONE = ["brief with sources and their dates"]
RES_VERIFY = ["facts fetched today, not recalled", "label Likely/Assumption where unverified"]
DES_DONE = ["asset file delivered at the requested size/format"]
DES_VERIFY = ["open and inspect the output file"]
CONTENT_DONE = ["copy in the requested tone and length, with a clear CTA"]
CONTENT_VERIFY = ["no invented facts, quotes or testimonials", "claims are checkable"]
BIZ_DONE = ["document or plan with owners, dates and next step"]
BIZ_VERIFY = ["every ask mapped to a section", "numbers rechecked"]
PERSONAL_DONE = ["reminder/plan set and confirmed in one line"]
PERSONAL_VERIFY = ["dates and times computed explicitly"]

TEAM: dict[str, list] = {
    "Engineering": [
        ("Frontend Engineer", ["coding", "git-github", "shell-and-files", "browser-automation", "web-search"],
         ["frontend", "ui", "website", "landing", "page", "react", "next", "nextjs", "css", "tailwind", "component", "responsive"],
         ENG_DONE, ENG_VERIFY,
         ["Next.js App Router", "React + TypeScript", "Tailwind & design systems", "Web performance (Core Web Vitals)",
          "Accessibility (WCAG)", "Animations (GSAP/Framer)", "Forms & validation", "State & data fetching",
          "Frontend testing (Playwright)", "SEO-friendly rendering"]),
        ("Backend Engineer (Node)", ["coding", "git-github", "shell-and-files", "web-search"],
         ["backend", "api", "node", "nodejs", "express", "endpoint", "webhook", "auth", "jwt", "rest"],
         ENG_DONE, ENG_VERIFY,
         ["REST API design", "Auth (JWT/OAuth)", "Webhooks & integrations", "Background jobs & queues", "File uploads & media",
          "Rate limiting & caching", "Logging & observability", "Payment gateway integration", "Realtime (WebSocket)", "Serverless functions"]),
        ("Backend Engineer (Python)", ["coding", "git-github", "shell-and-files", "web-search"],
         ["python", "fastapi", "django", "script", "automation", "scraper", "pipeline", "bot"],
         ENG_DONE, ENG_VERIFY,
         ["FastAPI services", "Django apps", "Data pipelines", "Scrapers & automation", "CLI tools", "Async & concurrency",
          "Packaging & virtualenvs", "Testing (pytest)", "Scheduling (cron/APScheduler)", "Telegram/Slack bots"]),
        ("Legacy PHP/CodeIgniter Maintainer", ["coding", "git-github", "shell-and-files", "verification-protocol"],
         ["php", "codeigniter", "ci3", "legacy", "hms", "hotel software", "mysql", "production", "hotfix"],
         ["root cause named; minimal patch only", "no framework or library changes"],
         ["read the surrounding code first", "never commit or push without explicit go-ahead"],
         ["Read-first bug fixing", "Minimal-diff patches", "CI3 controllers & models", "MySQL queries & indexes", "Session/auth issues",
          "Report & PDF generation", "Form validation", "Legacy JS/jQuery", "Cron & batch scripts", "Production hotfix protocol"]),
        ("Flutter Mobile Engineer", ["coding", "git-github", "shell-and-files", "web-search"],
         ["flutter", "dart", "mobile", "app", "android", "ios", "play store", "app store", "widget"],
         ENG_DONE, ENG_VERIFY,
         ["UI & widgets", "State management (Riverpod/Bloc)", "Navigation", "Offline & local DB", "Push notifications",
          "Camera & media", "Play Store release", "App Store release", "Performance & jank", "Platform channels"]),
        ("Android Engineer (Kotlin)", ["coding", "git-github", "shell-and-files", "web-search"],
         ["kotlin", "android", "jetpack", "compose", "gradle", "apk", "play console"],
         ENG_DONE, ENG_VERIFY,
         ["Jetpack Compose UI", "Architecture (MVVM)", "Room & local data", "Networking (Retrofit)", "Background work (WorkManager)",
          "Permissions & privacy", "Play Console & releases", "Crash & ANR fixing", "Gradle & build speed", "Accessibility"]),
        ("iOS Engineer (Swift)", ["coding", "git-github", "shell-and-files", "web-search"],
         ["swift", "swiftui", "ios", "xcode", "iphone", "testflight", "app store"],
         ENG_DONE, ENG_VERIFY,
         ["SwiftUI screens", "UIKit maintenance", "Core Data & persistence", "Networking & Codable", "Push & background modes",
          "In-app purchases", "TestFlight & App Store release", "Performance & memory", "Accessibility", "Widgets & extensions"]),
        ("DevOps / Linux Engineer", ["shell-and-files", "system-ops", "git-github", "web-search"],
         ["devops", "linux", "ubuntu", "server", "vps", "install", "setup", "service", "systemd", "nginx", "docker", "deploy", "ssh", "cron", "backup", "disk"],
         OPS_DONE, OPS_VERIFY,
         ["Ubuntu servers", "systemd services", "Nginx & TLS", "Docker & compose", "CI/CD (GitHub Actions)", "Backups & restore",
          "Monitoring & logs", "Firewall & SSH hardening", "Cron & automation", "Disk/memory troubleshooting"]),
        ("Cloud Engineer (free tiers)", ["shell-and-files", "system-ops", "web-search", "documents"],
         ["cloud", "oracle", "aws", "gcp", "cloudflare", "vercel", "netlify", "domain", "dns", "free tier", "hosting"],
         OPS_DONE, OPS_VERIFY,
         ["Oracle Always Free", "AWS free tier", "GCP free tier", "Cloudflare (DNS/Pages/Workers)", "Vercel/Netlify",
          "Supabase hosting", "Cost control & alerts", "Domains & DNS", "Object storage", "Serverless cron"]),
        ("Database Engineer", ["coding", "shell-and-files", "data-analysis", "documents"],
         ["database", "sql", "mysql", "postgres", "sqlite", "schema", "index", "query", "migration", "table"],
         ENG_DONE, ["run the query against sample data", "check indexes/explain plan"],
         ["MySQL", "PostgreSQL", "SQLite", "Schema design", "Indexing & query tuning", "Migrations", "Backups",
          "Data integrity & constraints", "Reporting queries", "Supabase Postgres (RLS)"]),
        ("Supabase Engineer", ["coding", "git-github", "web-search", "shell-and-files"],
         ["supabase", "rls", "edge function", "postgres", "realtime", "storage bucket", "policy"],
         ENG_DONE, ENG_VERIFY,
         ["Auth", "RLS policies", "Edge functions", "Storage", "Realtime", "Postgres functions & triggers", "Multi-tenant rows",
          "Client SDK (Next.js/Flutter)", "Migrations", "Webhooks"]),
        ("n8n Automation Engineer", ["n8n-automation", "web-search", "coding"],
         ["n8n", "workflow", "automation", "zapier", "webhook", "lead", "crm", "notification", "integration"],
         ["workflow JSON imports and runs; error path handled"], ["trigger it once with test data", "check the execution log"],
         ["Lead capture flows", "WhatsApp/Telegram notifications", "Email sequences", "CRM sync", "Webhook pipelines",
          "Scheduled reports", "AI-step workflows (local LLM)", "Error handling & retries", "Data transformation (Code node)",
          "Multi-tenant workflow templates"]),
        ("QA / Test Engineer", ["coding", "browser-automation", "shell-and-files", "task-planner"],
         ["test", "qa", "bug", "regression", "playwright", "e2e", "repro", "checklist", "release"],
         ["test list with pass/fail; repro steps for every bug"], ["re-run failing cases", "attach exact error text"],
         ["Test plans", "Playwright E2E", "API testing", "Regression suites", "Bug reports (repro steps)", "Mobile testing",
          "Performance testing", "Accessibility checks", "Release checklists", "Flaky-test triage"]),
        ("Security Reviewer", ["security-privacy", "coding", "shell-and-files", "web-search"],
         ["security", "vulnerability", "owasp", "secret", "token", "password", "injection", "hardening", "privacy", "audit"],
         ["findings ranked by severity with a fix for each"], ["re-check each finding against the code", "no secrets in output"],
         ["OWASP web review", "Secrets hygiene", "Auth & session review", "Dependency audit", "Input validation", "File upload safety",
          "Server hardening review", "Privacy & data minimisation", "Prompt-injection defence", "Incident checklist"]),
        ("AI / LLM Engineer", ["local-models", "coding", "web-search", "self-learning"],
         ["llm", "ollama", "model", "prompt", "agent", "rag", "whisper", "embedding", "fine-tune", "ai"],
         ["pipeline runs end to end with a measured result"], ["measure latency/accuracy on a small set", "state model + size used"],
         ["Ollama model selection", "Prompt design", "Tool-calling agents", "RAG over documents", "Fine-tuning / SFT data",
          "Evaluation sets", "Whisper/STT pipelines", "TTS & voice UX", "Guardrails", "Cost & latency optimisation"]),
        ("Git & Release Manager", ["git-github", "coding", "shell-and-files"],
         ["git", "github", "branch", "merge", "release", "tag", "changelog", "pr", "commit", "version"],
         ["clean history; release notes written"], ["git status/log reviewed", "secret scan on staged files"],
         ["Branching strategy", "PR reviews", "Commit hygiene", "Semantic versioning", "Changelogs", "Release tagging", "Hotfix flow",
          "Monorepo basics", "CI gating", "Repo hygiene & secret scans"]),
    ],
    "Data & Research": [
        ("Data Analyst", ["data-analysis", "documents", "web-search"],
         ["data", "analysis", "excel", "csv", "chart", "report", "kpi", "sales", "revenue", "numbers", "trend", "dashboard"],
         DATA_DONE, DATA_VERIFY,
         ["Sales & revenue analysis", "Cohort & retention", "Funnel analysis", "Excel/CSV cleaning", "Charts & dashboards",
          "KPI definitions", "Forecast basics", "A/B test readout", "Survey analysis", "Pricing analysis"]),
        ("Research Analyst", ["web-search", "documents", "verification-protocol"],
         ["research", "compare", "competitor", "market", "best", "options", "which", "evaluate", "review", "latest"],
         RES_DONE, RES_VERIFY,
         ["Market research", "Competitor teardown", "Tech comparison", "Vendor & tool evaluation", "Fact-checking",
          "Literature summary", "Trend scan", "Regulatory basics (India)", "Pricing benchmarks", "Source-cited briefs"]),
        ("Statistician", ["data-analysis", "documents"],
         ["statistics", "average", "mean", "regression", "probability", "sample", "significance", "correlation", "forecast"],
         DATA_DONE, ["show the formula used", "state assumptions and sample size"],
         ["Descriptive stats", "Hypothesis tests", "Regression", "Sampling", "Confidence intervals", "Time series basics",
          "Experiment design", "Data quality checks", "Visualising uncertainty", "Plain-language stats"]),
        ("BI / Dashboard Builder", ["data-analysis", "documents", "coding"],
         ["dashboard", "metric", "kpi", "weekly report", "summary", "occupancy", "revpar", "visualize", "monitor"],
         DATA_DONE, DATA_VERIFY,
         ["Metric trees", "SQL for dashboards", "Matplotlib reports", "HTML dashboards", "Weekly report automation",
          "Alert thresholds", "Reporting data model", "Executive summaries", "Hotel KPIs (occupancy, ADR, RevPAR)", "E-commerce KPIs"]),
    ],
    "Design & Media": [
        ("UI Designer", ["image-generation", "documents", "web-search", "coding"],
         ["design", "ui", "mockup", "screen", "layout", "figma", "color", "font", "typography", "dark mode", "icon"],
         DES_DONE, DES_VERIFY,
         ["Landing pages", "Dashboards", "Mobile app screens", "Design systems & tokens", "Dark mode", "Typography",
          "Colour & contrast", "Iconography", "Empty & loading states", "Handoff specs"]),
        ("UX Researcher", ["documents", "web-search", "task-planner"],
         ["ux", "user", "usability", "journey", "persona", "onboarding", "flow", "survey", "interview", "feedback"],
         BIZ_DONE, ["findings tied to observed behaviour, not guesses"],
         ["User interviews", "Usability tests", "Journey maps", "Personas", "Information architecture", "Onboarding flows",
          "Form UX", "Accessibility audit", "Survey design", "Insight synthesis"]),
        ("Brand Designer", ["image-generation", "documents"],
         ["brand", "logo", "poster", "flyer", "banner", "business card", "identity", "palette", "deck"],
         DES_DONE, DES_VERIFY,
         ["Logo direction", "Brand guidelines", "Social templates", "Pitch deck visuals", "Print (cards/flyers)", "Posters",
          "App icons", "Colour palettes", "Brand voice visuals", "Mockups"]),
        ("Video Editor / Motion", ["video-generation", "shell-and-files", "voice"],
         ["video", "reel", "shorts", "edit", "clip", "subtitle", "caption", "ffmpeg", "storyboard", "thumbnail", "padam"],
         DES_DONE, ["play the output once; check duration and audio"],
         ["Short-form reels", "Product demos", "Explainers", "Captions & subtitles", "Storyboards", "Thumbnails", "Audio cleanup",
          "Transitions & pacing", "Batch rendering (ffmpeg)", "Text-to-video prompts"]),
        ("Image / Graphics Artist", ["image-generation", "shell-and-files"],
         ["image", "photo", "picture", "graphic", "illustration", "background remove", "upscale", "infographic", "generate image"],
         DES_DONE, DES_VERIFY,
         ["Product shots", "Social posts", "Illustrations", "Background removal", "Upscaling", "Thumbnails", "Infographics",
          "Banner ads", "Photo retouch basics", "Prompt craft (FLUX/SDXL)"]),
    ],
    "Content & Marketing": [
        ("Copywriter", ["documents", "web-search"],
         ["copy", "write", "headline", "tagline", "description", "cta", "caption", "ezhuthu", "content"],
         CONTENT_DONE, CONTENT_VERIFY,
         ["Landing page copy", "Ad copy", "Email copy", "Product descriptions", "Taglines", "CTA optimisation",
          "Microcopy & UX writing", "Case studies", "Testimonial framing (real quotes only)", "Thanglish casual copy"]),
        ("SEO Specialist", ["web-search", "documents", "browser-automation"],
         ["seo", "keyword", "rank", "google", "search", "backlink", "meta", "schema", "sitemap", "local seo"],
         ["prioritised list with expected impact"], ["check live pages before claiming issues"],
         ["Keyword research", "On-page SEO", "Technical SEO", "Local SEO (Coimbatore)", "Content briefs", "Internal linking",
          "Schema markup", "Site audits", "Backlink outreach plan", "Rank tracking setup"]),
        ("Social Media Manager", ["documents", "image-generation", "web-search"],
         ["instagram", "linkedin", "social", "post", "reel", "hashtag", "youtube", "whatsapp status", "calendar", "followers"],
         CONTENT_DONE, CONTENT_VERIFY,
         ["Instagram", "LinkedIn", "YouTube Shorts", "WhatsApp Business", "Content calendar", "Hooks & captions",
          "Community replies", "Hashtag strategy", "Analytics readout", "Repurposing"]),
        ("Email Marketer", ["email-imap", "documents"],
         ["email", "newsletter", "mail", "sequence", "drip", "subject line", "outreach", "campaign"],
         CONTENT_DONE, ["sending only with explicit go-ahead", "unsubscribe/consent respected"],
         ["Welcome sequence", "Newsletter", "Re-engagement", "Transactional templates", "Deliverability basics", "Segmentation",
          "Subject lines", "A/B tests", "Consent-aware cold outreach", "Lead drips"]),
        ("Ads Specialist", ["documents", "web-search", "data-analysis"],
         ["ads", "meta ads", "facebook ads", "google ads", "campaign", "budget", "retarget", "conversion", "cpc", "roas"],
         BIZ_DONE, ["budget maths shown", "tracking setup listed"],
         ["Meta ads", "Google Search ads", "YouTube ads", "Retargeting", "Landing page match", "Budget pacing",
          "Creative testing", "Conversion tracking", "Lead-form ads", "Reporting"]),
        ("Content Strategist", ["documents", "web-search", "task-planner"],
         ["content strategy", "blog", "editorial", "topics", "pillar", "funnel", "lead magnet", "plan content"],
         BIZ_DONE, CONTENT_VERIFY,
         ["Content pillars", "Editorial calendar", "Blog outlines", "Lead magnets", "Funnel content", "Repurposing system",
          "Voice & tone guide", "Topic clusters", "Performance review", "Brief templates"]),
        ("Thanglish Content Writer", ["documents"],
         ["thanglish", "tamil", "casual", "script", "broadcast", "festival", "local", "pongal", "diwali", "kadai"],
         CONTENT_DONE, CONTENT_VERIFY,
         ["Casual posts", "Video scripts", "WhatsApp broadcasts", "Reel captions", "Thanglish explainers", "Customer replies",
          "FAQs", "Product stories", "Festival campaigns", "Local-audience hooks"]),
        ("Translator (Tamil / English)", ["documents"],
         ["translate", "translation", "tamil script", "english version", "subtitles", "bilingual", "mozhi"],
         ["meaning preserved; tone matched"], ["back-translate one tricky line to check"],
         ["Marketing copy", "UI strings", "Documents", "Subtitles", "Formal letters", "Tone adaptation", "Glossary building",
          "Proofreading", "Transliteration (Thanglish / Tamil script)", "Bilingual templates"]),
    ],
    "Business & Ops": [
        ("Project Manager", ["task-planner", "documents", "scheduler-reminders"],
         ["project", "plan", "milestone", "sprint", "timeline", "deadline", "status", "track", "schedule", "deliverable"],
         BIZ_DONE, BIZ_VERIFY,
         ["Scope & milestones", "Sprint planning", "Risk register", "Status reports", "Client updates", "Estimation",
          "Dependency tracking", "Retrospectives", "Handover docs", "Timeline recovery"]),
        ("Product Manager", ["documents", "task-planner", "web-search"],
         ["product", "feature", "prd", "user story", "roadmap", "mvp", "prioritize", "spec", "requirements"],
         BIZ_DONE, BIZ_VERIFY,
         ["PRDs", "User stories", "Prioritisation (RICE)", "MVP scoping", "Feature specs", "Roadmaps",
          "Competitive positioning", "Metrics & OKRs", "Launch checklists", "Feedback synthesis"]),
        ("Business Analyst", ["documents", "data-analysis", "task-planner"],
         ["process", "sop", "requirement", "workflow", "erp", "gap", "acceptance", "stakeholder", "business"],
         BIZ_DONE, BIZ_VERIFY,
         ["Requirements gathering", "Process mapping", "Gap analysis", "SOPs", "Automation candidates", "Data flow diagrams",
          "Acceptance criteria", "Stakeholder notes", "Cost-benefit basics", "ERP requirements"]),
        ("Finance & Pricing Analyst", ["data-analysis", "documents"],
         ["price", "pricing", "quote", "cost", "invoice", "budget", "profit", "expense", "vilai", "kanakku", "rate"],
         DATA_DONE, ["show the formula and inputs", "information, not financial advice"],
         ["Freelance quotes", "Project pricing", "Cost estimates", "Invoice tracking", "Cash-flow sheet", "Subscription pricing",
          "Hourly vs fixed analysis", "Expense categorisation", "Profitability per client", "Scenario sheets"]),
        ("Proposal & Quote Writer", ["documents", "web-search"],
         ["proposal", "quote", "scope of work", "sow", "contract", "upwork", "fiverr", "client pitch", "estimate"],
         BIZ_DONE, ["scope, price, timeline, exclusions all present"],
         ["Freelance proposals", "Scope of work", "Quotes", "Contract templates (lawyer review)", "SLAs", "Change requests",
          "Project briefs", "Client onboarding docs", "Upwork/Fiverr proposals", "Case-study pitches"]),
        ("Client Support Agent", ["documents", "email-imap", "memory"],
         ["support", "customer", "ticket", "complaint", "reply", "apolog", "faq", "how to", "issue", "client message"],
         ["reply drafted in the right tone; next step clear"], ["no promises dd can't keep", "sent only with go-ahead"],
         ["Ticket replies", "FAQ building", "Escalation notes", "Tone-calibrated apologies", "How-to guides", "Bug intake",
          "Status updates", "Renewal reminders", "Feedback requests", "Thanglish support replies"]),
        ("Sales & Outreach Writer", ["documents", "web-search", "email-imap"],
         ["sales", "cold email", "dm", "lead", "pitch", "follow up", "discovery", "objection", "partnership", "offer"],
         CONTENT_DONE, ["personalised to the named prospect", "sent only with go-ahead"],
         ["Cold emails", "LinkedIn DMs", "Follow-up sequences", "Discovery call scripts", "Objection handling", "Offer framing",
          "Referral asks", "Partnership pitches", "Demo scripts", "CRM notes"]),
        ("Legal-Doc Drafter (templates)", ["documents", "web-search"],
         ["nda", "agreement", "terms", "privacy policy", "contract", "clause", "refund policy", "legal", "gdpr", "dpdp"],
         ["template with placeholders and a 'lawyer review' note"], ["no legal advice claims", "jurisdiction stated"],
         ["NDA template", "Service agreement template", "Privacy policy draft", "Terms of service draft", "Refund policy",
          "Freelance contract clauses", "IP assignment clause", "GDPR/DPDP basics", "Cookie notice", "Review checklist"]),
        ("HR & Recruiting Writer", ["documents"],
         ["hiring", "job description", "interview", "candidate", "offer letter", "onboarding", "hr", "policy", "team"],
         BIZ_DONE, BIZ_VERIFY,
         ["Job descriptions", "Interview questions", "Offer letters (template)", "Onboarding checklists", "Freelancer agreements",
          "Performance review template", "Policies", "Screening rubric", "Rejection emails", "Team charter"]),
        ("Operations Coordinator", ["scheduler-reminders", "task-planner", "daily-briefing", "documents"],
         ["coordinate", "schedule", "meeting", "notes", "checklist", "vendor", "travel", "event", "weekly review", "follow-up"],
         PERSONAL_DONE, PERSONAL_VERIFY,
         ["Daily plan", "Reminders & follow-ups", "Meeting notes", "Checklists", "Vendor coordination", "Inventory basics",
          "Travel logistics", "Event planning", "Document filing", "Weekly review"]),
    ],
    "Personal & Life": [
        (GENERALIST_ROLE, ["scheduler-reminders", "daily-briefing", "memory", "mac-control", "web-search"],
         ["remind", "reminder", "alarm", "timer", "briefing", "weather", "news", "mani", "time", "open", "play", "note", "nyabagam", "help"],
         PERSONAL_DONE, PERSONAL_VERIFY,
         ["Reminders", "Morning briefing", "Day planning", "Quick lookups", "Mac tasks", "Notes & memory", "Shopping lists",
          "Call & message drafts", "Bill due dates", "Routine building"]),
        ("Tutor / Explainer", ["documents", "web-search", "coding"],
         ["explain", "teach", "learn", "concept", "what is", "how does", "eli5", "study", "exam", "interview prep", "puriya"],
         ["explanation with one example and one check question"], ["example actually runs / adds up"],
         ["Programming concepts", "Maths", "System design", "Interview prep", "Study plans", "Flashcards",
          "Simple explanations (ELI5)", "Exam strategy", "Code walkthroughs", "Thanglish teaching"]),
        ("Career Coach", ["documents", "web-search"],
         ["resume", "cv", "portfolio", "linkedin profile", "career", "freelance profile", "negotiate", "upwork profile", "personal brand"],
         BIZ_DONE, ["claims match real experience only"],
         ["Resume", "Portfolio", "LinkedIn profile", "Freelance positioning", "Rate negotiation", "Client communication",
          "Skill roadmap", "Interview answers", "Upwork profile", "Personal brand"]),
        ("Health & Habit Planner (general)", ["scheduler-reminders", "documents"],
         ["sleep", "exercise", "workout", "habit", "routine", "focus", "stress", "posture", "water", "walk", "diet plan"],
         ["general plan only, not medical advice; 'see a doctor' when relevant"], ["no diagnosis, no dosages"],
         ["Sleep routine", "Exercise schedule", "Screen-time plan", "Hydration & meal timing reminders", "Focus techniques",
          "Stress breaks", "Posture & ergonomics", "Walking plan", "Habit tracking", "When to see a doctor"]),
    ],
    "Hospitality Domain": [
        ("Hotel Operations Analyst", ["documents", "data-analysis", "task-planner"],
         ["hotel", "front desk", "housekeeping", "check-in", "checkout", "guest", "room", "night audit", "ota", "booking"],
         BIZ_DONE, ["KPI formulas shown (occupancy = rooms sold / rooms available)"],
         ["Front desk SOPs", "Housekeeping schedules", "Check-in/out flows", "Guest complaint handling", "Night audit basics",
          "Occupancy reports", "Room inventory", "Staff shift planning", "Guest communication templates", "OTA operations"]),
        ("Revenue & Dynamic Pricing Analyst", ["data-analysis", "documents", "web-search"],
         ["revpar", "adr", "dynamic pricing", "room rate", "occupancy forecast", "rate parity", "season", "tariff", "demand"],
         DATA_DONE, ["ADR = room revenue / rooms sold; RevPAR = ADR x occupancy — shown"],
         ["ADR & RevPAR", "Demand forecasting", "Seasonal pricing", "Competitor rate shopping", "Discount rules",
          "Length-of-stay rules", "Channel mix", "Rate parity", "Promotions", "Pricing dashboards"]),
        ("Restaurant & Menu Specialist", ["documents", "image-generation", "data-analysis"],
         ["restaurant", "menu", "dish", "qr menu", "3d menu", "food cost", "table", "order", "kitchen", "cafe"],
         BIZ_DONE, ["food-cost % = ingredient cost / selling price — shown"],
         ["Menu engineering", "QR menu flows", "3D menu content", "Menu pricing", "Food cost control", "Table turnover",
          "Online ordering UX", "Reviews management", "Festival menus", "Kitchen display basics"]),
    ],
}

SYNONYMS = {"seithi": "news", "mazhai": "weather", "veyil": "weather", "paattu": "music", "padam": "video", "vilai": "price",
            "kanakku": "calculation", "ezhuthu": "write", "mozhi": "translate", "kadai": "shop", "nyabagam": "reminder",
            "site": "website", "web app": "website", "webapp": "website", "automate": "automation", "mobile app": "app",
            "insta": "instagram", "fb": "facebook", "yt": "youtube", "ppt": "deck", "sheet": "excel", "db": "database"}


# ---------------- roster ----------------
def build_roster() -> list[dict]:
    rnd = random.Random(2026)
    pairs = [f"{f} {l}" for f in FIRST for l in LAST]
    rnd.shuffle(pairs)
    out, i = [], 0
    for dept, roles in TEAM.items():
        for role, skills, keywords, done, verify, specs in roles:
            assert len(specs) == 10, (role, len(specs))
            for n, spec in enumerate(specs):
                i += 1
                out.append({
                    "id": f"E{i:03d}", "name": pairs[i - 1], "level": LEVELS[n], "role": role, "dept": dept, "spec": spec,
                    "skills": skills, "keywords": keywords, "done_when": done, "verify": verify,
                    "strengths": [f"{spec}: ships the smallest correct change first", "explains decisions in two lines"],
                })
    assert len(out) == 500 and len({e["name"] for e in out}) == 500
    return out


def load_roster() -> list[dict]:
    if ROSTER.exists():
        return json.loads(ROSTER.read_text(encoding="utf-8"))
    return build_roster()


def save_roster(rows: list[dict]) -> None:
    ROSTER.parent.mkdir(parents=True, exist_ok=True)
    ROSTER.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")


# ---------------- matching ----------------
def normalize(text: str) -> str:
    t = text.lower()
    for k, v in SYNONYMS.items():
        t = re.sub(rf"\b{re.escape(k)}\b", v, t)
    return re.sub(r"[^a-z0-9+#./ ]+", " ", t)


def score(emp: dict, text: str) -> int:
    t = f" {normalize(text)} "
    words = set(re.findall(r"[a-z0-9+#]+", t))
    s = 0
    for kw in emp["keywords"]:
        if " " in kw or len(kw) <= 3:
            s += 4 if f" {kw} " in t or (len(kw) <= 3 and kw in words) else 0
        elif kw in words or any(w.startswith(kw) for w in words):
            s += 4
    stems = {w.rstrip("s") for w in words}
    for w in re.findall(r"[a-z0-9+#.]{4,}", emp["spec"].lower()):
        if w in words or w.rstrip("s") in stems:  # plural-insensitive, but "server" never matches "serverless"
            s += 3
    for w in re.findall(r"[a-z]{4,}", emp["role"].lower()):
        if w in words:
            s += 2
    for w in re.findall(r"[a-z]{4,}", emp["dept"].lower()):
        if w in words:
            s += 1
    return s


def find(text: str, top: int = 3, roster: list[dict] | None = None) -> list[tuple[int, dict]]:
    roster = roster or load_roster()
    ranked = sorted(((score(e, text), e) for e in roster), key=lambda x: (-x[0], x[1]["id"]))
    return ranked[:top]


def lookup(key: str, roster: list[dict]) -> dict | None:
    k = key.strip().lower()
    for e in roster:
        if e["id"].lower() == k or e["name"].lower() == k or e["name"].split()[0].lower() == k:
            return e
    hits = [e for e in roster if k and (k in e["role"].lower() or k in e["spec"].lower())]
    return hits[0] if hits else None


def assign(text: str, to: str | None = None, roster: list[dict] | None = None) -> tuple[dict | None, int, bool]:
    """(employee, score, forced). None when nothing fits (and no --to)."""
    roster = roster or load_roster()
    if to:
        e = lookup(to, roster)
        return (e, 99, True) if e else (None, 0, True)
    best = find(text, 1, roster)[0]
    if best[0] >= MIN_SCORE:
        return best[1], best[0], False
    return None, best[0], False


def brief(emp: dict) -> str:
    return (f"<employee>Assigned: {emp['name']} — {emp['level']} {emp['role']} ({emp['spec']}), {emp['dept']}. "
            f"Done when: {'; '.join(emp['done_when'])}. Verify: {'; '.join(emp['verify'])}. "
            f"Skills: {', '.join(emp['skills'][:4])}.</employee>")


def compact(emp: dict, sc: int | None = None) -> dict:
    d = {k: emp[k] for k in ("id", "name", "level", "role", "dept", "spec", "skills")}
    if sc is not None:
        d["score"] = sc
    return d


# ---------------- CLI ----------------
def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "--selftest":
        return selftest()
    if cmd == "--build":
        save_roster(build_roster())
        print(f"wrote {ROSTER} (500 employees)")
        return 0
    roster = load_roster()
    if cmd == "find":
        hits = find(rest[0] if rest else "", int(arg(rest, "--top", 3)), roster)
        print(json.dumps([compact(e, s) for s, e in hits], ensure_ascii=False, indent=1))
    elif cmd == "assign":
        text = rest[0] if rest and not rest[0].startswith("--") else ""
        if os.environ.get("DD_EMPLOYEES", "1") == "0" and "--to" not in rest:
            print("")
            return 0
        emp, sc, forced = assign(text, arg(rest, "--to"), roster)
        if emp is None:
            print("" if "--brief" in rest else json.dumps({"assigned": None, "score": sc}))
            return 0 if "--brief" in rest else 1
        if "--brief" in rest:
            print(brief(emp))
        else:
            print(json.dumps({**compact(emp, sc), "forced": forced, "done_when": emp["done_when"],
                              "verify": emp["verify"], "brief": brief(emp)}, ensure_ascii=False, indent=1))
    elif cmd == "show":
        e = lookup(rest[0] if rest else "", roster)
        print(json.dumps(e, ensure_ascii=False, indent=1) if e else f"no employee '{rest[0] if rest else ''}'")
        return 0 if e else 1
    elif cmd == "list":
        dept, role = arg(rest, "--dept"), arg(rest, "--role")
        rows = [e for e in roster if (not dept or dept.lower() in e["dept"].lower()) and (not role or role.lower() in e["role"].lower())]
        for e in rows:
            print(f"{e['id']}  {e['name']:<22} {e['level']:<9} {e['role']} — {e['spec']}")
        print(f"{len(rows)} employees")
    elif cmd == "depts":
        for dept, roles in TEAM.items():
            print(f"{dept}: {len(roles) * 10} — " + ", ".join(r[0] for r in roles))
    elif cmd == "stats":
        print(json.dumps({"employees": len(roster), "departments": {d: len(r) * 10 for d, r in TEAM.items()},
                          "roles": sum(len(r) for r in TEAM.values())}, indent=1))
    else:
        print(__doc__)
        return 2
    return 0


def selftest() -> int:
    r1, r2 = build_roster(), build_roster()
    assert r1 == r2 and len(r1) == 500, "roster must be deterministic and 500 strong"
    assert len({e["id"] for e in r1}) == 500 and len({e["name"] for e in r1}) == 500
    have = {p.name for p in SKILLS_ROOT.iterdir() if (p / "SKILL.md").is_file()} if SKILLS_ROOT.is_dir() else set()
    if have:
        missing = {s for e in r1 for s in e["skills"]} - have
        assert not missing, f"roster refers to unknown skills: {missing}"
    cases = {
        "next.js la landing page build pannu": "Frontend Engineer",
        "codeigniter invoice report bug fix pannu production la": "Legacy PHP/CodeIgniter Maintainer",
        "oracle free tier server la ollama install panni systemd service podu": None,  # DevOps or Cloud — both fine
        "whatsapp broadcast ku thanglish festival post ezhuthu": "Thanglish Content Writer",
        "last 3 months sales csv analysis panni chart kudu": "Data Analyst",
        "meta ads campaign budget 10000 plan pannu": "Ads Specialist",
        "nda template venum freelance client ku": "Legal-Doc Drafter (templates)",
        "flutter app la push notification add pannu": "Flutter Mobile Engineer",
        "hotel occupancy revpar report weekly": None,  # BI or Revenue — both fine
        "20 nimisham la tea remind pannu": GENERALIST_ROLE,
        "reels edit panni subtitles podu": "Video Editor / Motion",
        "n8n la lead capture workflow": "n8n Automation Engineer",
    }
    for text, role in cases.items():
        emp, sc, _ = assign(text, roster=r1)
        assert emp is not None and sc >= MIN_SCORE, (text, sc)
        if role:
            assert emp["role"] == role, (text, emp["role"], sc)
    for text in ("hi", "ok", "thanks da", "seri"):
        emp, sc, _ = assign(text, roster=r1)
        assert emp is None, (text, emp and emp["role"], sc)
    oracle = assign("oracle free tier server la ollama install panni systemd service podu", roster=r1)[0]["role"]
    assert oracle in ("DevOps / Linux Engineer", "Cloud Engineer (free tiers)"), oracle
    e, sc, forced = assign("anything", to="E001", roster=r1)
    assert forced and e["id"] == "E001"
    first = r1[7]["name"].split()[0]
    assert assign("x", to=first, roster=r1)[0]["name"].startswith(first)
    assert assign("x", to="security", roster=r1)[0]["role"] == "Security Reviewer"
    b = brief(r1[0])
    assert b.startswith("<employee>Assigned: ") and b.endswith("</employee>") and len(b) < 520, len(b)
    assert len(find("python fastapi service", 3, r1)) == 3 and find("python fastapi service", 1, r1)[0][1]["role"] == "Backend Engineer (Python)"
    os.environ["DD_EMPLOYEES"] = "0"
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["assign", "next.js landing page", "--brief"])
    assert buf.getvalue().strip() == "", "DD_EMPLOYEES=0 must print no brief"
    os.environ.pop("DD_EMPLOYEES")
    print("employees selftest ok (500 deterministic, unique, skills exist, routing, small-talk skipped, --to, brief, disable)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
