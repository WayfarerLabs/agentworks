"""Network-name prediction at the pre-reboot safety boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.transports import Transport


def predict_interface_names(target: Transport) -> dict[str, str]:
    """Apply the installed target udev rules without changing an interface."""
    command = r"""
for path in /sys/class/net/*; do
  current=${path##*/}
  [ "$current" = lo ] && continue
  properties=$(udevadm test-builtin net_setup_link "$path" 2>&1) || exit 70
  predicted=
  for property in ID_NET_NAME_ONBOARD ID_NET_NAME_SLOT ID_NET_NAME_PATH ID_NET_NAME_MAC; do
    value=$(printf '%s\n' "$properties" | sed -n "s/^.*${property}=//p" | tail -1)
    if [ -n "$value" ]; then
      predicted=$value
      break
    fi
  done
  printf '%s\t%s\n' "$current" "${predicted:-$current}"
done
""".strip()
    result = target.run(command, sudo=True, check=False)
    if not result.ok:
        raise StateError(
            "Debian upgrade could not predict post-reboot network interface names",
            hint="Inspect udevadm net_setup_link output and pin stable interface names before rebooting.",
        )
    predictions: dict[str, str] = {}
    for line in result.stdout.splitlines():
        current, separator, predicted = line.partition("\t")
        if not separator or not current or not predicted or current in predictions:
            raise StateError("Debian upgrade received invalid interface-name prediction output")
        predictions[current] = predicted
    if not predictions:
        raise StateError("Debian upgrade found no non-loopback network interface to verify")
    return predictions


def snapshot_provider_interface_names(target: Transport) -> dict[str, str]:
    """Record provider-managed names when guest udev does not own the interface."""
    result = target.run(
        "find /sys/class/net -mindepth 1 -maxdepth 1 -printf '%f\\n' | grep -vx lo | sort",
        check=False,
    )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not result.ok or not names:
        raise StateError("Debian upgrade could not inventory provider-managed network interfaces")
    return {name: name for name in names}


def require_stable_interface_names(predictions: dict[str, str]) -> None:
    """Refuse reboot when the installed rules predict a rename."""
    renames = {current: predicted for current, predicted in predictions.items() if current != predicted}
    if not renames:
        return
    detail = ", ".join(f"{current} -> {predicted}" for current, predicted in sorted(renames.items()))
    raise StateError(
        f"Debian upgrade predicts network interface rename(s): {detail}",
        hint=(
            "Pin stable interface names and update the guest network configuration, then rerun vm upgrade. "
            "Agentworks will not risk losing the only reconnect route."
        ),
    )


def verify_interface_names(target: Transport, predictions: dict[str, str]) -> None:
    """Verify every predicted post-reboot name exists after reconnect."""
    result = target.run(
        "find /sys/class/net -mindepth 1 -maxdepth 1 -printf '%f\\n' | grep -vx lo | sort",
        check=False,
    )
    actual = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    missing = sorted(set(predictions.values()) - actual)
    if not result.ok or missing:
        raise StateError(
            "Post-reboot interface names do not match the durable upgrade plan",
            hint="Use the platform console or explicitly restore the VM's managed checkpoint.",
        )
