# Android Agent — Validation Policy

## Required Validation Steps

Every implementation MUST pass both steps before PR creation:

1. **Build** — `run_validation_command(validation_type="build")`
   Equivalent command: `./gradlew assembleDebug --max-workers=1 -Pkotlin.compiler.execution.strategy=in-process -Dkotlin.daemon.enabled=false -Dorg.gradle.daemon=false`

2. **Unit Test** — `run_validation_command(validation_type="unit_test")`
   Equivalent command: `./gradlew testDebugUnitTest --max-workers=1 -Pkotlin.compiler.execution.strategy=in-process -Dkotlin.daemon.enabled=false -Dorg.gradle.daemon=false`

## Pre-Build Checklist

Before running Gradle:

- [ ] Clear journal lock: `rm -f $GRADLE_USER_HOME/caches/journal-1/journal-1.lock`
- [ ] Write `android.dexBuilderWorkerCount=1` to `$GRADLE_USER_HOME/gradle.properties`
- [ ] Confirm `ANDROID_GRADLE_JVM_ARGS=-Xmx2g -Dfile.encoding=UTF-8` is set

## On Validation Failure

1. Read the full Gradle error output.
2. Identify the failing task: `compileDebugKotlin`, `testDebugUnitTest`, `assembleDebug`.
3. Fix the root cause (syntax error, type mismatch, missing import, test assertion).
4. Clear any lock files again.
5. Re-run the same Gradle task.
6. If still failing: call `summarize_failure_context` + `fail_current_task`.

## Evidence Requirements

Before `complete_current_task`:
- Gradle build log captured (last 200 lines).
- Test result XML/HTML paths referenced in evidence.
- PR URL and branch name in artifact metadata.
