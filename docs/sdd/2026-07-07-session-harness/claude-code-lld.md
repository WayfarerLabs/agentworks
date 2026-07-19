# claude-code harness: low-level design

**Status:** Stub (to be written before Phase 2) **Repo:** `agentworks` **Path:** `cli/agentworks/`

Pins the `claude-code` built-in's tool-specific mechanics. Read `frd.md` R4 and `hla.md`
"claude-code's existence check" and "claude-code config vocabulary" first. Everything here is
verified against the latest stable Claude Code CLI at implementation time (latest-stable rule), not
from memory.

## To pin

- **Resume-vs-launch detection.** How a resumable session named `self._session_name` is detected
  (CLI listing vs on-disk session files); the HLA prefers folding the check into the launch snippet
  so check and launch are one invocation (the FRD "easy strengthening" for the check-to-launch
  race), with a `ctx.agent_target()` probe as the fallback if the one-liner gets awkward. Start and
  restart are symmetric about this.
- **Flag spellings.** Exact CLI flags for `permission_mode`, `model`, and how `extra_args` is
  appended verbatim; the launch-as vs resume invocation forms (today's sample:
  `claude --name {{session_name}}` / `claude --resume {{session_name}}`), reverified against the
  current CLI.
- **Visible decision.** The mechanism that surfaces whether the launch resumed or started fresh (an
  output line vs the pane's first visible output); the decision must never be silent.
- **Required executable.** `claude` probed via the shared `require_commands` helper.
- **Test double.** The fixture/stubbing strategy that exercises detection both directions without a
  real `claude` binary (a stub `Transport` whose `run` returns canned results keyed by the detection
  command).
- **Explicitly out of v1** (recorded only): user-level MCP inheritance, question-timeout control,
  Claude-subscription OAuth, remote-control enablement. `extra_args` is the interim escape hatch.

**Done when:** detection and every flag are verified against the current stable CLI and the test
double is specified, reviewed.
