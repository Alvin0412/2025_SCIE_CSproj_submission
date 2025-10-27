from django.core.management.base import BaseCommand
from django.utils import autoreload
import subprocess
import sys
import os
import signal
import psutil


def kill_old_dramatiq():
    for proc in psutil.process_iter(['pid', 'cmdline']):
        cmdline = proc.info['cmdline']
        if not cmdline:
            continue
        if '/usr/local/bin/dramatiq' in cmdline and 'django_dramatiq.setup' in cmdline:
            proc.kill()


class Command(BaseCommand):
    help = "Run Dramatiq workers with Django autoreload for development"

    def add_arguments(self, parser):
        # Accept all rundramatiq arguments directly
        parser.add_argument("args", nargs="*", help="Arguments for rundramatiq")

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting Dramatiq with autoreload..."))
        rundramatiq_args = options.get("args", [])

        # Run with Django autoreload so workers restart when code changes
        autoreload.run_with_reloader(self.run_workers, rundramatiq_args)

    def run_workers(self, rundramatiq_args):
        # Kill any old dramatiq processes to prevent port conflicts
        kill_old_dramatiq()
        self.stdout.write(self.style.WARNING("Dramatiq workers (re)starting..."))

        # Use the same Python executable and forward all args
        cmd = [sys.executable, "backend/manage.py", "rundramatiq", *rundramatiq_args]
        subprocess.call(cmd, stdout=sys.stdout, stderr=sys.stderr)
