FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 raven \
    && mkdir /data && chown raven:raven /data
COPY requirements-postgres.txt requirements-agents.txt requirements-teams.txt requirements-semantic.txt requirements-hosts.txt ./
RUN pip install --no-cache-dir -r requirements-postgres.txt
ARG INSTALL_SEMANTIC=true
ENV FASTEMBED_CACHE_PATH=/opt/raven-models
RUN if [ "$INSTALL_SEMANTIC" = true ]; then \
      pip install --no-cache-dir -r requirements-semantic.txt \
      && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/opt/raven-models')" \
      && chmod -R a+rX /opt/raven-models; fi
ARG INSTALL_TEAMS=false
RUN if [ "$INSTALL_TEAMS" = true ]; then pip install --no-cache-dir -r requirements-teams.txt; fi
ARG INSTALL_AGENTS=false
RUN if [ "$INSTALL_AGENTS" = true ]; then pip install --no-cache-dir -r requirements-agents.txt; fi
ARG INSTALL_HOSTS=false
RUN if [ "$INSTALL_HOSTS" = true ]; then pip install --no-cache-dir -r requirements-hosts.txt; fi
COPY --chown=raven:raven hosts ./hosts
COPY --chown=raven:raven raven ./raven
COPY --chown=raven:raven web ./web
COPY --chown=raven:raven fixtures ./fixtures
COPY --chown=raven:raven tests ./tests
COPY --chown=raven:raven scripts ./scripts
COPY --chown=raven:raven bench ./bench
COPY --chown=raven:raven evals ./evals
COPY --chown=raven:raven raven_mcp.py ./
COPY --chown=raven:raven LICENSE ./
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/raven-entrypoint
USER raven
EXPOSE 7333
ENTRYPOINT ["raven-entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--port", "7333", "--auth", "on"]
