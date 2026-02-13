FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY elpres/ ./elpres/
COPY static/ ./static/

RUN pip install --no-cache-dir .

# Game state persisted here
VOLUME /elpres

ENV ELPRES_DATA=/elpres

EXPOSE 8765

CMD ["python", "-m", "elpres.main"]
