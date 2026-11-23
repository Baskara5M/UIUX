FROM python:3.12-slim

# Show print() output in "docker logs" straight away (otherwise Python buffers it)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# The only package the app needs: the Neo4j driver
RUN pip install --no-cache-dir "neo4j>=5,<6"

# The app: the Python files, the map page, and the data model folder
# (neo4j_source.py checks Neo4j's counts against neo4j-datamodel/build-report.json)
COPY *.py ./
COPY web/ ./web/
COPY neo4j-datamodel/ ./neo4j-datamodel/
COPY acceptance/ ./acceptance/
COPY scenarios/ ./scenarios/

# server.py listens on 127.0.0.1, which Docker's port mapping can't reach.
# Inside the container it must listen on 0.0.0.0. Temporary fix until the address is a setting:
RUN sed -i 's/Server(("127.0.0.1", PORT)/Server(("0.0.0.0", PORT)/' server.py \
 && grep -q 'Server(("0.0.0.0", PORT)' server.py

# Run as a normal user, not root
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8100

# "healthy" once the API answers (the graph is loaded and the server is listening)
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/api/ports', timeout=4)"

CMD ["python", "server.py"]