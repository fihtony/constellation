# Android Compose Delivery Playbook

## Purpose

This playbook guides the Android Agent when implementing Android tasks using
Kotlin, Jetpack Compose, XML layouts, and the Gradle build system.

---

## 1. Project Inspection

Before writing any code, always read the existing project structure:

1. `list_local_dir` the root of the cloned repository to locate build files.
2. Read `app/build.gradle.kts` (or `app/build.gradle`) to understand:
   - `compileSdk`, `targetSdk`, `minSdk`
   - Kotlin version and compiler options
   - Existing dependencies (Compose, Hilt, Coroutines, etc.)
3. Read `gradle/libs.versions.toml` if it exists (version catalog pattern).
4. Read `settings.gradle.kts` for module structure.
5. Read the main `Application` class, `MainActivity`, and navigation graph (if present).
6. Identify the existing architecture pattern (MVVM, MVI, MVP) before adding code.

---

## 2. Dependency Management

### Version Catalog Pattern (libs.versions.toml)
When the project uses a version catalog:
- Add new versions to `[versions]` section
- Add library aliases to `[libraries]` section
- Reference via `libs.<alias>` in `build.gradle.kts`

### Common Additions

| Feature | Dependency |
|---------|-----------|
| Jetpack Compose UI | `androidx.compose.ui:ui` |
| Compose Material3 | `androidx.compose.material3:material3` |
| Compose Preview | `androidx.compose.ui:ui-tooling-preview` |
| Compose ViewModel | `androidx.lifecycle:lifecycle-viewmodel-compose` |
| RecyclerView | `androidx.recyclerview:recyclerview:1.3.2` |
| Fragment KTX | `androidx.fragment:fragment-ktx:1.6.2` |
| Fragment testing | `androidx.fragment:fragment-testing:1.6.2` (test) + `fragment-testing-manifest:1.6.2` (debug) |
| Robolectric | `org.robolectric:robolectric:4.12.2` |
| AndroidX Test | `androidx.test:core:1.5.0`, `androidx.test.ext:junit:1.1.5` |
| Hilt | `com.google.dagger:hilt-android` + `hilt-android-compiler` |
| Coroutines | `org.jetbrains.kotlinx:kotlinx-coroutines-android` |
| ViewModel | `androidx.lifecycle:lifecycle-viewmodel-ktx` |

---

## 3. Kotlin/Android Pitfalls

### NEVER Use Synthetic Imports
`kotlinx.android.synthetic.*` was deprecated in Kotlin 1.8 and **removed in Kotlin 2.0**.

**Always use instead:**
- **ViewBinding** (recommended): enable `viewBinding = true` in `buildFeatures {}`, inflate in `onCreateView`, clear in `onDestroyView`
- **Direct view lookup**: `view.findViewById<ViewType>(R.id.xxx)`

### Fragment Lifecycle
```kotlin
private var _binding: FragmentXxxBinding? = null
private val binding get() = _binding!!

override fun onCreateView(...): View {
    _binding = FragmentXxxBinding.inflate(inflater, container, false)
    return binding.root
}

override fun onDestroyView() {
    super.onDestroyView()
    _binding = null
}
```

### Compose vs XML
- Use **Jetpack Compose** when `compose = true` is in `buildFeatures` or the task explicitly requests it
- Use **Fragment + RecyclerView + XML** for list/scrollable screens otherwise
- Do NOT mix paradigms unless the existing codebase already does so

---

## 4. Testing Rules

### Unit Tests (app/src/test/)

Unit tests that need Android APIs MUST use **Robolectric**:
```kotlin
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class MyFragmentTest {
    // ...
}
```

**NEVER use `@RunWith(AndroidJUnit4::class)` in `app/src/test/`** — there is no emulator.

Required configuration in `app/build.gradle.kts`:
```kotlin
android {
    testOptions {
        unitTests {
            isIncludeAndroidResources = true
        }
    }
}
```

### Fragment Testing with `launchFragmentInContainer`

If any test uses `launchFragmentInContainer`, both of these are REQUIRED:
```kotlin
// build.gradle.kts
testImplementation(libs.androidx.fragment.testing)
debugImplementation(libs.androidx.fragment.testing.manifest)  // ← MANDATORY
```

Without `debugImplementation(fragment-testing-manifest)`, tests compile but throw
`NoClassDefFoundError` at runtime.

### Instrumentation Tests (app/src/androidTest/)
Use `@RunWith(AndroidJUnit4::class)` and Espresso. Mark with `@Ignore` if no device is available.

---

## 5. Gradle Validation

### CI-Friendly Build Command
```bash
./gradlew testDebugUnitTest \
  --no-daemon \
  --max-workers=1 \
  -Pkotlin.compiler.execution.strategy=in-process \
  -Dkotlin.daemon.enabled=false \
  -Dorg.gradle.vfs.watch=false \
  --console=plain
```

### Stale Lock Cleanup (before each build in containerized environments)
```bash
find ~/.gradle/caches -name "journal-1.lock" -delete 2>/dev/null || true
```

### GRADLE_USER_HOME Properties (for container builds)
Ensure `$GRADLE_USER_HOME/gradle.properties` contains:
```properties
org.gradle.daemon=false
org.gradle.workers.max=1
kotlin.compiler.execution.strategy=in-process
kotlin.daemon.enabled=false
android.dexBuilderWorkerCount=1
```

### JVM Memory for Kotlin 2.0 + Compose IR
```bash
export ANDROID_GRADLE_JVM_ARGS="-Xmx2g -Dfile.encoding=UTF-8"
```

---

## 6. Pre-Completion Verification Checklist

Run each check and fix violations BEFORE declaring the task complete:

```bash
# 1. Wrong runner in unit tests (must return 0 results)
grep -r "AndroidJUnit4" app/src/test/

# 2. Robolectric present
grep -r "robolectric" app/build.gradle.kts

# 3. Fragment testing manifest (if launchFragmentInContainer is used)
grep -r "launchFragmentInContainer" app/src/test/
grep "androidx.fragment.testing.manifest" app/build.gradle.kts

# 4. No synthetic imports (must return 0 results)
grep -r "kotlinx.android.synthetic" app/src/

# 5. Android resources available to unit tests
grep "isIncludeAndroidResources" app/build.gradle.kts
```

---

## 7. Navigation and Entry Path

If the task says the screen appears after an existing user action (bottom-nav,
menu item, button, deep link), the entry path MUST be wired:

1. Locate the host `Activity` (e.g. `MainActivity`)
2. Find the navigation component (`NavHostFragment`, `BottomNavigationView`, etc.)
3. Add the new `<fragment>` destination to the navigation graph XML
4. Add the menu item / bottom-nav entry pointing to that destination
5. Verify the route is reachable by inspecting host code

**A standalone Fragment not reachable from any live navigation is incomplete.**

---

## 8. Design Evidence

When a Figma or Stitch design reference is provided:

1. Implement the screen to match the visual spec (layout, colors, typography)
2. Create `docs/evidence/self-review.md` that maps each UI element to the design spec
3. If screenshot capture is possible in the environment, save real screenshots to `docs/evidence/`
4. **Never create placeholder or empty image files** — leave required image paths absent if capture is blocked and document the blocker in `self-review.md`
5. Real image files must be non-zero bytes and pass `file` format detection

---

## 9. Scope Discipline

- Keep generated files inside the cloned repository
- Output only files directly required by the task
- Do NOT add CI/CD pipelines, Firebase configs, or unrelated boilerplate
- Do NOT modify unrelated source files
- Do NOT invent features not described in the task

---

## 10. Common Build Errors and Fixes

| Error | Root Cause | Fix |
|-------|-----------|-----|
| `NoClassDefFoundError` in fragment test | Missing `debugImplementation(fragment-testing-manifest)` | Add to `app/build.gradle.kts` |
| `unresolved reference` for library alias | Alias mismatch between catalog and build file | Sync key names (dots→underscores in Kotlin DSL) |
| Robolectric `SDK 28 not available` | Missing `@Config(sdk = [28])` | Add to every `@RunWith(RobolectricTestRunner)` class |
| `Unresolved reference: R` in unit test | Missing `isIncludeAndroidResources = true` | Add to `testOptions { unitTests { } }` |
| `Kotlin daemon connection failed` | Daemon not disabled | Add `-Dkotlin.daemon.enabled=false` |
| Gradle lock file conflict | Stale lock from killed container | Delete `caches/journal-1/journal-1.lock` before build |
