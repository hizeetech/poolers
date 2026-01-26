#!/bin/bash

# Deployment script for PoolBetting

PROJECT_DIR="/root/poolbetting-main"
ENV_DIR="$PROJECT_DIR/env"

echo "Starting deployment..."

cd $PROJECT_DIR

# Pull latest changes (if using git)
# git pull origin main

# Activate virtual environment
source $ENV_DIR/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install uvicorn gunicorn

# Migrate database
python manage.py migrate

# Collect static files
python manage.py collectstatic --noinput

# Restart services
systemctl restart gunicorn
systemctl restart celery
systemctl restart celery-beat

echo "Deployment completed successfully!"
