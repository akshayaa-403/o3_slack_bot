# Project IVY — Live Demo Script

**Audience:** IT / HR / operations leaders evaluating an AI support assistant for Slack.
**Duration:** ~8–10 minutes live, + 5 minutes Q&A.
**One-line positioning:** *"IVY is a support agent that lives in Slack, answers on its own, escalates intelligently, and gets smarter every time your team closes a ticket — running entirely inside your own AWS account."*

---

## 0. Before you start (prep checklist)

Do this 10 minutes before the demo so nothing is cold:

- [ ] Open the Slack workspace and DM the **IVY** bot once ("hi") to warm the Lambda.
- [ ] Confirm the escalation ladder is on: worker `O3_slack_worker` has `ENABLE_ESCALATION_LADDER=true`, `ENABLE_LOCAL_KB=true`, `ENABLE_LLM_CHAT` on.
- [ ] Have a **screenshot** of an error ready to drag in (e.g. the PCQ / access-denied screenshot).
- [ ] Have the `/generate-intents` slash command working (Request URL points at the API Gateway URL).
- [ ] Keep this script on a second screen. Keep the AWS console **closed** — the whole point is that the buyer never has to look at plumbing.
- [ ] Optional: have the AWS CloudWatch Logs tab ready but hidden, only for a technical audience that asks "is this real?".

**Golden rule:** narrate the *value*, click the *product*. Never show code unless asked.

---

## 1. The hook (30 seconds — say this first)

> "Every company here runs the same play for IT and HR support: someone posts a question in Slack, waits, a human eventually answers, and the same question gets asked again next week. IVY breaks that loop. It answers instantly in Slack, escalates only when it has to, hands off to a human cleanly, and — this is the part nobody else does — it **turns every resolved ticket back into an answer** so the next person never has to wait. And it all runs inside *your* AWS account, so your data never leaves your cloud."

Then: *"Let me show you."*

---

## 2. Act 1 — Instant self-service answer (L1: Amazon Lex)

**Do:** In the IVY DM, type a common question that's in the knowledge base:

> `How do I reset my ADAM account password?`

**Expect:** IVY replies with the exact, step-by-step answer instantly.

**Say:**
> "That answer came from **Amazon Lex** — layer 1. We've loaded 330+ of this company's real, previously-resolved issues as intents. This is the cheap, instant, deterministic layer: no LLM cost, no hallucination risk. For the top questions your team asks every day, IVY just *knows*."

**Point out:** notice the buttons under the answer — the user is always in control of whether they got what they needed.

---

## 3. Act 2 — The escalation ladder (the core story)

This is the centerpiece. Ask something the knowledge base does **not** cleanly cover so Lex can't confidently answer.

**Do:** Type a fuzzier / novel question, e.g.:

> `My timesheet won't submit and the deadline is in an hour`

**Expect (L1 → offer L2):** IVY replies something like *"I couldn't find a direct answer for that. Want me to search the knowledge base?"* with an **`Explore Knowledge base`** button.

**Say:**
> "Here's what most bots get wrong — they either force-fit a wrong answer or dump you to a ticket. IVY does neither. Lex wasn't confident, so instead of guessing, it offers to go one layer deeper. **The user stays in control at every step.**"

**Do:** Click **`Explore Knowledge base`**.

**Expect (L2):** IVY searches the internal knowledge base and returns the closest matching article, now with a **`Try another solution`** button.

**Say:**
> "That's layer 2 — the **knowledge base**. Today it's searching this company's own resolved-ticket knowledge, but this is exactly where you'd plug in Confluence, SharePoint, HR policy docs, runbooks — anything. Still no human involved."

**Do:** If it's not quite right, click **`Try another solution`**.

**Expect (L3):** IVY calls the **LLM** (currently Amazon Nova on Bedrock) and returns a concrete, numbered, step-by-step solution.

**Say:**
> "Layer 3 is the **LLM** — and this is a real, reasoned answer, not a canned one. Two things matter here for you as a buyer: first, it's running on **Amazon Bedrock inside your account**, so prompts and data never leave your cloud. Second, the model is **pluggable** — Nova today, Claude or your own model tomorrow, one config change, zero rewrite. You are never locked to one AI vendor."

---

## 4. Act 3 — Conversational follow-up (L3 chat mode)

**Do:** Without clicking anything, just **type a follow-up** as if you're still stuck:

> `I tried that but it still shows an error`

**Expect:** IVY continues the conversation *with context* — it remembers the original issue and the previous answer, and refines its help. Every reply keeps a **`Talk to a support agent`** and **`Resolved`** button.

**Say:**
> "Notice it didn't reset or send me back to square one — I'm now in a **live back-and-forth with the assistant**, and it remembers the whole thread. Users can go up to 50 exchanges. And at *any* moment—" (hover the button) "—one tap gets them a human. No dead ends, ever."

---

## 5. Act 4 — Human handoff (L4: live agent)

**Do:** Click **`Talk to a support agent`**.

**Expect:** IVY acknowledges and initiates the handoff to a human (internal ticket / roster-based routing).

**Say:**
> "When IVY can't close it, it hands off cleanly — with the full conversation attached, so your agent isn't starting from 'hi, how can I help.' The person already tried the automated ladder, so the human only ever sees the *hard* tickets. That's where your expensive support hours should go."

---

## 6. Act 5 — Screenshots (image understanding)

**Do:** Drag an error **screenshot** into the DM (e.g. an access-denied dialog).

**Say while it processes:**
> "Half of real IT tickets are 'here's a screenshot of my error.' IVY reads it."

**Expect:** IVY runs OCR (Amazon Textract), extracts the text, and routes it *through the same escalation ladder* — Lex first, then KB, then LLM.

**Say:**
> "It pulled the text off the image with Textract and ran it through the exact same ladder. No special path — a screenshot is just another question to IVY."

---

## 7. Act 6 — The learning loop (the differentiator — save the best for here)

This is the part competitors can't easily copy. Set it up:

**Say:**
> "Everything so far has been IVY *answering*. Here's IVY *learning*."

**Do:** Run the slash command:

> `/generate-intents`

**Expect:** IVY reads recently **resolved** Jira tickets, clusters and labels them into proposed new intents, and posts a **review card** in Slack with **`Approve`** and **`Deny`** buttons.

**Say:**
> "IVY just looked at tickets your team *already resolved*, grouped the recurring ones, and drafted brand-new self-service answers from them — automatically. A human reviews and approves right here in Slack—"

**Do:** Click **`Approve`** (or **`Deny`** to show the CSV export).

**Say:**
> "—and it's now live in layer 1. So the question that needed a human last week gets answered *instantly* this week. **Your deflection rate compounds over time instead of going stale.** That's the flywheel: every ticket your team closes makes the bot smarter, with a human always in the approval loop for safety."

---

## 8. Close (say this, then stop talking)

> "So in one Slack app: instant answers, a knowledge-base layer, a reasoning LLM, clean human handoff, screenshots, and a learning loop that turns resolved tickets into future answers — all self-hosted in your AWS, model-agnostic, with a human in control at every escalation. It deflects the repetitive volume so your team spends its hours on the tickets that actually need a person."

Then ask: *"Where does your team feel this pain most — IT, HR, or onboarding?"* — and let them talk.

---

## 9. Anticipated questions (have these ready)

| They ask | You answer |
|---|---|
| **"Does our data go to OpenAI/Anthropic?"** | "No. The LLM runs on Amazon Bedrock inside your own AWS account. Prompts and documents stay in your cloud. The model is also swappable — you're not tied to any one vendor." |
| **"How accurate is it? Won't it hallucinate?"** | "The first two layers are deterministic — Lex intents and knowledge-base retrieval, no generation. The LLM only kicks in when those miss, and every escalation is user-driven with a one-tap human handoff, so a wrong answer is never a dead end." |
| **"How much work to set up?"** | "It deploys into your AWS account. We seed layer 1 from your existing resolved tickets — the same `/generate-intents` flow you just saw — so it's productive on day one and improves weekly." |
| **"How is this different from just adding Claude to Slack?"** | "A raw LLM in Slack is only layer 3. It has no cheap deterministic first layer, no retrieval over *your* docs, no human handoff workflow, and — critically — no learning loop that converts resolved tickets into reusable answers. IVY is the whole support workflow, not just a chat box." |
| **"What does it cost to run?"** | "It's your AWS bill — mostly serverless Lambda and per-call Bedrock, so you pay per use, not a fat per-seat SaaS license. The deterministic layers keep LLM spend low because they answer the common stuff for free." |
| **"Can it do Teams instead of Slack?"** | "The architecture is channel-agnostic — the same backend can front Microsoft Teams or a web widget. Slack is just the first surface." |

---

## 10. Timing cheat-sheet

| Act | Time | Must-hit line |
|---|---|---|
| Hook | 0:30 | "turns every resolved ticket back into an answer" |
| L1 Lex | 1:00 | "instant, no hallucination risk" |
| Ladder L1→L2→L3 | 2:30 | "user stays in control at every step" + "pluggable model, in your cloud" |
| L3 chat | 1:00 | "remembers the thread, human one tap away" |
| Live agent | 0:45 | "humans only see the hard tickets" |
| Screenshot | 1:00 | "a screenshot is just another question" |
| Learning loop | 2:00 | "every ticket your team closes makes the bot smarter" |
| Close | 0:30 | "deflects the repetitive volume" |

If you're short on time, **cut the screenshot act** — never cut the learning loop (Act 6). That's what they'll remember.
