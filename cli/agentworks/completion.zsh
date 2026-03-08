#compdef agentworks

# Zsh completion for agentworks CLI
# Install: agentworks completion zsh > ~/.zfunc/_agentworks
#   then ensure ~/.zfunc is in your fpath and run compinit

_agentworks_vms() {
    local -a vms
    vms=(${(f)"$(agentworks vm list 2>/dev/null | tail -n +3 | awk '{print $1}')"})
    _describe 'vm' vms
}

_agentworks_vm_hosts() {
    local -a hosts
    hosts=(${(f)"$(agentworks vm-host list 2>/dev/null | tail -n +3 | awk '{print $1}')"})
    _describe 'vm-host' hosts
}

_agentworks_workspaces() {
    local -a workspaces
    workspaces=(${(f)"$(agentworks workspace list 2>/dev/null | tail -n +3 | awk '{print $1}')"})
    _describe 'workspace' workspaces
}

_agentworks() {
    local -a commands
    local curcontext="$curcontext" state line

    _arguments -C \
        '--help[Show help]' \
        '1:command:->command' \
        '*::arg:->args'

    case $state in
        command)
            commands=(
                'init:Create a sample config file'
                'vm:Manage virtual machines'
                'vm-host:Manage VM hosts'
                'workspace:Manage workspaces'
            )
            _describe 'command' commands
            ;;
        args)
            case $line[1] in
                vm)
                    _agentworks_vm "$@"
                    ;;
                vm-host)
                    _agentworks_vm_host "$@"
                    ;;
                workspace)
                    _agentworks_workspace "$@"
                    ;;
            esac
            ;;
    esac
}

_agentworks_vm() {
    local -a subcommands
    _arguments -C \
        '--help[Show help]' \
        '1:subcommand:->subcommand' \
        '*::arg:->args'

    case $state in
        subcommand)
            subcommands=(
                'create:Create a new VM'
                'list:List VMs'
                'shell:Open a shell on a VM'
                'start:Start a stopped VM'
                'stop:Stop a running VM'
                'delete:Delete a VM'
            )
            _describe 'subcommand' subcommands
            ;;
        args)
            case $line[1] in
                create)
                    _arguments \
                        '--name[VM name]:name:' \
                        '--platform[Platform]:platform:(lima azure wsl2)' \
                        '--vm-host[VM host]:host:_agentworks_vm_hosts' \
                        '--vm-user[Username on VM]:user:' \
                        '--cpus[Number of CPUs]:cpus:' \
                        '--memory[Memory in GiB]:memory:' \
                        '--disk[Disk in GiB]:disk:' \
                        '--azure-vm-size[Azure VM size]:size:' \
                        '*--extra-packages[Additional apt packages]:package:' \
                        '*--git-hosts[Git hosts to register]:host:' \
                        '--help[Show help]'
                    ;;
                shell|start|stop|delete)
                    _arguments \
                        '1:vm:_agentworks_vms' \
                        '--help[Show help]'
                    ;;
                delete)
                    _arguments \
                        '1:vm:_agentworks_vms' \
                        '--force[Force delete]' \
                        '--help[Show help]'
                    ;;
            esac
            ;;
    esac
}

_agentworks_vm_host() {
    local -a subcommands
    _arguments -C \
        '--help[Show help]' \
        '1:subcommand:->subcommand' \
        '*::arg:->args'

    case $state in
        subcommand)
            subcommands=(
                'add:Register a new VM host'
                'list:List VM hosts'
                'remove:Remove a VM host'
            )
            _describe 'subcommand' subcommands
            ;;
        args)
            case $line[1] in
                remove)
                    _arguments \
                        '1:host:_agentworks_vm_hosts' \
                        '--force[Remove even if VMs reference this host]' \
                        '--help[Show help]'
                    ;;
            esac
            ;;
    esac
}

_agentworks_workspace() {
    local -a subcommands
    _arguments -C \
        '--help[Show help]' \
        '1:subcommand:->subcommand' \
        '*::arg:->args'

    case $state in
        subcommand)
            subcommands=(
                'create:Create a workspace'
                'shell:Open a shell into a workspace'
                'list:List workspaces'
                'delete:Delete a workspace'
            )
            _describe 'subcommand' subcommands
            ;;
        args)
            case $line[1] in
                create)
                    _arguments \
                        '--name[Workspace name]:name:' \
                        '--vm[Target VM]:vm:_agentworks_vms' \
                        '--local[Create a local workspace]' \
                        '--template[Workspace template]:template:' \
                        '--open-vscode[Open in VS Code]' \
                        '--help[Show help]'
                    ;;
                shell)
                    _arguments \
                        '1:workspace:_agentworks_workspaces' \
                        '--no-tmuxinator[Skip tmuxinator]' \
                        '--help[Show help]'
                    ;;
                list)
                    _arguments \
                        '--vm[Filter by VM]:vm:_agentworks_vms' \
                        '--help[Show help]'
                    ;;
                delete)
                    _arguments \
                        '1:workspace:_agentworks_workspaces' \
                        '--yes[Skip confirmation]' \
                        '--help[Show help]'
                    ;;
            esac
            ;;
    esac
}

_agentworks "$@"
