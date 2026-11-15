# Direct artifact declarations: transition

At main `432766fa` on 2026-11-15, an owning scope selects ordered named bundles through
`artifacts.bundles`. Bundles provide five type maps; captures use the existing v4 format with v2/v3
readers. Artifact identity uses the actual owner, artifact type and key, independently of the
selected bundle name.

The new form is additive:

```yaml
artifacts:
  bundles: [shared-guidance]
  declare:
    hints:
      task:
        text: Check the release notes before making changes.
```

Existing resources require no edits. Bundle-only fingerprints, normalized identities and capture
versions remain stable. An operator may move a single-purpose entry into `declare` and remove its
bundle reference at the same owner. Identical normalized content keeps its input and origin IDs;
selection provenance and declaration freshness can change. The next supported owning setup captures
the current declaration and reconciles through the existing integration operation.

Omission and empty maps inherit. `declare: null` clears inherited direct content, while
`bundles: []` affects only bundle references. Existing named bundle inheritance is unchanged.
Direct content does not add a workspace reinit or automatic descendant refresh operation. Normal
deferral timing and cleanup limitations still apply.

Compatibility tests must compare known bundle-only fingerprint inputs and captured IDs, rather than
only comparing two calls through a newly modified implementation. Inspection and replacement output
must distinguish a direct declaration from a missing or literal bundle name, including when captured
evidence predates the declaration edit.
