# For MCP directories (e.g. Glama) that start the server to list its tools. The tools need
# Windows with NI LabVIEW to do anything; in this Linux image they report that.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["labview-mcp"]
