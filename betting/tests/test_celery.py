from django.test import TestCase
from poolbetting.celery import debug_task

class CeleryTestCase(TestCase):
    def test_debug_task(self):
        """
        Test that the debug task can be called.
        We use CELERY_TASK_ALWAYS_EAGER=True in settings (or override here) 
        to execute synchronously for testing.
        """
        with self.settings(CELERY_TASK_ALWAYS_EAGER=True):
            result = debug_task.delay()
            # The debug_task returns None (it just prints), but the result object should be successful
            self.assertTrue(result.successful())
