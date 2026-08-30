"""Human rendering for the VM description fact record."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output

if TYPE_CHECKING:
    from agentworks.vms.manager.inspect import VMDescription


def render_vm_description(description: VMDescription) -> None:
    """Render VM detail facts with the legacy human layout."""
    vm = description.vm
    for diagnostic in description.diagnostics:
        error = diagnostic.error
        output.warn(f"{error}" + (f"\n{error.hint}" if error.hint else ""))

    site_platform = description.platform or "-"
    backend_label = description.backend or "-"
    status_label = description.observed_status or "-"
    if description.status_disposition is not None:
        status_label += f" ({description.status_disposition})"

    output.info(f"Name:           {vm.name}")
    output.info(f"Created:        {vm.created_at}")
    output.info(f"Site:           {vm.site}")
    output.info(f"Platform:       {site_platform}")
    output.info(f"Backend:        {backend_label}")
    output.info(f"Status:         {status_label}")
    output.info(f"Hostname:       {vm.hostname}")
    slug = description.system_slug
    slug_label = slug or ("(none)" if description.system_slug_state == "declined" else "-")
    if slug and vm.hostname != f"{slug}-{vm.name}":
        slug_label += " (not applied to this VM)"
    output.info(f"System Slug:    {slug_label}")
    output.info(f"Template:       {vm.template or '-'}")
    output.info(f"Admin User:     {vm.admin_username}")
    output.info(f"Provisioning:   {vm.provisioning_status}")
    output.info(f"Initialization: {vm.initialization_status}")
    output.info(f"Tailscale IP:   {vm.tailscale_host or '-'}")

    from agentworks.instance_description import render_instance_state

    render_instance_state(description.instance_state)

    live = description.live_resources
    if vm.cpus is not None or live is not None:
        output.info(f"\n{'Resources':<16}{'Requested':<14}{'Current':<14}{'Used'}")
        output.detail(
            f"{'CPU':<16}"
            f"{str(vm.cpus) if vm.cpus else '-':<14}"
            f"{live.cpus if live else '-':<14}"
            f"{'load ' + live.load_average if live else '-'}"
        )
        output.detail(
            f"{'Memory':<16}"
            f"{str(vm.memory_gib) + 'G' if vm.memory_gib else '-':<14}"
            f"{live.memory_total if live else '-':<14}"
            f"{live.memory_used + ' (' + live.memory_percent + ')' if live else '-'}"
        )
        output.detail(
            f"{'Swap':<16}"
            f"{str(vm.swap_gib) + 'G' if vm.swap_gib else '-':<14}"
            f"{live.swap_total if live else '-':<14}"
            f"{live.swap_used + ' (' + live.swap_percent + ')' if live else '-'}"
        )
        output.detail(
            f"{'Disk':<16}"
            f"{str(vm.disk_gib) + 'G' if vm.disk_gib else '-':<14}"
            f"{live.disk_total if live else '-':<14}"
            f"{live.disk_used + ' (' + live.disk_percent + ')' if live else '-'}"
        )

    if vm.last_seen_at:
        output.info(f"Last Seen:      {vm.last_seen_at}")

    output.info(f"\nAgents ({len(description.agents)}):")
    if description.agents:
        for agent in description.agents:
            grant_label = "all" if agent.grant_all else str(agent.grant_count)
            output.detail(f"{agent.name}  (user: {agent.linux_user}, grants: {grant_label})")
    else:
        output.detail("(none)")

    output.info(f"\nWorkspaces ({len(description.workspaces)}):")
    if description.workspaces:
        for workspace in description.workspaces:
            output.detail(f"{workspace.name}  ({workspace.path})")
            with output.section():
                if workspace.sessions:
                    output.detail(f"Sessions ({len(workspace.sessions)}):")
                    with output.section():
                        for session in workspace.sessions:
                            mode_label = (
                                session.mode
                                if session.mode == "unknown"
                                else f"agent:{session.agent_name}"
                                if session.agent_name
                                else "admin"
                            )
                            output.detail(f"{session.name}  [{session.template}]  {mode_label}")
                else:
                    output.detail("(no sessions)")
    else:
        output.detail("(none)")

    output.info(f"\nEvents ({len(description.events)}):")
    if description.events:
        for event in description.events:
            detail = f"  {event.detail}" if event.detail else ""
            output.detail(f"{event.created_at}  {event.event}{detail}")
    else:
        output.detail("(none)")
