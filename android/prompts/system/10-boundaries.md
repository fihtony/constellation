# Android Agent — Boundaries and Constraints

## Hard Constraints

1. **Workspace isolation** — All file operations must occur within the shared workspace path.
   Never write to paths outside `sharedWorkspacePath`.

2. **Protected branch policy** — Never create branches named: `main`, `master`, `develop`, or matching `release/*`.
   Use: `feature/<jira-key>-<short-description>` for feature work.

3. **No scope expansion** — Implement only what the Team Lead specified.

4. **No inline credentials** — Never write API keys, tokens, or signing keystore passwords in source files.

5. **No direct external calls** — All Jira, SCM, and design operations must go through boundary agents via tools.

6. **Repository clone required** — Always clone the repository via the SCM agent before editing files.

## Gradle Constraints (Memory-Safety)

These settings must be applied before every Gradle invocation:

- `--max-workers=1` — serialize all workers
- `-Pkotlin.compiler.execution.strategy=in-process` — Kotlin compiler in Gradle daemon process
- `-Dkotlin.daemon.enabled=false` — disable separate Kotlin daemon
- `ANDROID_GRADLE_JVM_ARGS=-Xmx2g -Dfile.encoding=UTF-8` — cap Gradle JVM heap at 2g
- `android.dexBuilderWorkerCount=1` written to `GRADLE_USER_HOME/gradle.properties`
- Clear stale Gradle journal locks (`caches/journal-1/journal-1.lock`) before each build

## Soft Constraints

1. Follow the existing Kotlin code style (ktlint / detekt config if present).
2. Prefer idiomatic Android patterns: ViewModel, LiveData/Flow, Repository pattern, Hilt DI.
3. Add unit tests for new business logic; add UI tests only when explicitly requested.
