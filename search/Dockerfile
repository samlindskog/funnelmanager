# Build context: repository root (needed for libs/fm_runtime).
FROM python:3.13-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY libs/fm_runtime ./libs/fm_runtime
COPY search/requirements.txt ./requirements.txt
RUN pip install --prefix=/install ./libs/fm_runtime -r requirements.txt

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --user-group --no-create-home app
COPY --from=build /install /usr/local
WORKDIR /srv
COPY search/app ./app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
