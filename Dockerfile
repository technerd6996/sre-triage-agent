# Compiled in the first stage, so the final image holds no Python source.
#   docker build -t triage1:1.0.0 .
FROM python:3.13-slim-trixie AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ ccache patchelf make && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY requirements-prod.txt ./
COPY packaging/constraints.txt packaging/triage1.py packaging/
RUN pip install -q nuitka ordered-set zstandard -r requirements-prod.txt -c packaging/constraints.txt
COPY app app
COPY static static
COPY skills skills
COPY db/schema.sql db/schema.sql
RUN PYTHONPATH=/src python -m nuitka --standalone --assume-yes-for-downloads \
      --output-dir=/out --output-filename=triage1 \
      --include-package=app --include-package=uvicorn --include-package=langgraph --include-package=langchain_openai \
      --include-package=psycopg --include-package=psycopg_pool --include-package=psycopg_binary \
      --nofollow-import-to=torch --nofollow-import-to=sentence_transformers --nofollow-import-to=transformers \
      --include-data-dir=static=static --include-data-dir=skills=skills \
      --include-data-files=db/schema.sql=db/schema.sql \
      '--noinclude-data-files=static/v1/*' \
      packaging/triage1.py

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin triage \
    && mkdir /data && chown triage:triage /data && chmod 700 /data
COPY --from=build /out/triage1.dist /opt/triage1
ENV TRIAGE_HOME=/data TRIAGE_HOST=0.0.0.0 TRIAGE_PORT=8000
USER triage
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
ENTRYPOINT ["/opt/triage1/triage1"]
CMD ["serve"]
