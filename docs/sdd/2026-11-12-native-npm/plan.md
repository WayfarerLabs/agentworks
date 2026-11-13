# Plan

Delivery vehicle: one feature branch, `feat/native-npm`, containing one coherent
feature. The session lead owns these artifacts, architecture, and integration.

- [ ] Add validated npm fields to admin and agent declarations and resolved state;
      verify inheritance, instance overlays, schema emission, and disabled behavior.
- [ ] Implement a shared native npm runner with per-user prefix, minimum age,
      version checks, strict package engines, and actionable failure handling.
- [ ] Make mise optional at VM scope and arrange both user flows so mise and npm
      precede custom user install commands with their executables on PATH.
- [ ] Update permanent guides, generated field teaching, and affected sample
      configuration to describe actual behavior and runtime provisioning.
- [ ] Verify npm against a local registry with old/new direct and transitive
      releases, explicit pins, reinitialization, and executable invocation.
- [ ] Run appropriate repository checks and independent project, complexity, and
      generic correctness reviews; resolve material findings.
- [ ] Record the final validation evidence and any unavailable VM coverage before
      closeout. Do not claim unperformed checks or create a lockfile early.
