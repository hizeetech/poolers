from celery import shared_task

from .services import process_due_void_requests


@shared_task
def process_void_requests():
    return process_due_void_requests()

