FROM python:3.10.21-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://mirrors.nju.edu.cn/pytorch/whl/cpu
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=8

WORKDIR /app
RUN mkdir -p /app/log

COPY requirements.txt .
# Keep the CPU wheel pinned, and avoid querying its index for every ordinary package.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    sed -n '/^torch==/p' requirements.txt > /tmp/requirements-torch.txt \
    && test -s /tmp/requirements-torch.txt \
    && python -m pip install --no-deps --index-url "$TORCH_INDEX_URL" \
       --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" \
       -r /tmp/requirements-torch.txt

# This list retains torch's exact pin so pip also installs its missing dependencies.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    sed '/^--extra-index-url[[:space:]]/d' requirements.txt > /tmp/requirements-pypi.txt \
    && python -m pip install --index-url "$PIP_INDEX_URL" \
       --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" \
       -r /tmp/requirements-pypi.txt \
    && python -m pip check

COPY ics_agent ./ics_agent
COPY reminder ./reminder
COPY knowledge_base_in ./knowledge_base_in
COPY support-chat.html ./support-chat.html

EXPOSE 3001

CMD ["python", "-m", "ics_agent.app.serve"]
