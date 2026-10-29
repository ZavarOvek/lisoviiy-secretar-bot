FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-ukr \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home app \
    && mkdir /app \
    && chown app:app /app
WORKDIR /app

# Dependencies first, so that code changes do not reinstall them.
# The stub package is replaced by the real one on COPY; the editable install points at src/.
COPY pyproject.toml README.md ./
RUN mkdir -p src/lisovyi \
    && touch src/lisovyi/__init__.py \
    && pip install -e ".[dev]"

COPY --chown=app:app . .
USER app

CMD ["python", "-m", "lisovyi"]
