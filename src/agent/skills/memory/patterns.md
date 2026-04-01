# Organization Patterns

## Pattern 1: Category-Based Structure

Most common. Organize by type of information:

```
~/memory/
├── projects/
├── people/
├── decisions/
├── knowledge/
└── collections/
```

**Best for:** General use, multiple domains.

---

## Pattern 2: Domain-Focused Structure

Everything organized around one domain:

```
~/memory/
├── clients/
├── deals/
├── products/
├── competitors/
└── market-research/
```

**Best for:** Professionals focused on one area (sales, research, etc).

---

## Pattern 3: Time-Based Structure

Organized by when things happened:

```
~/memory/
├── 2026/
│   ├── q1/
│   └── q2/
├── 2025/
└── archive/
```

**Best for:** Journaling, logging, historical tracking.

---

## Pattern 4: Hybrid Structure

Mix of categories and time:

```
~/memory/
├── active/           # Current focus
│   ├── projects/
│   └── people/
├── reference/        # Always relevant
│   ├── knowledge/
│   └── preferences/
└── archive/          # Historical
    ├── 2025/
    └── 2024/
```

**Best for:** People who need both current and historical context.

---

## Pattern 5: Growing a Category

When a category gets big, split it:

**Before (100+ entries):**
```
~/memory/projects/INDEX.md  # Too long
```

**After (split by status):**
```
~/memory/projects/
├── INDEX.md          # Just points to subdirs
├── active/
│   └── INDEX.md      # 20 entries
├── paused/
│   └── INDEX.md      # 15 entries
└── archived/
    └── INDEX.md      # 100+ entries (OK, rarely accessed)
```

---

## Pattern 6: Syncing from Built-In Memory

If user wants to copy info from agent's built-in memory:

```
~/memory/sync/
├── INDEX.md
├── preferences.md    # Copied from MEMORY.md
└── key-decisions.md  # Copied from MEMORY.md
```

**Sync process:**
1. Read from built-in (MEMORY.md, etc)
2. Reformat for this system
3. Write to ~/memory/sync/
4. Update ~/memory/sync/INDEX.md with sync date

**Never modify built-in memory.** Sync is read-only.

---

## Pattern 7: Quick Capture → Organize Later

For fast entry without thinking about structure:

```
~/memory/
├── inbox/
│   └── INDEX.md      # Unsorted items
├── projects/
└── ...
```

**Flow:**
1. Capture to inbox/ immediately
2. Weekly: sort inbox/ into proper categories
3. Delete from inbox/ after sorting

---

## Pattern 8: Cross-References

When items relate to multiple categories:

```markdown
# ~/memory/projects/alpha.md

## Team
- Alice (PM) → see people/alice.md
- Bob (Dev) → see people/bob.md

## Key Decisions
- Database choice → see decisions/2026.md#database-alpha
```

**Use relative links.** Never duplicate content.

---

## Pattern 9: Archiving Old Content

When content is old but might be needed:

**Don't delete. Archive:**
```bash
# Move to archive
mv ~/memory/projects/old-thing.md ~/memory/archive/projects/

# Update indices
# 1. Remove from projects/INDEX.md
# 2. Add to archive/INDEX.md
```

**Archive INDEX.md:**
```markdown
# Archive

| Item | Type | Archived | Reason |
|------|------|----------|--------|
| OldProject | project | 2026-01 | Completed |
```

---

## Pattern 10: Search Optimization

Make content findable with good keywords:

```markdown
# ~/memory/people/alice.md

# Alice Smith

**Keywords:** PM, product manager, Acme Corp, alpha project, weekly sync

## Profile
...
```

When searching, keywords at top help grep/semantic search find the right file.
