# Courtier Skill Authoring Guide

## Overview

Skills are Markdown documents with YAML frontmatter. They define reusable
workflows that the OrchestratorAgent can dispatch to SubAgents.

## Quick Start

Create a skill file in your domain's `skills/` directory:

```markdown
---
name: my_skill
description: A one-line summary of what this skill does
version: "1.0"
type: skill
tools: [tool_name_1, tool_name_2]
input_model: MySkillInput
mode: auto
timeout_seconds: 600
retry_policy: on_error
---

# My Skill

## Purpose

Describe what this skill accomplishes.

## Workflow

1. Parse the input document
2. Validate against rule set
3. Generate audit report

## Output Format

Return results as structured JSON:

```json
{
  "status": "pass",
  "issues": [],
  "summary": "..."
}
```

## Error Handling

- If the document cannot be parsed, return an error with reason
- If no rules match, return an empty issues list

## Examples

### Successful Audit
Input: valid document
Output: { "status": "pass", "issues": [], "summary": "All checks passed" }

### Failed Audit
Input: document with format errors
Output: { "status": "fail", "issues": [...], "summary": "N format issues found" }
```

## Frontmatter Reference

| Field | Required | Type | Default | Description |
|-------|----------|------|---------|-------------|
| name | Yes | string | — | Unique skill identifier (snake_case) |
| description | Yes | string | — | One-line summary |
| version | Yes | string | — | Semantic version of the skill |
| type | Yes | string | — | Must be "skill" |
| tools | Yes | string[] | — | Tool names this skill requires |
| input_model | No | string | — | Pydantic model for skill input |
| mode | No | string | "auto" | Execution mode |
| timeout_seconds | No | integer | 600 | Max execution time |
| retry_policy | No | string | "none" | Retry behavior |

## Execution Modes

| Mode | Description |
|------|-------------|
| sequential | Run skill steps one at a time |
| parallel | Run independent steps concurrently |
| auto | Orchestrator decides (default) |

## Retry Policies

| Policy | Description |
|--------|-------------|
| none | No retry on failure (default) |
| on_error | Retry once if the skill returns an error |
| on_timeout | Retry once if the skill times out |

## Skill Body

The Markdown body after the frontmatter is the SubAgent's system prompt.
Use it to define:

- **Purpose**: What the skill accomplishes
- **Workflow**: Step-by-step instructions
- **Output Format**: Expected response structure
- **Error Handling**: How to handle failures
- **Examples**: Input/output pairs for clarity

The body supports full Markdown: headings, lists, code blocks, tables.

## Best Practices

1. **Be specific**: Give concrete steps, not abstract guidance
2. **Define output format**: Use a JSON schema or structured format
3. **Include examples**: Show expected input/output pairs
4. **Handle errors**: Document how to respond to common failure modes
5. **Keep it focused**: One skill = one well-defined task
6. **Version your skills**: Increment version when changing behavior

## Prompt Template Keys

Skills can reference the following template variables in their body:

- `{{ task }}` — the user's task description
- `{{ file_path }}` — path to the uploaded file
- `{{ skill_name }}` — this skill's name
- `{{ skill_description }}` — this skill's description

These are automatically injected by the PromptEngine at dispatch time.
