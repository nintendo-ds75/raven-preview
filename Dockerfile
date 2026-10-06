FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bridge \
    && mkdir /data && chown bridge:bridge /data
COPY requirements-postgres.txt requirements-agents.txt requirements-teams.txt ./
RUN pip install --no-cache-dir -r requirements-postgres.txt
ARG INSTALL_TEAMS=false
RUN if [ "$INSTALL_TEAMS" = true ]; then pip install --no-cache-dir -r requirements-teams.txt; fi
ARG INSTALL_AGENTS=false
RUN if [ "$INSTALL_AGENTS" = true ]; then pip install --no-cache-dir -r requirements-agents.txt; fi
COPY --chown=bridge:bridge bridge ./bridge
COPY --chown=bridge:bridge web ./web
COPY --chown=bridge:bridge fixtures ./fixtures
COPY --chown=bridge:bridge tests ./tests
COPY --chown=bridge:bridge scripts ./scripts
COPY --chown=bridge:bridge bench ./bench
COPY --chown=bridge:bridge evals ./evals
COPY --chown=bridge:bridge bridge_mcp.py ./
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/bridge-entrypoint
USER bridge
EXPOSE 7333
ENTRYPOINT ["bridge-entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--port", "7333", "--auth", "on"]
