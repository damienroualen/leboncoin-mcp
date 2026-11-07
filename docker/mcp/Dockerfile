FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Playwright only drives the remote Chrome over CDP, so no browser is installed here.
RUN useradd --create-home app
USER app

CMD ["python", "-m", "leboncoin_mcp.server"]
