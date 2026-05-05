FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# Install system dependencies (tini for signal handling, git for SCM tools)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates tini git curl unzip openjdk-21-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

RUN java_home_dir="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")" \
    && ln -s "$java_home_dir" /usr/lib/jvm/default-java

RUN mkdir -p /opt/android-sdk

# Import any corporate CA certificates placed in certs/ (supports .crt and .pem)
COPY certs/ /usr/local/share/ca-certificates/
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates

RUN mkdir -p /opt/android-sdk/cmdline-tools \
    && curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -o /tmp/android-cmdline-tools.zip \
    && unzip -q /tmp/android-cmdline-tools.zip -d /opt/android-sdk/cmdline-tools \
    && mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest \
    && rm -f /tmp/android-cmdline-tools.zip

RUN pip install --no-cache-dir requests

COPY common/          /app/common/
COPY android/         /app/android/
COPY .github/skills/  /app/.github/skills/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    JAVA_HOME=/usr/lib/jvm/default-java \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    PATH=/usr/lib/jvm/default-java/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH

RUN yes | sdkmanager --sdk_root=/opt/android-sdk --licenses >/dev/null \
    && sdkmanager --sdk_root=/opt/android-sdk \
        "platform-tools" \
        "platforms;android-36" \
        "build-tools;35.0.0"

LABEL constellation.agent_id="android-agent"
LABEL constellation.agent_name="Android Agent"
LABEL constellation.agent_role="execution"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "android/app.py"]
