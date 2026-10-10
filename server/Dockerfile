# Built on the selfhost host by compose (see ../deploy/selfhost). No secrets in the image.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes -o /tmp/req.txt && pip install --no-cache-dir -r /tmp/req.txt && rm /tmp/req.txt
COPY sb_server ./sb_server
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
ENTRYPOINT ["python", "-m", "sb_server"]
CMD ["serve"]
