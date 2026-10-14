# Digests observed from successful registry pulls during T05 development.
FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS frontend
WORKDIR /opt/frontend
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund --maxsockets=5

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
COPY requirements.txt /opt/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/requirements.txt
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend /opt/frontend /opt/frontend
COPY runner.py profiles.py structural.cjs /opt/verifier/
COPY pytest_evidence.py /usr/local/lib/python3.12/site-packages/verifier_pytest.py
ENV PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/python3.12", "-I", "/opt/verifier/runner.py"]
CMD ["idle"]
