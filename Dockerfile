# Hosted demo image (Hugging Face Spaces, or any Docker host): sandbox + dashboard in one container.
# The worker's browser reaches the sandbox on 127.0.0.1:8001 inside the container; only the dashboard is exposed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

# Hugging Face runs containers as uid 1000; the app writes runs/, sandbox/data/ and the quota ledger.
RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
COPY --chown=user . .

# No display in a container: always headless, and the dashboard's "show browser" option is ignored.
ENV WORKER_HEADLESS=1 \
    WORKER_FORCE_HEADLESS=1

EXPOSE 7860
CMD ["python", "start.py", "--no-browser", "--host", "0.0.0.0", "--port", "7860"]
