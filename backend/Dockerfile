FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    netcat-openbsd \
    curl \
    iputils-ping \
    poppler-utils \
    && apt-get clean

WORKDIR /app
COPY backend/deploy/requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install -r requirements.txt

COPY ./backend ./backend
COPY ./.env  .

COPY backend/deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x backend/deploy/entrypoint.sh

ENTRYPOINT ["backend/deploy/entrypoint.sh"]