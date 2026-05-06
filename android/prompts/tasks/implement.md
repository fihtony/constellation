# Android Implementation Task

You are implementing changes in an Android project using Kotlin/Jetpack Compose.

## Your Workflow

1. **Analyze** requirements and design context.
2. **Clone** the target repository.
3. **Create** a feature branch.
4. **Implement** the required changes.
5. **Build** with Gradle using CI-friendly flags.
6. **Test** with unit tests; fix failures with bounded recovery.
7. **Push** and create a Pull Request.
8. **Complete** with evidence artifacts.

## Build Configuration

- Use `--max-workers=1` for container environments.
- Clear stale Gradle lock files before builds.
- Recovery loop: max 3 attempts for build/test failures.
