# Development Process

A daily cadence for continuous development on Noctua/Hootline.

---

## Every Day

### Morning — 15 min
Pick one thing. Priority order:
1. `docs/known-issues.md` — High severity first
2. `docs/incident-log.md` — any open incidents
3. Things you noticed yesterday
4. New features

Write acceptance criteria before touching code:
> "After this is done, [specific observable outcome]"

### During Development
Idea/Bug → Acceptance criteria → Eval task → Build → Run evals → Update docs → Commit

### Evening — 10 min
1. Run `python evals/run_evals.py`
2. Update `docs/known-issues.md`
3. Note tomorrow's priority

---

## Definition of Done
- [ ] `python evals/run_evals.py` passes
- [ ] Relevant doc updated
- [ ] If bug: `docs/incident-log.md` has closed entry with eval id

---

## The Three Prompts

**Daily planning:** "Look at known-issues.md, incident-log.md, feature-inventory.md. What's the one most important thing today? Write acceptance criteria."

**New feature:** "I want to build [X]. Before coding: write 3 evals, check data-architecture.md for schema changes, check system-design.md for architecture changes. Then implement. Run evals when done."

**Bug:** "Bug: [description]. Find root cause, add INC-00X to incident-log.md, fix it, write eval, run evals, confirm passing, close incident."

---

## Files and When to Update

| File | Update when |
|------|-------------|
| `docs/known-issues.md` | Bug found (add) or fixed (remove) |
| `docs/incident-log.md` | Every bug |
| `docs/feature-inventory.md` | Feature added or completed |
| `docs/system-design.md` | Architecture changes |
| `docs/data-architecture.md` | Schema changes |
| `evals/tasks/core.yaml` | Every bug fix + every new feature |

---

## Priority Order
1. High severity open incident
2. Failing eval (regression)
3. Medium severity known issue
4. New feature
