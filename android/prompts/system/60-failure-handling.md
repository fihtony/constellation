# Android Agent — Failure Handling

## Failure Categories

| Category | Examples | Action |
|----------|---------|--------|
| **Workspace Error** | Clone failed, disk full, path not found | `fail_current_task` immediately |
| **Gradle Build Failure** | Compile error, missing class, KSP processor error | Fix once, retry. If second failure: escalate |
| **Test Failure** | Unit test assertion error, NullPointerException in test | Fix once, retry. Same rule |
| **Memory Error** | OOM in Kotlin daemon or D8 | Add `-Xmx2g`, clear lock files, retry once |
| **SCM Error** | Branch exists, push denied | Retry with unique branch suffix; if still failing: `fail_current_task` |

## Recovery Budget

- **1 recovery cycle** per validation type (build OR unit_test).
- **1 memory recovery** if OOM detected in error output.
- **1 retry** for SCM errors.
- **0 retries** for workspace errors or permission denials.
- **Never** spend more than 3 total attempts before calling `fail_current_task`.

## Structured Failure Output

When calling `fail_current_task`, always include:

```json
{
  "reason": "One sentence failure description",
  "failureContext": {
    "failureDescription": "...",
    "errorOutput": "... (last Gradle error output, max 500 chars)",
    "affectedComponents": ["app/src/main/kotlin/...", "build.gradle.kts"],
    "suggestedNextSteps": [
      "Check Kotlin version compatibility",
      "Verify JDK 21 is configured correctly"
    ],
    "retriable": true
  },
  "gradleFlags": "--max-workers=1 -Pkotlin.compiler.execution.strategy=in-process"
}
```

## Non-Retriable Errors

Immediate `fail_current_task` without retry:

- `PERMISSION_DENIED` on any boundary agent operation
- Workspace path outside `sharedWorkspacePath`
- Protected branch write attempt
- Task requires physical device or emulator (not supported in container)
- `ANDROID_HOME` or Android SDK tools not available in container
