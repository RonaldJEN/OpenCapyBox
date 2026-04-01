# Setup — Memory

Read this on first use. Guide the user through setting up their personal memory system.

## Your Attitude

You're giving them superpowers. Infinite memory, perfectly organized, for anything they want. This is exciting — help them see the possibilities.

**Important:** This is SEPARATE from built-in agent memory. It's a parallel system that complements what already exists.

## The Conversation

### 1. Explain What This Is

"I can set up an infinite memory system for you — separate from my basic memory. It's for anything you want to store long-term: projects, people, decisions, knowledge, collections... whatever matters to you.

It won't interfere with how I normally remember things. This is additional, organized storage that scales as big as you need."

### 2. Ask What They Need

"What kinds of things would be most useful to have perfectly organized?

Some examples people use:
- **Projects** — full history, decisions, context for each project
- **People** — detailed profiles of everyone you work with
- **Decisions** — why you chose X over Y, so you remember later
- **Knowledge** — things you're learning, reference material
- **Collections** — books, recipes, ideas, anything you collect"

Let them tell you. Don't assume.

### 3. Ask About Sync

"My built-in memory already tracks some things. Would you like me to sync any of that into this new system?

For example, I could copy:
- Preferences you've told me
- Important decisions we've made
- Key contacts

Or we can start fresh and only add new things."

### 4. Create the Structure

Based on their answers, create `~/memory/` with:
- `config.md` — their preferences
- `INDEX.md` — root index
- Folders for each category they mentioned
- INDEX.md in each folder

### 5. First Entry

Ask: "What's something you'd like me to remember right now?"

Store it immediately. Show them it works.

## What You're Saving

**In ~/memory/config.md:**
```markdown
# Memory Config

Created: YYYY-MM-DD
Sync from built-in: [yes/no]

## Categories
- projects/
- people/
- [whatever they said]

## Preferences
- [How they want to find things]
- [How often to organize]
```

**In ~/memory/INDEX.md:**
```markdown
# Memory Index

| Category | Description | Index |
|----------|-------------|-------|
| Projects | Project histories | projects/INDEX.md |
| People | Contact network | people/INDEX.md |

Last updated: YYYY-MM-DD
```

## When "Done"

Once you've:
1. Created ~/memory/ with their categories
2. Set up INDEX.md files
3. Stored one real thing

...the system is live. It grows from there through normal use.
