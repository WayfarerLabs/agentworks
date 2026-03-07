# Agentworks

CLI for orchestrating workspace lifecycle across multiple compute targets (VMs and local host).

## Repository Structure

```text
cli/     Python CLI (uv, Python 3.12+)
tools/   Agent tools and MCP servers (future)
proxy/   Tool proxy service (future)
```

## Getting Started

```bash
cd cli
uv sync
uv run agentworks --help
```
