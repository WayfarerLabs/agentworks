---
name: sdd
description:
  "Spec-Driven Development workflow for significant development efforts"
targets: ["*"]
---

# Spec-Driven Development

For significant development efforts, we use spec-driven development (SDD) to
guide the development of the project.

Note that it is ok to skip SDD for small, simple changes.

## Feature Directory

The specs and related artifacts for each development effort are stored in a
subdir of the `/docs/sdd` directory called the "feature directory". The feature
directory name should start with `<YYYY>-<MM>-<DD>-` (representing the start
date) to easily identify the feature age and order of creation.

## Artifacts

Within the feature directory, we store the following artifacts:

- `frd.md` (Functional Requirements Document): A markdown file that contains the
  functional specification for the development effort. This should be focused on
  the functional requirements (business requirements, user stories, personas,
  etc.) and avoid technical details unless they are fundamental to the business
  requirements.
- `hla.md` (High-Level Architecture): A markdown file that describes the
  high-level architecture. This should specify overall topology, components,
  major interfaces, suggested/required tech stack choices, integration with the
  Gruntweave platform, etc. This generally shouldn't have low-level details such
  as code samples, specific versions, etc. unless they are critical to the
  architecture. Pseudocode for critical algorithms is allowed when appropriate.
- `plan.md`: A markdown file that detailed technical plan for the development
  effort complete with checkboxes for tracking the work as it is completed. Plan
  should include specific definitions of done that can be used to objectively
  determine completeness. If phasing is appropriate, the phases should be
  described here, either within a single plan file or as multiple plan files.
  This should provide checkboxes and the work should be tracked here.

Most efforts will involve one or more "low-level design" (LLD) documents that
provide more detailed technical specifications for specific components,
algorithms, interfaces, etc. These should be stored in the feature directory
with descriptive names (e.g. `something-lld.md`). As a general rule, the plan
should include generating any missing low-level design documents as part of the
work, and they should be linked to from the plan.

Additional artifacts related to the development effort (e.g. background
research, UI concepts, API specifications, data models, diagrams, etc.) may be
stored in the feature directory as needed.

## Lockfile

When work on the SDD is done, a `locked.md` file should be created in the
feature directory. This file should have a date and summarize the final state of
everything. Once a lockfile is created, the SDD artifacts are considered
"locked" and should not be modified except in exceptional circumstances. If
changes are needed, the lockfile should be updated with a date and summary of
the changes.

## Branching Model

Work driven via SDD should be done in one or more feature branches. The general
pattern is:

1. Create an initial feature branch. This should generally relate to the naming
   of the feature directory, although additional info (e.g. phase) is allowed.
2. Create the SDD feature directory and artifacts in this branch.
3. If pre-implementation review is needed, publish a draft PR to allow others to
   review and provided feedback on the SDD artifacts.
4. The first push of work should use that existing branch.
5. SDD artifacts will naturally get merged with the work itself.
6. If additional work remains per the specs, it should be done in additional
   feature branches, tracking the work via the existing plan files. It is
   entirely permissible (encouraged) to modify the artifacts if the
   requirements, architecture, plan, etc. has changed.
7. Alternatively, if future work superseded unfinished work in an existing SDD
   feature directory, that future work should update the existing SDD specs to
   indicate that the remaining work is superseded.
