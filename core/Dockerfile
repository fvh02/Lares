FROM python:3.13-slim

# The docker command, so the core can start the room containers (socket mounted from the host)
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker

COPY core/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY core/lares_core /app/lares_core
WORKDIR /app
ENV LARES_DATA=/data LARES_PORT=8080
EXPOSE 8080
CMD ["python", "-m", "lares_core"]
