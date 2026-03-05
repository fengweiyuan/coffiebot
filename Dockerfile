FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
# 先装 opencv-python-headless 再装其他依赖，防止 rapidocr-onnxruntime 拉入桌面版 opencv
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p coffiebot bridge && touch coffiebot/__init__.py && \
    uv pip install --system --no-cache opencv-python-headless && \
    uv pip install --system --no-cache . && \
    uv pip uninstall --system opencv-python 2>/dev/null; true && \
    uv pip install --system --no-cache --force-reinstall opencv-python-headless && \
    rm -rf coffiebot bridge

# Copy the full source and install
COPY coffiebot/ coffiebot/
COPY bridge/ bridge/
RUN uv pip install --system --no-cache . && \
    uv pip uninstall --system opencv-python 2>/dev/null; true && \
    uv pip install --system --no-cache --force-reinstall opencv-python-headless

# Build the WhatsApp bridge
WORKDIR /app/bridge
RUN npm install && npm run build
WORKDIR /app

# Create config directory
RUN mkdir -p /root/.coffiebot

ENTRYPOINT ["coffiebot"]
CMD ["status"]
