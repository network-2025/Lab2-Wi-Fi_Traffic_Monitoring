# src/tasks.py
import os
from celery import Celery
from celery.schedules import crontab
from pymongo import MongoClient
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration from environment variables, with defaults for host network mode
MONGODB_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
DB_NAME = os.environ.get('DB_NAME', 'traffic_monitor')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

# Initialize Celery
# The first argument is the name of the current module.
# The `include` argument is a list of modules to import when the worker starts.
celery_app = Celery('tasks', broker=REDIS_URL, backend=REDIS_URL, include=['src.tasks'])

@celery_app.task
def refresh_database():
    """
    Drops the 'packets' collection from the database to refresh it.
    This is a periodic task managed by Celery Beat.
    """
    try:
        logger.info("Connecting to MongoDB to refresh the database...")
        client = MongoClient(MONGODB_URI)
        db = client[DB_NAME]
        
        logger.info(f"Dropping collection 'packets' from database '{DB_NAME}'...")
        db.packets.drop()
        
        logger.info("Collection 'packets' dropped successfully. The database is now clear and will be repopulated by the running capture process.")
        client.close()
        return "Database cleared successfully."
    except Exception as e:
        logger.error(f"An error occurred while refreshing the database: {e}")
        raise

# Celery Beat Schedule to run the task every 3 hours
celery_app.conf.beat_schedule = {
    'refresh-db-every-3-hours': {
        'task': 'src.tasks.refresh_database',
        'schedule': 3600 * 3, # Run every 3 hours (in seconds)
    },
}

celery_app.conf.timezone = 'UTC'
