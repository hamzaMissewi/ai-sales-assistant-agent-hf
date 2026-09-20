FROM python:3.12-slim

RUN useradd -m -u 1000 user
WORKDIR /app

# tesseract enables local OCR so image uploads become searchable
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user:user . /app
RUN cp .env.example .env \
    && chmod +x start.sh entrypoint.sh \
    && mkdir -p /home/user/.cache \
    && ln -s /var/chroma/cache /home/user/.cache/chroma \
    && chmod -R a+rwX /app \
    && chown -R user:user /home/user

ENV HOME=/home/user
ENV CHROMA_DB_PATH=/var/chroma/vectordb
EXPOSE 7860

ENTRYPOINT ["./entrypoint.sh"]
CMD ["./start.sh"]