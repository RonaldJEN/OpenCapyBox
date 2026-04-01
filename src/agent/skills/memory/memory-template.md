# Memory Templates

## System Configuration

Create `~/memory/config.md`:

```markdown
# Memory Config

Created: YYYY-MM-DD
Owner: [name]

## Sync Settings
sync_from_builtin: false
sync_categories: []

## Categories
- projects/
- people/
- decisions/
- [custom]/

## Preferences
find_method: navigate | search | both
maintenance: weekly | monthly
```

---

## Root Index

Create `~/memory/INDEX.md`:

```markdown
# Memory Index

## Categories

| Category | Items | Updated | Index |
|----------|-------|---------|-------|
| Projects | 12 | 2026-02-22 | projects/INDEX.md |
| People | 45 | 2026-02-20 | people/INDEX.md |
| Decisions | 23 | 2026-02-22 | decisions/INDEX.md |

## Quick Stats
Total items: ~80
Last maintenance: 2026-02-15
```

---

## Projects

**Index: `~/memory/projects/INDEX.md`**
```markdown
# Projects Index

| Project | Status | Stack | Updated | File |
|---------|--------|-------|---------|------|
| Alpha | Active | React | 2026-02 | alpha.md |
| Beta | Paused | Python | 2026-01 | beta.md |

Active: 5 | Paused: 3 | Archived: 20
```

**Entry: `~/memory/projects/{name}.md`**
```markdown
# Project: [Name]

## Overview
Status: active | paused | complete
Started: YYYY-MM-DD
Stack: [technologies]

## Description
[What it is, why it matters]

## Key Decisions
- [YYYY-MM-DD] [Decision and reasoning]

## History
- [YYYY-MM-DD] [What happened]

## Current State
[Where things stand]

## Next Steps
- [ ] [Action]
```

---

## People

**Index: `~/memory/people/INDEX.md`**
```markdown
# People Index

## By Relationship

### Work
| Name | Role | Company | File |
|------|------|---------|------|
| Alice | PM | Acme | alice.md |

### Clients
| Name | Company | File |
|------|---------|------|
| Bob | ClientCo | bob.md |

### Personal
| Name | Context | File |
|------|---------|------|
| Carol | Friend | carol.md |

Total: 45 contacts
```

**Entry: `~/memory/people/{name}.md`**
```markdown
# [Name]

## Basic Info
Role: 
Company: 
Relationship: work | client | personal
Last contact: YYYY-MM-DD

## How We Know Each Other
[Context]

## Key Facts
- [Important things to remember]

## Communication Style
- [How they prefer to communicate]

## History
- [YYYY-MM-DD] [Interaction]
```

---

## Decisions

**Index: `~/memory/decisions/INDEX.md`**
```markdown
# Decisions Index

## By Year

| Year | Count | File |
|------|-------|------|
| 2026 | 23 | 2026.md |
| 2025 | 89 | 2025.md |

## By Category

| Category | Count | File |
|----------|-------|------|
| Technical | 45 | technical.md |
| Business | 30 | business.md |
| Personal | 37 | personal.md |
```

**Entry: `~/memory/decisions/{category}.md` or `{year}.md`**
```markdown
# Decisions — [Category/Year]

## [YYYY-MM-DD] [Decision Title]

**Decision:** [What was decided]
**Options considered:** [What else was possible]
**Reasoning:** [Why this choice]
**Outcome:** [What happened, if known]
**Revisit:** [When to reconsider, if ever]

---

## [Another Decision]
...
```

---

## Knowledge

**Index: `~/memory/knowledge/INDEX.md`**
```markdown
# Knowledge Index

| Topic | Depth | Updated | File |
|-------|-------|---------|------|
| Machine Learning | Deep | 2026-02 | ml/ |
| Cooking | Growing | 2026-01 | cooking.md |
| Finance | Reference | 2025-12 | finance.md |
```

**Entry: `~/memory/knowledge/{topic}.md`**
```markdown
# [Topic]

## Overview
[What this is about]

## Key Concepts
- **[Concept]:** [Explanation]

## References
- [Source 1]
- [Source 2]

## Notes
[Learnings, insights]

## Questions
- [Things still to learn]
```

---

## Collections

**Index: `~/memory/collections/INDEX.md`**
```markdown
# Collections Index

| Collection | Items | Updated | File |
|------------|-------|---------|------|
| Books | 156 | 2026-02 | books.md |
| Recipes | 45 | 2026-01 | recipes.md |
| Ideas | 89 | 2026-02 | ideas.md |
```

**Entry: Format varies by collection type**

Books example:
```markdown
# Books

## Read
| Title | Author | Rating | Date | Notes |
|-------|--------|--------|------|-------|
| [Book] | [Author] | 5/5 | 2026-01 | [Key takeaway] |

## To Read
- [Book] by [Author] — [Why interested]

## Notes on Specific Books
### [Book Title]
[Detailed notes]
```

---

## Sync Folder (Optional)

If user wants to sync from built-in memory:

**`~/memory/sync/INDEX.md`**
```markdown
# Synced from Built-In Memory

| What | Source | Last Sync | File |
|------|--------|-----------|------|
| Preferences | MEMORY.md | 2026-02-22 | preferences.md |
| Key Decisions | MEMORY.md | 2026-02-22 | decisions.md |

Note: This is one-way sync. Built-in memory is not modified.
```

---

## Index Size Limits

| Index Type | Max Entries | When Exceeded |
|------------|-------------|---------------|
| Root INDEX.md | 20 categories | Unlikely, OK |
| Category INDEX.md | 100 items | Split into subcategories |
| Subcategory INDEX.md | 100 items | Split again |

**Splitting example:**
```
projects/
├── INDEX.md              # "See active/, archived/"
├── active/
│   ├── INDEX.md          # Active projects
│   └── ...
└── archived/
    ├── INDEX.md          # Archived projects
    └── ...
```
