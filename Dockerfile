FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_home mcp_home
ENV MCP_HOME_DB=/data/home.db PORT=8000
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "-m", "mcp_home.server"]
