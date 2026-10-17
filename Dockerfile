FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY casa_mcp casa_mcp
ENV CASA_DB=/data/casa.db PORT=8000
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "-m", "casa_mcp.server"]
