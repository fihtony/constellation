# Android build and A2A agent image
# Provides: Python 3.12 (A2A agent runtime) + JDK 21 + Android SDK (compileSdk 36)
# Platform: linux/amd64 (required for Android SDK toolchain compatibility on Apple Silicon hosts)

FROM python:3.12-slim

WORKDIR /app

# System dependencies:
#   tini        — proper PID 1 / signal handling in containers
#   git         — SCM operations in agent workflows
#   curl+unzip  — Android SDK command-line tools download
#   openjdk-21-jdk-headless — required by Gradle 8.x and Android SDK tools
#   locales     — UTF-8 locale for consistent build output encoding
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        git \
        curl \
        unzip \
        openjdk-21-jdk-headless \
        locales \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# JDK environment — explicit amd64 path (this image always runs linux/amd64)
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

# Optional: import extra CA certificates (e.g. corporate TLS inspection proxy certs).
# Place .crt or .pem files in the certs/ directory at the repo root before building.
# If certs/ contains only .gitkeep or is empty, this step is a harmless no-op.
COPY certs/ /usr/local/share/ca-certificates/
# 1. Convert any .pem files to .crt so update-ca-certificates picks them up.
# 2. Update the OS-level trust store.
# 3. Import every .crt into the JDK keystore so Gradle / Java HTTPS clients also trust them.
#    keytool exits 1 if the alias already exists; || true makes that non-fatal.
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates \
    && find /usr/local/share/ca-certificates/ -name "*.crt" ! -name "ca-certificates.crt" | while read cert; do \
         alias=$(basename "$cert" .crt); \
         keytool -noprompt -importcert -trustcacerts \
                 -file "$cert" -alias "$alias" \
                 -keystore "${JAVA_HOME}/lib/security/cacerts" \
                 -storepass changeit 2>/dev/null || true; \
       done

# Android SDK — download and install command-line tools
RUN mkdir -p /opt/android-sdk/cmdline-tools \
    && curl -fsSL \
        https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
        -o /tmp/android-cmdline-tools.zip \
    && unzip -q /tmp/android-cmdline-tools.zip -d /opt/android-sdk/cmdline-tools \
    && mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest \
    && rm -f /tmp/android-cmdline-tools.zip

ENV ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    PATH=/usr/lib/jvm/java-21-openjdk-amd64/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH

# Accept SDK licenses and install SDK components.
#   platforms;android-36  — compile target (compileSdk 36 / Android 16)
#   build-tools;35.0.0    — build tools baseline for AGP 8.x
# Note: `yes | sdkmanager` causes SIGPIPE when sdkmanager stops reading; disable pipefail
# for just this step (same approach used in standard Android CI images).
RUN set +o pipefail \
    && yes | sdkmanager --sdk_root=/opt/android-sdk --licenses >/dev/null 2>&1 \
    && sdkmanager --sdk_root=/opt/android-sdk \
        "platform-tools" \
        "platforms;android-36" \
        "build-tools;35.0.0" \
    && chmod -R a+rX /opt/android-sdk

# Python dependencies for the A2A agent HTTP server
RUN pip install --no-cache-dir requests

# Agent source code
COPY common/          /app/common/
COPY android/         /app/android/
COPY .github/skills/  /app/.github/skills/

# Python environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Gradle CI defaults — applied automatically to every ./gradlew invocation in this container.
# These settings disable the daemon and cap memory to keep builds stable on emulated amd64.
#   ANDROID_GRADLE_JVM_ARGS — read by android/app.py _android_gradle_base_args()
#   ANDROID_GRADLE_MAX_WORKERS — read by android/app.py _android_gradle_base_args()
#   GRADLE_OPTS — parsed by the Gradle launcher itself (--no-daemon, vfs.watch off)
ENV ANDROID_GRADLE_JVM_ARGS="-Xmx1024m -Dfile.encoding=UTF-8" \
    ANDROID_GRADLE_MAX_WORKERS="1" \
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.workers.max=1 -Dorg.gradle.vfs.watch=false -Dkotlin.daemon.enabled=false"

LABEL constellation.agent_id="android-agent"
LABEL constellation.agent_name="Android Agent"
LABEL constellation.agent_role="execution"

# Run as non-root (OWASP A05) — create user after installs so /app ownership is correct
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "android/app.py"]
