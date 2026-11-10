FROM python:3.13-slim

# Playwright's bundled Node needs libatomic (missing from the slim image since Playwright 1.65).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Playwright only drives the remote Chrome over CDP, so no browser is installed here.
RUN useradd --create-home app
USER app

CMD ["python", "-m", "leboncoin_mcp.server"]
