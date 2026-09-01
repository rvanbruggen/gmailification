FROM python:3.12-slim

# Never bake secrets into this image: tokens, passwords and config are all
# mounted at runtime (/config read-only, /data read-write).

RUN groupadd -g 1000 gmailification && useradd -m -u 1000 -g 1000 gmailification

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gmailification/ ./gmailification/

USER gmailification
VOLUME ["/data"]
EXPOSE 8377

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "-m", "gmailification.healthcheck"]

ENTRYPOINT ["python", "-m", "gmailification"]
