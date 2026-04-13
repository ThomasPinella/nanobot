---
name: skill-creator
description: >
  Create new skills, modify and improve existing skills, and guide users through the skill
  development process. Use when the user wants to create a skill from scratch, turn a workflow
  into a reusable skill, edit or optimize an existing skill, test a skill against sample prompts,
  improve a skill's description for better triggering, or package a skill for distribution.
  Also use when the user says things like "make this a skill", "I want a skill for X",
  "can we turn this into something reusable", or "help me build a skill".
---

# Skill Creator

A skill for creating new skills and iteratively improving them.

## How This Skill Works

Creating a good skill is an iterative process:

1. **Understand** what the skill should do and roughly how
2. **Draft** the skill (or improve an existing one)
3. **Test** it on a few realistic prompts
4. **Evaluate** the results with the user
5. **Improve** based on feedback
6. **Repeat** until it works well
7. **Optimize** the description for reliable triggering
8. **Package** and deliver

Your job is to figure out where the user is in this process and help them move forward. Maybe they say "I want a skill for X" and you start from scratch. Maybe they already have a draft and want to improve it. Maybe they just finished using a skill and noticed it struggles with certain inputs. Meet them where they are.

Be flexible — if the user says "I don't need to run tests, let's just write it," do that. The process is a guide, not a mandate.

## Communicating with the User

Pay attention to context cues about the user's technical level. Some users are experienced developers; others are new to this. In the default case:

- "evaluation" and "test cases" are fine to use
- For terms like "JSON schema", "YAML frontmatter", or "assertions", briefly explain them if you're not sure the user knows what they mean
- Short definitions in parentheses work well: "the frontmatter (the metadata block at the top of the file)"

---

## Creating a Skill

### Step 1: Capture Intent

Start by understanding what the user wants. The current conversation may already contain a workflow the user wants to capture (e.g., "turn this into a skill"). If so, extract what you can from the conversation — tools used, steps taken, corrections made, input/output formats — and confirm with the user before moving on.

Key questions to answer (ask only what's not already clear):

1. **What should this skill enable?** What task or domain does it cover?
2. **When should it trigger?** What would a user say that should activate this skill?
3. **What's the expected output?** Files, messages, actions, a combination?
4. **Are there edge cases or variants?** Different modes, input types, or configurations?

Don't overwhelm the user with questions. Start with the most important ones and follow up as needed.

### Step 2: Interview and Research

Proactively ask about edge cases, input/output formats, example files, success criteria, and dependencies. If the user mentions specific tools or APIs, check whether they're available (MCP servers, CLI tools, etc.).

Conclude this step when you have a clear picture of what the skill should do and how.

### Step 3: Plan Reusable Contents

Before writing, analyze the examples and workflows to identify what should be bundled:

- **Scripts** (`scripts/`): Code that would be rewritten every time. Example: a PDF rotation script, a file converter, a data validator.
- **References** (`references/`): Documentation the agent needs while working. Example: API schemas, database layouts, company policies.
- **Assets** (`assets/`): Files used in output, not loaded into context. Example: templates, boilerplate projects, images.

Ask: "For each example use case, what would I need to recreate from scratch every time?" — that's what should be bundled.

### Step 4: Initialize the Skill

For new skills, run the `init_skill.py` script to generate the directory structure:

```bash
python <skill-creator-path>/scripts/init_skill.py <skill-name> \
  --path <output-directory> \
  [--resources scripts,references,assets] \
  [--examples]
```

For Hazel, custom skills live under the active workspace's `skills/` directory (e.g., `<workspace>/skills/my-skill/SKILL.md`) so they're discovered automatically. You can also place them in the global directory `~/.agents/skills/` to share across workspaces.

Skip this step if improving an existing skill.

#### Skill Naming

- Lowercase letters, digits, and hyphens only (hyphen-case)
- Under 64 characters
- Prefer short, verb-led phrases: `pdf-editor`, `gh-address-comments`
- Directory name must match the `name` in frontmatter

### Step 5: Write the SKILL.md

This is the core of skill creation. A skill has two parts: **frontmatter** (metadata the agent always sees) and **body** (instructions loaded when the skill triggers).

#### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, images, etc.)
```

#### Progressive Disclosure

Skills use three loading levels to manage context efficiently:

1. **Metadata** (name + description) — always in context (~100 words)
2. **SKILL.md body** — loaded when skill triggers (aim for <500 lines)
3. **Bundled resources** — loaded as needed (unlimited; scripts can execute without reading)

Keep SKILL.md lean. When approaching 500 lines, split content into reference files with clear pointers about when to read them.

#### Writing the Frontmatter

```yaml
---
name: my-skill
description: >
  What the skill does and when to use it. Be specific about triggers.
---
```

The `description` is the primary triggering mechanism — it determines whether the agent activates the skill. Include both what the skill does AND specific scenarios that should trigger it. Be slightly "pushy" — the agent tends to under-trigger rather than over-trigger.

Good description example:
> Comprehensive document creation, editing, and analysis with support for tracked changes, comments, and formatting preservation. Use when working with .docx files for creating, modifying, or analyzing professional documents, even if the user doesn't explicitly mention "Word" or "docx".

In Hazel, `metadata` and `always` frontmatter fields are also supported when needed, but keep frontmatter minimal.

#### Writing the Body

The body contains instructions for the agent *after* the skill has triggered. Key principles:

**Explain the why.** The agent is smart. Rather than rigid MUSTs and NEVERs, explain the reasoning behind instructions. "Format dates as ISO 8601 because the downstream parser expects it" is more effective than "ALWAYS format dates as ISO 8601." If you find yourself writing ALWAYS or NEVER in all caps, try reframing as an explanation instead.

**Be concise.** The context window is shared with conversation history, other skills, and the system prompt. Only include information the agent doesn't already know. Challenge each paragraph: "Does this justify its token cost?"

**Prefer examples over explanations.** A concrete example teaches more than a verbose description:

```markdown
## Commit message format
**Example:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication
```

**Set appropriate degrees of freedom.** Match specificity to the task's fragility:
- **High freedom** (text instructions): Multiple approaches valid, context-dependent
- **Medium freedom** (pseudocode/parameters): Preferred pattern exists, some variation OK
- **Low freedom** (specific scripts): Operations are fragile, consistency critical

**Use imperative form.** Write "Extract the data" not "The data should be extracted."

**Structure for the skill's purpose.** Common patterns:
- **Workflow-based**: Sequential steps (good for processes)
- **Task-based**: Grouped operations (good for tool collections)
- **Reference**: Standards and specs (good for guidelines)

When a skill supports multiple variants (frameworks, providers, domains), keep the core workflow in SKILL.md and move variant-specific details into `references/` files:

```
cloud-deploy/
├── SKILL.md (workflow + selection logic)
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```

#### What NOT to Include

- README.md, CHANGELOG.md, or other auxiliary docs
- Information the agent already knows (general coding knowledge, common tools)
- "When to use this skill" sections in the body (that belongs in the description)
- Duplicate information across SKILL.md and reference files

### Step 6: Test the Skill

After writing the skill, come up with 2-3 realistic test prompts — the kind of thing a real user would actually say. Share them with the user for review.

Good test prompts are:
- **Realistic**: Include personal context, specific file names, casual language
- **Varied**: Cover the main use case, an edge case, and a variant
- **Substantive**: Complex enough that the agent would actually benefit from the skill

Bad: `"Format this data"`, `"Create a chart"`
Good: `"I have a CSV of our Q4 sales in ~/Downloads/sales_q4.csv — can you make a summary with profit margins by region?"`

**Running tests:** Use the skill on each test prompt and observe:
- Did the agent follow the skill's instructions?
- Was the output what you expected?
- Did the agent waste time on unproductive steps?
- Were there gaps in the instructions?

If Hazel's `spawn` tool is available, you can run multiple test cases as background subagents in parallel to save time.

Share results with the user for feedback before making changes.

### Step 7: Iterate

This is the heart of the process. You've tested the skill and gotten feedback. Now improve it.

#### How to Think About Improvements

1. **Generalize from the feedback.** The skill will be used across many different prompts, not just these test cases. Don't overfit to specific examples — if there's a stubborn issue, try different approaches rather than adding narrow patches.

2. **Keep it lean.** Remove instructions that aren't pulling their weight. Read the test transcripts — if the skill makes the agent waste time on unproductive steps, cut those instructions.

3. **Look for repeated work.** If every test case results in the agent writing a similar helper script, that script should be bundled in `scripts/`. Write it once and tell the skill to use it.

4. **Explain, don't command.** If you find yourself adding rigid rules to fix issues, try explaining the reasoning instead. "Users expect the chart to load in under 2 seconds, so prefer SVG over high-res PNG" teaches the agent to make similar judgment calls in novel situations.

#### The Loop

1. Apply improvements to the skill
2. Rerun test cases
3. Share results with the user
4. Read feedback, improve again
5. Repeat until the user is satisfied or feedback is all positive

---

## Improving an Existing Skill

When the user brings an existing skill to improve:

1. Read the current SKILL.md and understand what it does
2. Ask the user what's not working or what they want to change
3. If the user has specific examples of failures, use those as test cases
4. Apply improvements following the same iteration principles
5. Preserve the original skill name — don't rename it

---

## Description Optimization

The description is the most important part of a skill for triggering. After the skill content is solid, optimize the description.

### Writing a Good Description

1. **Start with what the skill does** in plain language
2. **List specific trigger scenarios** — the contexts and phrases that should activate it
3. **Include non-obvious triggers** — things a user might say that should activate this skill even if they don't mention it by name
4. **Be slightly pushy** — err on the side of triggering too often rather than too rarely

### Testing Trigger Quality

Think of 5-10 realistic prompts:
- Half should trigger the skill
- Half should NOT trigger it (but be close enough to be tricky)

For should-trigger prompts, think about coverage: different phrasings, casual vs. formal, explicit vs. implicit references to the skill's domain.

For should-NOT-trigger prompts, focus on near-misses: queries that share keywords but actually need something different. `"Write a fibonacci function"` is too obviously different — `"summarize this PDF for me"` is a better negative test for a PDF editing skill.

Run these prompts mentally or in practice and adjust the description based on what triggers correctly.

---

## Validating a Skill

Before packaging, validate the skill structure:

```bash
python <skill-creator-path>/scripts/quick_validate.py <path/to/skill-folder>
```

This checks:
- SKILL.md exists with valid YAML frontmatter
- Name matches directory name and follows conventions
- Description is complete (no TODOs, under 1024 chars)
- Only allowed files/directories in the skill root

## Packaging a Skill

When the skill is ready for distribution, package it:

```bash
python <skill-creator-path>/scripts/package_skill.py <path/to/skill-folder> [output-directory]
```

This validates the skill and then creates a `.skill` file (a zip archive) containing all the skill's files. Symlinks are rejected for security. The resulting `.skill` file can be shared and installed.

---

## Quick Reference: Skill File Limits

| Component | Guideline |
|-----------|-----------|
| Description | Under 1024 characters, specific triggers included |
| SKILL.md body | Under 500 lines |
| Reference files | Table of contents if over 100 lines |
| Skill name | Under 64 characters, hyphen-case |
| Resource dirs | Only `scripts/`, `references/`, `assets/` |
