# Built on the selfhost host by compose (see ../deploy/selfhost). No secrets in the image.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Hashes from uv.lock are enforced; nothing outside the lock is installed.
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/req.txt \
 && pip install --no-cache-dir --require-hashes --no-deps -r /tmp/req.txt && rm /tmp/req.txt
COPY sb_server ./sb_server
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SB_HOST=0.0.0.0
EXPOSE 8000
ENTRYPOINT ["python", "-m", "sb_server"]
CMD ["serve"]
