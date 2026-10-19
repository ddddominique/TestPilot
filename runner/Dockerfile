FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

RUN pip install --no-cache-dir pytest==8.3.5 \
    && useradd --uid 65532 --no-create-home --shell /usr/sbin/nologin testpilot

COPY pytest_runner.py /opt/testpilot/runner.py
RUN chmod 0555 /opt/testpilot/runner.py

USER 65532:65532
ENTRYPOINT ["python", "-I", "-B", "/opt/testpilot/runner.py"]
