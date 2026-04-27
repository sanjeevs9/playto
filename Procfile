release: cd backend && python manage.py migrate --noinput
web: cd backend && gunicorn playto_pay.wsgi --bind 0.0.0.0:$PORT --workers 2 --threads 4 --log-file -
worker: cd backend && celery -A playto_pay worker -l info --concurrency=2
beat: cd backend && celery -A playto_pay beat -l info
