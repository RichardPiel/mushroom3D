# Build depuis la racine du dépôt (Dokploy / Docker).
FROM python:3.12-slim-bookworm

WORKDIR /app
COPY server/incubator_ui.py ./

ENV UI_HOST=0.0.0.0 \
    UI_PORT=8765 \
    DATA_DIR=/data

EXPOSE 8765
VOLUME /data

CMD ["python", "-u", "incubator_ui.py"]
