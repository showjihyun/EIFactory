# Phase 0~2 devcontainer 이미지. Phase 3 Docker 직접 구성의 출발점으로 재사용한다 (기획안 9장).
FROM node:24-bookworm

ARG UV_VERSION=0.11.2
ARG INSTALL_CLAUDE_CODE=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends make git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3 /usr/local/bin/python

COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /uvx /usr/local/bin/

RUN if [ "$INSTALL_CLAUDE_CODE" = "true" ]; then npm install -g @anthropic-ai/claude-code; fi

# node_modules는 이름 있는 볼륨으로 마운트한다. 이미지에 경로를 미리 만들어 두면
# 처음 마운트할 때 소유자(node)가 볼륨으로 복사된다.
RUN mkdir -p /workspaces/bookmark-factory/frontend/node_modules /home/node/.venvs \
    && chown -R node:node /workspaces /home/node/.venvs

# 호스트(Windows)의 backend/.venv와 섞이지 않도록 가상환경을 작업 폴더 밖에 둔다.
ENV UV_PROJECT_ENVIRONMENT=/home/node/.venvs/backend \
    UV_LINK_MODE=copy \
    PYTHON=python3

USER node
WORKDIR /workspaces/bookmark-factory
