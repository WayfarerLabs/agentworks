Please also reference the following rules as needed. The list below is provided in TOON format, and `@` stands for the project root directory.

rules[11]:
  - path: @.codex/memories/always-consider-completions.md
  - path: @.codex/memories/always-consider-docs.md
  - path: @.codex/memories/always-consider-sample-config.md
  - path: @.codex/memories/always-consider-sdd-artifacts.md
  - path: @.codex/memories/ask-questions.md
    description: Prefer asking clarifying questions over making assumptions
    applyTo[1]: **/*
  - path: @.codex/memories/cli-conventions.md
    description: CLI command shape and naming conventions
    applyTo[3]: **/agentworks/cli/**/*.py,**/completions/**/*.py,**/agentworks/**/manager.py
  - path: @.codex/memories/code-style.md
    description: General style and formatting guidelines
    applyTo[1]: **/*
  - path: @.codex/memories/development-process.md
    description: Follow the standard agentic development process on every effort
    applyTo[1]: **/*
  - path: @.codex/memories/latest-stable-versions.md
    description: Always use the latest stable version when installing or updating software
    applyTo[1]: **/*
  - path: @.codex/memories/permission-to-fail.md
    description: It is ok to say you don't know or that something isn't working
    applyTo[1]: **/*
  - path: @.codex/memories/push-back.md
    description: Push back respectfully when you see a better path
    applyTo[1]: **/*

# Overview

Welcome to Agentworks! This project is a collection of tools, libraries, and best practices for
agentic software development.

Please take a look around and familiarize yourself with the structure of the project.
