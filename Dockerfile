FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
COPY scripts ./scripts
COPY public ./public
RUN npm run build

FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TOUCHLINE_SERVER_MODE=true TOUCHLINE_DATA_DIR=/var/lib/touchline PORT=8765
WORKDIR /app
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock.txt && useradd --create-home --uid 10001 touchline && mkdir -p /var/lib/touchline && chown touchline:touchline /var/lib/touchline
COPY server ./server
COPY agent_skills ./agent_skills
COPY public ./public
COPY --from=frontend /build/dist ./dist
USER touchline
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/api/health',timeout=4)"
CMD ["python", "-m", "server.serve"]
