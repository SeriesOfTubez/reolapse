# Alpine base: far smaller and a much lower CVE surface than debian-slim for a
# workload that just needs Python + ffmpeg.
FROM python:3.12-alpine

# ffmpeg for encoding, tini for correct signal handling as PID 1
RUN apk add --no-cache ffmpeg tini

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY VERSION ./
COPY webapp ./webapp

# Run as an unprivileged user; /app/data is where a fresh volume mounts.
RUN adduser -D app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# Health check targets the web role. The capture/scheduler services don't
# serve HTTP, so docker-compose disables the check for them.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/videos', timeout=5)" || exit 1

# Default process; docker-compose overrides `command` per service.
ENTRYPOINT ["tini", "--"]
CMD ["python", "capture.py", "--loop"]
