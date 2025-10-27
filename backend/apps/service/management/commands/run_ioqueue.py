# ioqueue/management/commands/run_io_service.py
import faulthandler
faulthandler.enable()

from django.core.management.base import BaseCommand
import asyncio
import os
import socket
from backend.apps.service.ioqueue.runner import IORunner


class Command(BaseCommand):
    help = "Run single-process asyncio I/O task service (db + memory)"

    def handle(self, *args, **options):
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        runner = IORunner(worker_id)
        asyncio.run(runner.run())
