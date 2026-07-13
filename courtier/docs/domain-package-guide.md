# Courtier Domain Package Guide

## Overview

A Courtier domain package extends the platform with domain-specific capabilities.
Each domain package is a self-contained directory with plugins, skills, rules,
and localized prompts.

## Quick Start

1. Copy the template: `cp -r domains/docaudit domains/my-domain`
2. Edit `config/domain.yaml` with your domain metadata
3. Add plugins in `plugins/`
4. Add skills in `skills/`
5. Add prompts in `config/prompts/{locale}/`
6. Validate: `courtier validate-domain domains/my-domain`

## Directory Structure

```
my-domain/
├── config/
│   ├── domain.yaml          # Required: domain metadata
│   └── prompts/
│       ├── zh-CN/           # Chinese prompts
│       │   ├── orchestrator.yaml
│       │   ├── subagent.yaml
│       │   ├── behavioral.yaml
│       │   ├── chat.yaml
│       │   ├── errors.yaml
│       │   └── tools.yaml
│       └── en-US/           # English prompts
│           └── ...
├── plugins/                 # JSON-RPC plugins
│   └── my_tool/
│       ├── plugin.yaml
│       └── entry.py
├── skills/                  # Skill definitions (.md)
│   └── my_skill.md
├── rules/                   # Domain rules (JSON/YAML)
│   └── my_rules.json
├── models/                  # Domain data models (optional)
│   └── my_model.py
└── pyproject.toml
```

## domain.yaml Reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| name | Yes | string | Unique domain identifier (kebab-case) |
| title | No | string | Human-readable display title |
| description | No | string | One-line description of the domain |
| locales | Yes | string[] | Supported locale tags (e.g. zh-CN, en-US) |
| requires_plugins | No | string[] | Plugin names this domain depends on |
| requires_services | No | string[] | Infrastructure services needed (mysql, elasticsearch, minio) |

### Example

```yaml
name: my-domain
title: My Domain
description: Custom document processing domain
locales: [en-US, zh-CN]
requires_plugins: [my_parser, my_validator]
requires_services: [mysql]
```

## Prompt Templates

Domain packages provide locale-specific prompt templates as YAML files.
Each file contains one or more named templates used by the PromptEngine.

### Reserved Template Keys

The core engine recognizes these template keys:

| Key | Purpose |
|-----|---------|
| orchestrator.system_prompt | Main orchestrator identity and instructions |
| orchestrator.task_decomposition | Task breakdown prompt |
| orchestrator.workflow_rules | Orchestrator behavior rules |
| subagent.system_prompt | SubAgent identity and instructions |
| subagent.tool_call_reminder | Reminder to continue tool execution |
| behavioral.thinking_directive | Concise reasoning rules |
| behavioral.rules | Full behavioral rule set |
| behavioral.pre_turn_reminder | Per-turn constraint reminder |
| behavioral.periodic_reminder | Periodic rule review reminder |
| chat.system_prompt | Chat agent identity |
| chat.welcome_message | Initial greeting |
| errors.tool_timeout | Tool timeout message |
| errors.permission_denied | Permission error message |
| errors.tool_not_found | Missing tool error message |
| errors.internal_error | Internal error message |
| tools.invocation_rules | Tool calling rules |
| ui.agent_name | Agent display name |

Templates use Jinja2 syntax. Variables are injected at render time.

## Plugin Requirements

Each plugin in `plugins/` must have a `plugin.yaml` manifest:

```yaml
name: my_tool
version: "1.0"
type: tool
description: "What this plugin does"
tools:
  - name: my_tool_action
    description: "Action description"
    parameters:
      type: object
      properties:
        param1:
          type: string
          description: "Parameter description"
      required: [param1]
```

## Skill Requirements

Each skill in `skills/` is a Markdown file with YAML frontmatter:

```markdown
---
name: my_skill
description: What this skill does
version: "1.0"
type: skill
tools: [my_tool_action]
input_model: MySkillInput
mode: auto
timeout_seconds: 600
retry_policy: on_error
---

# My Skill

Skill body — natural language instructions for the SubAgent.
```

## Validation

Run the CLI to validate your domain package:

```bash
courtier validate-domain domains/my-domain/
```

This checks:
- domain.yaml exists and is valid
- All declared locales have prompt YAML files
- All required plugins exist with valid plugin.yaml
- skills/ directory has at least one .md file
