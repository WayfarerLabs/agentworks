# Mid-Stream Corrections

This is a collection of feedback based on using the system along the way.

## Workstation Update on Commit 904bae3

### Feedback from Agent-Assisted Update

I had a Claude session help me handle the breaking changes are secret-source. This was their
feedback:

```text
Feedback on the docs commands

The doctor hint is the best error message I've hit in a CLI like this. It gave the exact YAML skeleton and quoted my own values back — the real op:// reference, my real account, in the position they needed to move to. That alone is why I could fix this without reading a line of source.

Ranked by what I'd actually change:

1. The hint is missing the [secret_config].backends half of the migration. Following the hint to the letter fixes the manifest and leaves backends = ["onepassword", ...] — still a backend name where a source name is now required. I only learned that from guide concept-migration's remove-retired-sections action, which spells it out ("update [secret_config].backends with source names"). One added line in the hint closes the gap.
2. Registry validation is fail-fast, so "1 fail" understated the job by 12×. Doctor named one secret; twelve needed editing, across three files. A user budgeting effort from that number will be wrong. Collecting every invalid backend_mappings key in one pass would also kill the five downstream skipped (config or manifests unavailable) rows, which are cascade noise from a single bad key rather than five independent problems.
3. resource sample secret-source demos the one backend that never needs the manifest. The sample shows backend: name: env-var, while concept-secrets says env-var and prompt are synthesized and "the simple case needs no source manifest." So the example is the case you'd never write. Defaulting it to onepassword — with account shown — would have handed me my answer directly.
4. One concept, three verbs. secret-source says Agentworks publishes env-var/prompt; concept-secrets says it synthesizes them; doctor's row says synthesized default. Worth picking one.
5. On the rename: explain is the better name, and the strings need to travel with it. describe-kind is currently embedded in at least guide concept-secrets ("Use agw resource describe-kind secret-backend/NAME"), the secret-source kind body, and the secret-backend/onepassword body — all as literal copy-paste instructions, so a rename without them leaves the docs telling users to run a command that no longer exists. Note also that explain will sit next to describe (instances) and describe-kind (kinds) — explain/describe is a clearer split than describe/describe-kind, which is a real improvement, not just a rename.
6. concept-migration is written for an agent, not a human. ~20k characters, structured as action records with Consent boundary: / Expected state: / If refused:. Genuinely good for me; punishing to skim. Since --agent already exists as a flag (guide concept-onboarding --agent), the action-record format seems like it belongs behind it, with a shorter prose default.
```

I honestly noticed this as well. The guide content is full of agent instructions. I thought we were
only going to add that if --agent was specified. And even then, this feels super excessive.

### Additional Operator Feedback

1. `agw guide` is a monster. Listing every single resource seems insane. I think we should list
   kinds and maybe counts within them and then instruct the user to run `agw guide <kind>` to see
   the resources of that kind. This would make it much more manageable.
2. Many of the resource descriptions start with "Configuration description (plain text; not
   guidance):". Wtf? Just use the description.
