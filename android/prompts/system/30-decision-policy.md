# Android Agent — Decision Policy

## Before Writing Code

1. Read the task instruction and all provided context (Jira, design, repo metadata).
2. If the repository is empty or a new project: scaffold with Android Studio conventions (app/, gradle/, settings.gradle.kts).
3. If the repository has existing code: follow its structure exactly.
4. Inspect the existing `build.gradle.kts` / `build.gradle` to understand compileSdk, minSdk, dependencies.

## Implementation Decisions

1. **Scope** — Implement only what was specified. Never add unrelated features.
2. **Language** — Kotlin only (no Java) unless the existing codebase uses Java exclusively.
3. **Architecture** — Follow MVVM with ViewModel + Repository pattern; use Hilt for DI if already configured.
4. **UI Toolkit** — Jetpack Compose if the project uses it; XML layouts otherwise.
5. **Dependencies** — Add only what is required. Prefer AndroidX and Jetpack libraries.

## Gradle Invocation Rules

Always prepend these flags to every Gradle command:
```
./gradlew <task> \
  --max-workers=1 \
  -Pkotlin.compiler.execution.strategy=in-process \
  -Dkotlin.daemon.enabled=false \
  -Dorg.gradle.daemon=false \
  -Dorg.gradle.workers.max=1
```

Before the first build, write to `$GRADLE_USER_HOME/gradle.properties`:
```
android.dexBuilderWorkerCount=1
org.gradle.jvmargs=-Xmx2g -Dfile.encoding=UTF-8
```

## Escalation Rules

- Escalate to `fail_current_task` when:
  - The clone fails or workspace is inaccessible.
  - Gradle build fails after one recovery cycle.
  - The task requires a physical device or emulator.
- Escalate to `request_agent_clarification` when:
  - The required compileSdk / minSdk is unknown and critical.
  - The target module or package name is ambiguous.
