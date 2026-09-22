FROM python:3.11-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --target /app .

FROM gcr.io/distroless/python3-debian12:nonroot
COPY --from=build /app /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
USER nonroot
ENTRYPOINT ["python3", "-m", "indexwright.cli"]
