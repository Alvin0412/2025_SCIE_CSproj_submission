#!/bin/bash
set -e
export PYTHONPATH=/app

mkdir -p /logs

echo "Waiting for the database..."
while ! nc -z db 5432; do
  sleep 1
done
echo "Database is ready!"

#while true; do
#  sleep 1
#done
#python backend/manage.py showmigrations
python backend/manage.py makemigrations pastpaper indexing accounts
python backend/manage.py migrate accounts
python backend/manage.py migrate admin --fake
python backend/manage.py migrate
echo "Database migrated!"
python backend/manage.py collectstatic --noinput
echo "Static files collected!"
#python backend/manage.py rundramatiq
#echo "Dramatiq ready!"
#
#uvicorn backend.config.asgi:application --host 0.0.0.0 --port 8000 --reload
exec supervisord -c backend/deploy/supervisord.conf
