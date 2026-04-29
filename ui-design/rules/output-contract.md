# UI Design Agent Output Contract

## Required Outputs

- Explicit design source and target reference.
- Structured summary of the relevant frame, node, screen, or component.
- Supporting links or file paths for downloaded assets when available.
- Error category for auth, lookup, or ambiguity failures.

## Artifact Expectations

- Preserve canonical identifiers such as file IDs, node IDs, screen IDs, or project names.
- Return asset paths or image references when the workflow requires them.

## Failure Output

- Explain whether the failure was caused by missing target information, provider auth, or ambiguous lookup results.
