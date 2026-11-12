# Only TRIAGE-1's own code is compiled. Python and the open-source libraries ship unchanged, as on Windows.
#   docker build -t triage1:1.0.0 .
FROM python:3.13-slim-trixie AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ ccache patchelf make && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN pip install -q nuitka ordered-set zstandard
COPY app app
RUN python -m nuitka --mode=module --include-package=app --no-pyi-file --assume-yes-for-downloads --output-dir=/out --remove-output app

FROM python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin triage \
    && mkdir /data && chown triage:triage /data && chmod 700 /data
WORKDIR /opt/triage1
COPY requirements-prod.txt packaging/constraints.txt /tmp/
RUN pip install -q --no-cache-dir -r /tmp/requirements-prod.txt -c /tmp/constraints.txt && rm /tmp/requirements-prod.txt /tmp/constraints.txt
COPY --from=build /out/ /opt/triage1/
COPY packaging/windows/launch.py /opt/triage1/launch.py
COPY static /opt/triage1/static
COPY skills /opt/triage1/skills
COPY db/schema.sql /opt/triage1/db/schema.sql
ENV TRIAGE_HOME=/data TRIAGE_HOST=0.0.0.0 TRIAGE_PORT=8000
USER triage
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
ENTRYPOINT ["python", "/opt/triage1/launch.py"]
CMD ["serve"]
